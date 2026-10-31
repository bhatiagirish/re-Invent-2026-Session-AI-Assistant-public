"""Reservation service (Phase 11 — PHASED, gated on config.RESERVATIONS_ENABLED).

Design reference: requirements 21.2, 21.3, 21.4, 21.8, 21.9. This module mirrors
``favorites.py`` exactly: AWS Events is the SOURCE OF TRUTH for reservations and
this module never keeps a local reservation list. It wraps the authenticated
per-user EventsClient and returns structured ActionResult data. It NEVER reports
success unless AWS Events actually confirms it.

A reservation is NOT a favorite — the two are distinct user actions and the
wording here is deliberately kept separate (ActionResult.type is "reserve" /
"cancel-reservation", reasons use reservation vocabulary).

Error/timeout mapping that leaves reservation state unchanged lives at the
handler layer (as it does for favorites); here we translate confirmed AWS Events
responses into truthful per-session outcomes.
"""

from __future__ import annotations

from typing import Optional

import logging
import os

from errors import McpError
from favorites import (_failed_ids, _failure_reason, extract_session_ids,
                       resolve_sessions_with_total)
from mcp_client import EventsClient
from models import ActionResult, Session

# Use the app-configured logger (handler + INFO level set in logging_utils).
# A plain getLogger(__name__) has no handler in Lambda, so its lines are
# silently dropped -- which is why the earlier reserve debug line never
# appeared in CloudWatch.
_log = logging.getLogger("reinvent_assistant")


def _unwrap_result(resp):
    """AWS Events wraps tool payloads in a ``{"result": {...}}`` envelope (and
    occasionally a doubly-nested one). Peel up to two ``result`` layers so the
    bucket extractors see the real succeeded/failed arrays. Non-dict or
    unwrapped inputs are returned unchanged."""
    for _ in range(2):
        if isinstance(resp, dict) and set(resp.keys()) == {"result"} and isinstance(resp["result"], dict):
            resp = resp["result"]
        else:
            break
    return resp if isinstance(resp, dict) else {}


def _succeeded_ids(resp: dict) -> set[str]:
    """Extract the set of sessionIds AWS Events AFFIRMATIVELY confirmed as
    reserved. The ReserveSessions doc says the response "reports each session
    independently, as succeeded or failed" and warns a 200 does not imply
    success -- so we must read the SUCCESS bucket, not infer it from absence of
    failure. Tolerant of the shapes AWS may use for the success list."""
    ids: set[str] = set()
    for key in ("succeeded", "successful", "reserved", "reservations", "success"):
        bucket = resp.get(key)
        if isinstance(bucket, list):
            for entry in bucket:
                if isinstance(entry, str):
                    ids.add(entry)
                elif isinstance(entry, dict):
                    sid = entry.get("sessionId") or entry.get("id")
                    if sid:
                        ids.add(sid)
        elif isinstance(bucket, dict):
            ids.update(bucket.keys())
    return ids


def reserve_session(events: EventsClient, session_id: str,
                    session_code: Optional[str] = None) -> ActionResult:
    """Reserve a seat. success IFF sessionId is absent from `failed[]`
    (Req 21.2, 21.3). An already-reserved failure reason is reported as success
    with reason="already_reserved" — this is NOT a safe blind retry, so we do
    NOT re-send the reservation; any other failure -> failed (Req 21.4)."""
    raw = events.reserve_sessions([session_id])
    # AWS Events wraps the payload in a {"result": {...}} envelope (observed
    # live: the response's only top-level key was "result"). Unwrap it (one or
    # two levels, defensively) before looking for the succeeded/failed buckets,
    # otherwise both come back empty and every reserve reads as "not confirmed".
    resp = _unwrap_result(raw)
    succeeded = _succeeded_ids(resp)
    failed = _failed_ids(resp)
    # Troubleshooting only: with MCP_DEBUG on, log the raw ReserveSessions
    # response (the owner's reservation data, no credentials). Off by default:
    # the response schema is confirmed and pinned in _succeeded_ids/_failed_ids,
    # so routine reserves no longer write reservation details to CloudWatch.
    if os.environ.get("MCP_DEBUG", "").lower() in ("1", "true", "yes"):
        try:
            import json as _json
            _log.info("reserve_sessions RAW_RESPONSE for %s: raw=%s unwrapped=%s",
                      session_id,
                      _json.dumps(raw, default=str)[:1500] if isinstance(raw, dict) else repr(raw)[:1500],
                      _json.dumps(resp, default=str)[:1500] if isinstance(resp, dict) else repr(resp)[:1500])
            _log.info("reserve_sessions parsed: in_succeeded=%s in_failed=%s",
                      session_id in succeeded, session_id in failed)
        except Exception:
            pass

    # TRUTHFUL success requires POSITIVE confirmation: the id must appear in the
    # succeeded bucket. We do NOT infer success from mere absence from failed[]
    # (that false-positived a "seats full" session that AWS reported as failed
    # in a bucket our old code didn't read, or in neither bucket).
    if session_id in succeeded and session_id not in failed:
        return ActionResult(type="reserve", status="success",
                            session_id=session_id, session_code=session_code)

    # Explicit failure: map an already-reserved reason to idempotent success
    # (desired end state already holds; not a safe blind re-send), everything
    # else -> failed with the reason surfaced for the UI/model.
    if session_id in failed:
        reason = (_failure_reason(resp, session_id) or "").lower()
        if "already" in reason or "exist" in reason or "duplicate" in reason:
            return ActionResult(type="reserve", status="success",
                                session_id=session_id, session_code=session_code,
                                reason="already_reserved")
        # The live ReserveSessions failure carries a `code` like "sessionFull"
        # (camelCase); match that plus human phrasings defensively.
        if "full" in reason or "capacity" in reason or "soldout" in reason or "sold out" in reason:
            return ActionResult(type="reserve", status="failed",
                                session_id=session_id, session_code=session_code,
                                reason="session_full")
        if "clash" in reason or "conflict" in reason or "overlap" in reason:
            return ActionResult(type="reserve", status="failed",
                                session_id=session_id, session_code=session_code,
                                reason="schedule_conflict")
        return ActionResult(type="reserve", status="failed",
                            session_id=session_id, session_code=session_code,
                            reason="could_not_reserve")

    # Ambiguous: AWS confirmed neither success nor failure for this id. Per the
    # doc ("treat a code you do not recognize as a refusal you cannot act on"),
    # fail SAFE -- never claim a seat we cannot prove was reserved.
    return ActionResult(type="reserve", status="failed",
                        session_id=session_id, session_code=session_code,
                        reason="not_confirmed")


