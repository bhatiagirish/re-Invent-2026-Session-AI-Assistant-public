"""Favorites service (Phase 4).

Design reference: design.md "Favorite Flow", Properties 2 (favorite truthfulness)
and 3 (unfavorite semantics), requirements 6/7/8/9.

AWS Events is the SOURCE OF TRUTH for favorites — this module never keeps a
local favorites list. It wraps the authenticated per-user EventsClient and
returns structured ActionResult / Session data. It NEVER reports success unless
AWS Events actually confirms it.
"""

from __future__ import annotations

from typing import Optional

from errors import (
    EventAccessDeniedError,
    McpError,
    McpTimeoutError,
    McpUnavailableError,
)
import config
from logging_utils import log_event
from mcp_client import EventsClient, McpAuthExpired
from models import ActionResult, Session


def _failed_ids(resp: dict) -> set[str]:
    """Extract the set of sessionIds that FAILED from an AssociateFavorites
    response, tolerant of a few shapes AWS Events may use."""
    failed = resp.get("failed")
    ids: set[str] = set()
    if isinstance(failed, list):
        for entry in failed:
            if isinstance(entry, str):
                ids.add(entry)
            elif isinstance(entry, dict):
                sid = entry.get("sessionId") or entry.get("id")
                if sid:
                    ids.add(sid)
    elif isinstance(failed, dict):
        ids.update(failed.keys())
    return ids


def _failure_reason(resp: dict, session_id: str) -> Optional[str]:
    failed = resp.get("failed")
    if isinstance(failed, dict):
        entry = failed.get(session_id)
        if isinstance(entry, dict):
            return entry.get("reason") or entry.get("message") or entry.get("code")
        if isinstance(entry, str):
            return entry
    if isinstance(failed, list):
        for entry in failed:
            if isinstance(entry, dict) and (entry.get("sessionId") == session_id):
                return entry.get("reason") or entry.get("message") or entry.get("code")
    return None


def favorite_session(events: EventsClient, session_id: str,
                     session_code: Optional[str] = None) -> ActionResult:
    """Add a favorite. success IFF sessionId is absent from `failed[]`
    (Property 2). Already-favorited is reported as success (idempotent from the
    user's perspective) rather than a hard failure; other failures -> failed."""
    resp = events.associate_favorites([session_id])
    failed = _failed_ids(resp)
    if session_id not in failed:
        return ActionResult(type="favorite", status="success",
                            session_id=session_id, session_code=session_code)
    reason = (_failure_reason(resp, session_id) or "").lower()
    if "already" in reason or "exist" in reason or "duplicate" in reason:
        return ActionResult(type="favorite", status="success",
                            session_id=session_id, session_code=session_code,
                            reason="already_favorited")
    return ActionResult(type="favorite", status="failed",
                        session_id=session_id, session_code=session_code,
                        reason="could_not_favorite")


def unfavorite_session(events: EventsClient, session_id: str,
                       session_code: Optional[str] = None) -> ActionResult:
    """Remove a favorite. A 404 means 'not currently favorited' and is reported
    as success (the desired end state), NOT a false failure (Property 3)."""
    try:
        events.disassociate_favorite(session_id)
        return ActionResult(type="unfavorite", status="success",
                            session_id=session_id, session_code=session_code)
    except McpError as exc:
        text = f"{exc}".lower()
        if "404" in text or "not found" in text or "not favorited" in text:
            return ActionResult(type="unfavorite", status="success",
                                session_id=session_id, session_code=session_code,
                                reason="not_currently_favorited")
        return ActionResult(type="unfavorite", status="failed",
                            session_id=session_id, session_code=session_code,
                            reason="could_not_unfavorite")