def cancel_reservation(events: EventsClient, session_id: str,
                       session_code: Optional[str] = None) -> ActionResult:
    """Cancel a reservation. A 404 / not-currently-reserved outcome means the
    desired end state already holds and is reported as success with
    reason="not_currently_reserved", NOT a false failure (Req 21.8, 21.9); any
    other error -> failed, leaving reservation state unchanged."""
    try:
        resp = events.cancel_reservation(session_id)
        if isinstance(resp, dict) and resp.get("notReserved"):
            return ActionResult(type="cancel-reservation", status="success",
                                session_id=session_id, session_code=session_code,
                                reason="not_currently_reserved")
        return ActionResult(type="cancel-reservation", status="success",
                            session_id=session_id, session_code=session_code)
    except McpError as exc:
        text = f"{exc}".lower()
        if "404" in text or "not found" in text or "not reserved" in text:
            return ActionResult(type="cancel-reservation", status="success",
                                session_id=session_id, session_code=session_code,
                                reason="not_currently_reserved")
        return ActionResult(type="cancel-reservation", status="failed",
                            session_id=session_id, session_code=session_code,
                            reason="could_not_cancel_reservation")


def get_reserved_ids(events: EventsClient) -> list[str]:
    """Return the owner's currently reserved sessionIds from AWS Events (source
    of truth), via a SINGLE ``GetSchedule`` call reading the normalized
    ``reserved[]`` array.

    Mirrors :func:`favorites.get_favorite_ids` (which reads
    ``schedule.get("favorites")``); this reads ``schedule.get("reserved")``.
    Order is preserved and blank / duplicate ids are dropped, keeping only the
    first occurrence of each id (Req 6.2). The list is derived solely from the
    live GetSchedule result — never a local cache, never fabricated (Req 6.3) —
    and is NOT capped, so reserved counts stay truthful (Req 6.5).
    """
    schedule = events.get_schedule()
    if not isinstance(schedule, dict):
        return []
    # Accept plain id strings AND objects carrying the id (see
    # favorites.extract_session_ids). Previously only strings were kept, which
    # dropped every object entry and made the reserved count read 0.
    return extract_session_ids(schedule.get("reserved"))


def get_reserved_sessions_with_total(events: EventsClient,
                                     max_details: Optional[int] = None
                                     ) -> tuple[list[Session], int]:
    """Return the owner's reserved sessions as full ``Session`` objects together
    with the TRUE number of reserved ids (Req 5.1, 5.2, 5.3, 5.4).

    Mirrors :func:`favorites.get_favorites_with_total`. Fetches the reserved ids
    from AWS Events (source of truth) via :func:`get_reserved_ids` — a single
    ``GetSchedule`` read of the normalized ``reserved[]`` array — then delegates
    the ordered id->``Session`` detail fan-out to the SHARED
    :func:`favorites.resolve_sessions_with_total` helper so favorites and
    reservations share one bounded-concurrency implementation (reuse, not
    copy-paste).

    Guarantees inherited from the shared helper: order follows the reserved
    list, any un-fetchable id is OMITTED rather than fabricated, the detail
    fan-out is bounded by ``max_details`` (defaulting to
    ``config.MAX_SCHEDULE_DETAILS``), and ``total`` is the true reserved id count
    so callers can show an honest "showing N of M" (Req 5.4). An expired MCP
    token is re-raised for the caller's single bounded refresh+retry.
    """
    all_ids = get_reserved_ids(events)
    return resolve_sessions_with_total(events, all_ids, max_details=max_details)


def get_reserved_sessions(events: EventsClient,
                          max_details: Optional[int] = None) -> list[Session]:
    """Return the owner's reserved sessions as full ``Session`` objects.

    Thin wrapper over :func:`get_reserved_sessions_with_total`, mirroring how
    :func:`favorites.get_favorites` delegates to ``get_favorites_with_total``.
    Order follows the reserved list and un-fetchable sessions are skipped rather
    than fabricated. Use :func:`get_reserved_sessions_with_total` when you need
    the true reserved count to report truncation honestly (Req 5.1, 5.3).
    """
    sessions, _total = get_reserved_sessions_with_total(
        events, max_details=max_details)
    return sessions