def resolve_sessions_with_total(events: EventsClient, ids: list[str],
                                max_details: Optional[int] = None
                                ) -> tuple[list[Session], int]:
    """Resolve an ORDERED list of session ids to full ``Session`` details via a
    bounded-concurrency ``GetSession`` fan-out, returning ``(sessions, total)``.

    This is the single shared fan-out used by both favorites and reservations
    (reuse, not copy-paste). It is caller-agnostic: the caller supplies the
    already-ordered, already-normalized id list (e.g. from GetSchedule
    ``favorites[]`` or ``reserved[]``); this function only resolves them to
    details. Guarantees:

      * **Order preserved** — the returned sessions follow the input ``ids``
        order exactly.
      * **No fabrication** — any id whose detail can't be fetched (missing
        session, transient error, or timeout) is OMITTED, never invented.
      * **Bounded fan-out** — at most ``max_details`` ids are fetched; it
        defaults to ``config.MAX_SCHEDULE_DETAILS`` so a normal list is shown in
        full. Concurrency is bounded by ``config.SCHEDULE_FETCH_WORKERS`` (the
        existing bounded pool), turning dozens of independent read-only round
        trips into a few waves to stay within the request time budget.
      * **Honest total** — ``total`` is the TRUE number of input ids
        (``len(ids)``), independent of how many resolved to details or whether
        the fan-out was capped, so callers can say "showing N of M" instead of
        implying the shown set is complete.

    An expired MCP token (``McpAuthExpired``) is a whole-request condition, not
    a per-session miss: it is re-raised (fail fast) so the caller can do its
    single bounded refresh+retry instead of letting every fetch burn its own
    timeout. A per-session miss is skipped.
    """
    import concurrent.futures

    if max_details is None:
        max_details = config.MAX_SCHEDULE_DETAILS

    total = len(ids)
    capped = ids[:max_details]
    if not capped:
        return [], total

    auth_expired: list[McpAuthExpired] = []

    def fetch(sid: str):
        try:
            return sid, events.get_session(sid)
        except McpAuthExpired as exc:
            auth_expired.append(exc)
            return sid, None
        except (McpError, McpTimeoutError, McpUnavailableError):
            return sid, None

    by_id: dict = {}
    workers = min(config.SCHEDULE_FETCH_WORKERS, len(capped))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for sid, sess in ex.map(fetch, capped):
            if sess:
                by_id[sid] = sess
    if auth_expired:
        raise auth_expired[0]
    # Preserve the original id ordering; omit un-fetchable ids (no fabrication).
    return [by_id[sid] for sid in capped if sid in by_id], total


def extract_session_ids(entries) -> list[str]:
    """Pull sessionIds out of a GetSchedule ``favorites[]`` / ``reserved[]``
    array, order-preserving.

    AWS Events may return either plain id strings or objects that carry the id
    under ``sessionId`` (or ``id``). Accepting both matters: a reader that kept
    only strings silently dropped every object entry, so a count of reserved
    seats came back 0 even though the schedule view (which already accepted
    objects) showed them. Blank and duplicate ids are dropped; nothing is
    fabricated.
    """
    if not isinstance(entries, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        sid = None
        if isinstance(entry, str):
            sid = entry
        elif isinstance(entry, dict):
            sid = entry.get("sessionId") or entry.get("id") or entry.get("sessionID")
        if not isinstance(sid, str):
            continue
        sid = sid.strip()
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


def get_favorite_ids(events: EventsClient) -> list[str]:
    """Return the user's current favorite sessionIds from AWS Events (source of
    truth). Never a local cache."""
    schedule = events.get_schedule()
    if isinstance(schedule, dict):
        return extract_session_ids(schedule.get("favorites"))
    return []


def get_favorites(events: EventsClient,
                  max_details: Optional[int] = None) -> list[Session]:
    """Return the user's favorites as full Session objects.

    Fetches favorite ids from GetSchedule, then GetSession for each. The detail
    fetches run CONCURRENTLY (bounded pool) because each is independent and
    read-only, turning dozens of sequential round trips into a few waves so we
    stay within the request time budget. Sessions that can't be fetched are
    skipped rather than fabricated; order follows the favorites list.

    ``max_details`` bounds the per-session detail fan-out; it defaults to
    ``config.MAX_SCHEDULE_DETAILS`` (not a small hard-coded number) so a normal
    favorites list is shown in full. Use :func:`get_favorites_with_total` when
    you need the true favorite count to report truncation honestly.
    """
    sessions, _total = get_favorites_with_total(events, max_details=max_details)
    return sessions


def get_favorites_with_total(events: EventsClient,
                             max_details: Optional[int] = None
                             ) -> tuple[list[Session], int]:
    """Like :func:`get_favorites`, but also return the TRUE number of favorites
    (the full id count from GetSchedule), independent of how many were resolved
    to details. Lets callers say "showing N of M" instead of implying the shown
    set is complete when the detail fan-out was capped (no fabrication).

    Fetches the favorite ids from GetSchedule (source of truth), then delegates
    the ordered id->Session detail fan-out to the shared
    :func:`resolve_sessions_with_total` helper so favorites and reservations
    share one bounded-concurrency implementation.
    """
    all_ids = get_favorite_ids(events)
    return resolve_sessions_with_total(events, all_ids, max_details=max_details)
