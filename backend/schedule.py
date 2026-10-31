"""Personal-time datetime conversion + validation (Phase 5 / Task 8.2).

The owner always works in Pacific event-local time (``America/Los_Angeles``).
This module converts those local start/end values to the AWS Events MCP
datetime contract and enforces the contract's constraints BEFORE any MCP call,
so a malformed request that bypasses the client is still rejected server-side
(never trust the client).

MCP datetime contract (requirements.md glossary, Req 18.2/18.3, 19.2/19.3):
  * ``startDateTime`` / ``endDateTime`` are ``YYYY-MM-DDTHH:MM:SS`` interpreted
    as UTC with NO trailing ``Z`` and NO offset.
  * Stored to the minute: the seconds component MUST be ``00``.
  * Duration MUST be a positive whole number of 5-minute increments.
  * end MUST be strictly after start.

Worked example (December is PST = UTC-8; DST not in effect):
  Dec 1, 8:30-10:30 AM Pacific
    -> start = 2026-12-01T16:30:00, end = 2026-12-01T18:30:00

A real IANA rule (``zoneinfo.ZoneInfo("America/Los_Angeles")``) is used rather
than a hardcoded ``-8``, so both PST (UTC-8) and PDT (UTC-7) dates convert
correctly year-round.

Design reference: design.md "Phase 5 Addendum" section 2 (MCP datetime contract
and timezone handling).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from errors import ValidationError

# Pacific event-local timezone. ZoneInfo applies the real IANA rule, so the
# correct offset (PST -8 / PDT -7) is selected per date automatically.
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# Contract output format: no 'Z', no offset, seconds included (always "00").
_CONTRACT_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Durations are stored/validated in 5-minute increments.
_INCREMENT_MINUTES = 5


def _parse_pacific(value: str, field: str) -> datetime:
    """Parse a Pacific-local datetime string into a tz-aware datetime.

    Accepts the contract-shaped form ``YYYY-MM-DDTHH:MM:SS`` as well as the
    minute-only form ``YYYY-MM-DDTHH:MM`` (seconds default to 00). The returned
    datetime is anchored to ``America/Los_Angeles``; it is NOT yet validated for
    the contract constraints (that happens in :func:`to_utc_contract`).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"The {field} time is required.", field=field
        )
    raw = value.strip()
    parsed: datetime | None = None
    for fmt in (_CONTRACT_FORMAT, "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValidationError(
            f"The {field} time must look like YYYY-MM-DDTHH:MM "
            f"(Pacific event-local time).",
            field=field,
        )
    # Attach Pacific tz (naive -> aware). ZoneInfo resolves PST vs PDT by date.
    return parsed.replace(tzinfo=PACIFIC_TZ)


def pacific_to_utc_contract(value: str, field: str = "start") -> str:
    """Convert a single Pacific-local time to the UTC contract string.

    Validates that the seconds component is ``00`` (Req 18.2/18.3) and returns
    ``YYYY-MM-DDTHH:MM:SS`` in UTC with no ``Z`` and no offset. Raises
    :class:`ValidationError` on malformed input or non-zero seconds.
    """
    local = _parse_pacific(value, field)
    if local.second != 0 or local.microsecond != 0:
        raise ValidationError(
            f"The {field} time must be on a whole minute "
            f"(seconds must be 00).",
            field=field,
        )
    utc = local.astimezone(timezone.utc)
    return utc.strftime(_CONTRACT_FORMAT)


def to_utc_contract(start: str, end: str) -> tuple[str, str]:
    """Convert Pacific-local start/end to the UTC contract pair, validating all
    personal-time constraints BEFORE returning (and thus before any MCP call).

    Returns ``(start_utc, end_utc)`` each as ``YYYY-MM-DDTHH:MM:SS`` (UTC, no
    ``Z``, no offset, seconds ``00``).

    Raises :class:`ValidationError` naming the specific violated constraint when:
      * either value is malformed or missing,
      * either value has a non-zero seconds component,
      * end is not strictly after start, or
      * the duration is not a positive whole number of 5-minute increments.

    Validates: Requirements 18.2, 18.3, 19.2, 19.3
    """
    # Parse both ends first so we validate seconds on each (Req 18.2).
    start_local = _parse_pacific(start, "start")
    end_local = _parse_pacific(end, "end")

    for field, dt in (("start", start_local), ("end", end_local)):
        if dt.second != 0 or dt.microsecond != 0:
            raise ValidationError(
                f"The {field} time must be on a whole minute "
                f"(seconds must be 00).",
                field=field,
            )

    # Compare in absolute (UTC) terms so a DST boundary can't distort duration.
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    # end strictly after start (Req 18.2/18.3).
    if end_utc <= start_utc:
        raise ValidationError(
            "The end time must be after the start time.",
            field="end",
        )

    # Duration a positive whole number of 5-minute increments (Req 18.2/18.3).
    duration = end_utc - start_utc
    total_seconds = duration.total_seconds()
    if total_seconds % (_INCREMENT_MINUTES * 60) != 0:
        raise ValidationError(
            f"The duration must be a whole number of "
            f"{_INCREMENT_MINUTES}-minute increments.",
            field="end",
        )

    return start_utc.strftime(_CONTRACT_FORMAT), end_utc.strftime(_CONTRACT_FORMAT)


# ---------------------------------------------------------------------------
# Schedule aggregation (Task 8.5 / Req 17.1-17.3, 22.1-22.2)
# ---------------------------------------------------------------------------
#
# build_schedule_view() turns a raw GetSchedule result into a structured
# ScheduleView. It mirrors favorites.get_favorites: resolve each reserved and
# favorite sessionId to full Session details through a bounded-concurrency
# GetSession fan-out, preserving list order and SKIPPING any session that can't
# be fetched rather than fabricating one. Personal-time entries map straight
# through PersonalTimeEntry.from_mcp. Each list is ordered by start time
# ascending, tie-broken by end time ascending, then by title case-insensitive
# (Req 17.2). AWS Events is the sole source of truth; nothing is cached and
# nothing is invented (Req 17.7, 22.2).

import concurrent.futures
import time
from typing import Optional

from errors import McpError, McpTimeoutError, McpUnavailableError
from mcp_client import McpAuthExpired  # early use in _resolve_sessions
from models import PersonalTimeEntry, ScheduleView, Session

# Items with no determinable start/end sort to the end rather than being
# dropped or fabricated with a fake time. datetime.max in UTC is the sentinel.
_SORT_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _extract_session_ids(entries: list) -> list[str]:
    """Pull sessionIds out of a GetSchedule array, order-preserving.

    Tolerant of the two shapes AWS Events uses for reserved[]/favorites[]: a
    list of plain id strings, or a list of objects carrying the id under a few
    common keys. Blank/duplicate ids are dropped; nothing is fabricated.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        sid: Optional[str] = None
        if isinstance(entry, str):
            sid = entry
        elif isinstance(entry, dict):
            sid = entry.get("sessionId") or entry.get("id") or entry.get("sessionID")
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


def _resolve_sessions(events, ids: list[str], max_details: Optional[int] = None,
                      deadline: Optional[float] = None) -> list[Session]:
    """Resolve sessionIds to Session details via a bounded concurrent GetSession
    fan-out (reuses the favorites.get_favorites pattern).

    Each GetSession is independent and read-only, so they run in a small thread
    pool to stay within the request time budget. Order follows ``ids``; any id
    whose detail can't be fetched (error, timeout, or missing session) is
    skipped, never fabricated. ``deadline`` (seconds) overrides
    ``config.SCHEDULE_FETCH_DEADLINE`` so several fan-outs can share one budget.
    """
    if max_details is None:
        max_details = config.MAX_SCHEDULE_DETAILS
    ids = ids[:max_details]
    if not ids:
        return []

    # A per-session fetch failure (not found / transient / tool error) is
    # SKIPPED, never fabricated. But an EXPIRED TOKEN (McpAuthExpired) is a
    # whole-request condition, not a per-session one: swallowing it would make
    # every one of ~N calls burn its own timeout before we give up (the 29s
    # hang we observed on a stale session). So we FAIL FAST -- capture the first
    # auth failure and re-raise after the pool unwinds, letting the caller do
    # its single bounded refresh+retry (or surface "please reconnect").
    auth_expired: list[McpAuthExpired] = []
    errors: dict = {}          # error type -> count, for the fetch summary log

    def fetch(sid: str):
        try:
            return sid, events.get_session(sid)
        except McpAuthExpired as exc:
            auth_expired.append(exc)
            return sid, None
        except (McpError, McpTimeoutError, McpUnavailableError) as exc:
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
            return sid, None

    by_id: dict[str, Session] = {}
    done = 0
    workers = min(config.SCHEDULE_FETCH_WORKERS, len(ids))
    if deadline is None:
        deadline = config.SCHEDULE_FETCH_DEADLINE
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(fetch, sid): sid for sid in ids}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=deadline):
                sid, sess = fut.result()
                done += 1
                if sess:
                    by_id[sid] = sess
        except concurrent.futures.TimeoutError:
            # Overall budget exceeded: return what resolved in time (complete-as-
            # possible) rather than hang to the 29s wall. Stragglers are skipped,
            # never fabricated.
            pass
    finally:
        # Don't block shutdown on slow stragglers (py>=3.9 supports cancel_futures).
        ex.shutdown(wait=False, cancel_futures=True)
    # These failures are swallowed per session by design, so record a summary:
    # how many were asked for, came back, were still running at the deadline, and
    # which error types occurred (throttling and timeouts show up here).
    log_fetch_summary("schedule/detail_fetch", len(ids), len(by_id),
                      len(ids) - done, errors)
    if auth_expired:
        # Token is expired for the whole fan-out -> surface it so the caller
        # refreshes once and retries, instead of returning a silently-empty
        # schedule or hanging.
        raise auth_expired[0]
    return [by_id[sid] for sid in ids if sid in by_id]


def _session_bounds(session: Session) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a Session for sorting.

    A Session carries Pacific event-local date/time plus a length in minutes;
    convert to absolute UTC so sessions and (already-UTC) personal-time entries
    sort on one axis. end = start + length when a length is present, else end
    collapses to start. A session with no usable time sorts to the far future.
    """
    st = session.session_time
    if not st or not st.date or not st.time:
        return _SORT_FAR_FUTURE, _SORT_FAR_FUTURE
    try:
        local = datetime.strptime(f"{st.date}T{st.time}", "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return _SORT_FAR_FUTURE, _SORT_FAR_FUTURE
    start = local.replace(tzinfo=PACIFIC_TZ).astimezone(timezone.utc)
    minutes = st.length if isinstance(st.length, int) and st.length > 0 else 0
    end = start + timedelta(minutes=minutes)
    return start, end


def _personal_bounds(entry: PersonalTimeEntry) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a PersonalTimeEntry for sorting.

    start/end are stored in the UTC contract form (YYYY-MM-DDTHH:MM:SS, no
    offset); parse them as UTC. A missing/unparseable bound sorts to the far
    future so it falls after well-formed items rather than being dropped.
    """
    def parse(value: str) -> datetime:
        if not value:
            return _SORT_FAR_FUTURE
        try:
            return datetime.strptime(value, _CONTRACT_FORMAT).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return _SORT_FAR_FUTURE

    start = parse(entry.start)
    end = parse(entry.end) if entry.end else start
    return start, end


def _order(items: list, bounds) -> list:
    """Order items by start asc, then end asc, then title case-insensitive.

    ``bounds`` maps an item to its (start_utc, end_utc) pair. The title tiebreak
    (Req 17.2) reads ``.title`` which both Session and PersonalTimeEntry expose.
    Python's sort is stable, so equal keys keep their source order.
    """
    def key(item):
        start, end = bounds(item)
        title = (getattr(item, "title", "") or "").casefold()
        return (start, end, title)

    return sorted(items, key=key)


def _fmt_pacific_window(start_utc, end_utc) -> str:
    """Render a (start_utc, end_utc) pair as a readable Pacific event-local
    window, e.g. "2026-12-01 Tue 08:30-09:30 PT". Returns "time unknown" for the
    far-future sentinel so an item with no usable time is labelled, never
    fabricated with a made-up slot."""
    if start_utc == _SORT_FAR_FUTURE:
        return "time unknown"
    s_local = start_utc.astimezone(PACIFIC_TZ)
    day = s_local.strftime("%Y-%m-%d %a %H:%M")
    if end_utc and end_utc != _SORT_FAR_FUTURE and end_utc != start_utc:
        e_local = end_utc.astimezone(PACIFIC_TZ)
        # Same calendar day -> show just the end clock time; else full end stamp.
        if e_local.date() == s_local.date():
            return f"{day}-{e_local.strftime('%H:%M')} PT"
        return f"{day} - {e_local.strftime('%Y-%m-%d %a %H:%M')} PT"
    return f"{day} PT"


def filter_view_to_date(view: ScheduleView, pacific_date: str) -> ScheduleView:
    """Return a ScheduleView containing only items that fall on ``pacific_date``
    (a Pacific event-local 'YYYY-MM-DD'). An item's day is its START time
    converted to Pacific. Items with no determinable time are excluded from a
    day-scoped view (we can't claim they're on that day). The ids-only
    GetSchedule means we still had to resolve details to know dates; this just
    narrows what we SHOW/narrate so a single-day reply is short.
    """
    try:
        target = datetime.strptime(pacific_date.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return view  # unparseable -> no filtering (caller shows the full view)

    def on_day_session(sess) -> bool:
        start, _ = _session_bounds(sess)
        if start == _SORT_FAR_FUTURE:
            return False
        return start.astimezone(PACIFIC_TZ).date() == target

    def on_day_personal(pt) -> bool:
        start, _ = _personal_bounds(pt)
        if start == _SORT_FAR_FUTURE:
            return False
        return start.astimezone(PACIFIC_TZ).date() == target

    return ScheduleView(
        reserved=[s for s in view.reserved if on_day_session(s)],
        favorites=[s for s in view.favorites if on_day_session(s)],
        personal_time=[p for p in view.personal_time if on_day_personal(p)],
        # Keep the completeness marker: if some sessions could not be loaded,
        # this day may be missing items, and the UI/agent should say so.
        incomplete=dict(view.incomplete),
        indexed=dict(view.indexed),
    )


def summarize_schedule_for_model(view: ScheduleView) -> str:
    """Render a ScheduleView as a per-item, time-bearing list for the agent.

    Unlike a bare count, this gives the model each item's KIND, code/title, and
    an explicit Pacific start-end window, so it can answer time questions such
    as "which of my sessions overlap?". Every line comes straight from the
    resolved ScheduleView (real AWS Events data); nothing is invented, and an
    item with no determinable time is shown as "time unknown" rather than
    dropped or guessed. Items are listed in the view's chronological order.
    """
    lines: list[str] = []

    def session_line(kind: str, sess: Session) -> str:
        start, end = _session_bounds(sess)
        window = _fmt_pacific_window(start, end)
        code = sess.abbreviation or sess.session_id or "?"
        title = sess.title or "(untitled)"
        return f"- [{kind}] {code} \"{title}\" | {window}"

    for sess in view.reserved:
        lines.append(session_line("reserved", sess))
    for sess in view.favorites:
        lines.append(session_line("favorite", sess))
    for pt in view.personal_time:
        start, end = _personal_bounds(pt)
        window = _fmt_pacific_window(start, end)
        title = pt.title or "(untitled)"
        lines.append(f"- [personal time] \"{title}\" | {window}")

    return "\n".join(lines)


def find_conflicts(view: ScheduleView) -> list[dict]:
    """Return the list of overlapping item PAIRS in a ScheduleView, computed
    server-side (not by the model eyeballing a list). Each pair is a dict with
    both items' kind, label, and Pacific window. Two items conflict when their
    [start,end) windows overlap; items with no determinable time are ignored.
    Reserved sessions, favorites, and personal-time blocks are all compared
    against each other (a favorite can clash with a personal block, etc.).
    """
    items = []  # (start_utc, end_utc, kind, label)

    def add_session(kind, sess):
        start, end = _session_bounds(sess)
        if start == _SORT_FAR_FUTURE or end == start:
            return  # no usable window -> can't judge overlap
        label = (sess.abbreviation or sess.session_id or "?")
        if sess.title:
            label = f"{label} \"{sess.title}\""
        items.append((start, end, kind, label))

    for sess in view.reserved:
        add_session("reserved", sess)
    for sess in view.favorites:
        add_session("favorite", sess)
    for pt in view.personal_time:
        start, end = _personal_bounds(pt)
        if start == _SORT_FAR_FUTURE or not end or end == start:
            continue
        items.append((start, end, "personal time", pt.title or "(untitled)"))

    items.sort(key=lambda t: (t[0], t[1]))
    conflicts: list[dict] = []
    for i in range(len(items)):
        s_i, e_i, k_i, l_i = items[i]
        for j in range(i + 1, len(items)):
            s_j, e_j, k_j, l_j = items[j]
            if s_j >= e_i:
                break  # sorted by start; no later item can overlap item i
            # overlap iff s_j < e_i and s_i < e_j (half-open intervals)
            if s_i < e_j and s_j < e_i:
                conflicts.append({
                    "a": {"kind": k_i, "label": l_i,
                          "window": _fmt_pacific_window(s_i, e_i)},
                    "b": {"kind": k_j, "label": l_j,
                          "window": _fmt_pacific_window(s_j, e_j)},
                })
    return conflicts


def build_schedule_view(events, max_details: Optional[int] = None,
                        time_budget: Optional[float] = None,
                        catalog=None) -> ScheduleView:
    """Build an ordered ScheduleView from the owner's GetSchedule result.

    ``time_budget`` (seconds, default ``SCHEDULE_REQUEST_BUDGET_SECONDS``) is the
    time this whole call may take. Whatever ``GetSchedule`` uses comes off what
    the GetSession detail fetch may spend, so a slow ``GetSchedule`` can no longer
    push the request past Lambda's 29 s limit (which killed it with a 500).

    Resolves reserved + favorite sessionIds to full Session details via a
    bounded-concurrency GetSession fan-out (reusing the favorites pattern,
    preserving order and skipping un-fetchable sessions), maps personalTime[]
    entries to PersonalTimeEntry, and orders every list by start time ascending,
    then end time ascending, then title case-insensitive (Req 17.2). Every item
    derives solely from AWS Events state; nothing is cached or fabricated
    (Req 17.1, 17.3, 17.7, 22.1, 22.2).
    """
    total_budget = float(config.SCHEDULE_REQUEST_BUDGET_SECONDS
                         if time_budget is None else time_budget)
    began = time.monotonic()
    schedule = events.get_schedule()
    if not isinstance(schedule, dict):
        return ScheduleView()

    # Diagnostic (MCP_DEBUG only): log the RAW shape of the first favorite/
    # reserved entry so we can see whether GetSchedule already carries title/
    # time (letting us skip the per-session GetSession fan-out) or returns bare
    # ids. Scrubbed + truncated by log_event; no secrets present in schedule data.
    import os as _os
    if _os.environ.get("MCP_DEBUG", "").lower() in ("1", "true", "yes"):
        fav_raw = (schedule.get("favorites") or [None])[0]
        res_raw = (schedule.get("reserved") or [None])[0]
        log_event(
            request_id=None, operation="schedule/shape_probe", success=True,
            extra={"favoriteEntry": str(fav_raw)[:400],
                   "reservedEntry": str(res_raw)[:400],
                   "favCount": len(schedule.get("favorites") or []),
                   "resCount": len(schedule.get("reserved") or [])},
        )

    reserved_ids = _extract_session_ids(schedule.get("reserved") or [])
    favorite_ids = _extract_session_ids(schedule.get("favorites") or [])

    # The two fan-outs share ONE time budget. Run back to back with a budget
    # each, they could add up to twice SCHEDULE_FETCH_DEADLINE and overrun the
    # Lambda timeout; sharing keeps the whole detail fetch within one deadline.
    # The detail fetch gets at most SCHEDULE_FETCH_DEADLINE, and never more than
    # what is left of the whole-request budget after GetSchedule (keeping a
    # second of margin to build and return the response).
    left = total_budget - (time.monotonic() - began) - 1.0
    budget = max(1.0, min(float(config.SCHEDULE_FETCH_DEADLINE), left))
    started = time.monotonic()

    # RESERVED sessions are real commitments, so their details are always read
    # live. FAVORITES take their details from the catalog index when it is ready
    # (no AWS Events calls); only favorites missing from it are read live. That
    # is what keeps a long favorites list from needing ~70 parallel connections.
    snap = None
    if catalog is not None and config.FAVORITE_DETAILS_FROM_INDEX:
        snap = catalog.load_ready()
    index_by_id = {s.session_id: s for s in snap.sessions} if snap is not None else {}
    favorite_ids_capped = favorite_ids[:max_details if max_details is not None
                                       else config.MAX_SCHEDULE_DETAILS]
    from_index = {sid: index_by_id[sid] for sid in favorite_ids_capped if sid in index_by_id}
    favorites_live_ids = [sid for sid in favorite_ids_capped if sid not in from_index]

    reserved = _resolve_sessions(events, reserved_ids, max_details=max_details,
                                 deadline=budget)
    remaining = max(1.0, budget - (time.monotonic() - started))
    live_favorites = _resolve_sessions(events, favorites_live_ids,
                                       max_details=max_details, deadline=remaining)
    found = dict(from_index)
    found.update({s.session_id: s for s in live_favorites})
    favorites = [found[sid] for sid in favorite_ids_capped if sid in found]

    indexed: dict = {}
    if from_index:
        indexed = {"favorites": len(from_index), "ageSeconds": snap.age_seconds}

    personal_time = [
        PersonalTimeEntry.from_mcp(item)
        for item in (schedule.get("personalTime") or [])
        if isinstance(item, dict)
    ]

    # Be explicit when the view is not the whole story: the id arrays from
    # GetSchedule are the TRUE counts, the resolved sessions may be fewer (size
    # cap, deadline, or sessions AWS Events lists but cannot return).
    incomplete: dict = {}
    if len(reserved) < len(reserved_ids):
        incomplete["reserved"] = {"loaded": len(reserved), "total": len(reserved_ids)}
    if len(favorites) < len(favorite_ids):
        incomplete["favorites"] = {"loaded": len(favorites), "total": len(favorite_ids)}

    return ScheduleView(
        reserved=_order(reserved, _session_bounds),
        favorites=_order(favorites, _session_bounds),
        personal_time=_order(personal_time, _personal_bounds),
        incomplete=incomplete,
        indexed=indexed,
    )


# ---------------------------------------------------------------------------
# Personal-time service functions (Task 9.1 / Req 18, 19, 20)
# ---------------------------------------------------------------------------
#
# These wrap the EventsClient personal-time MCP wrappers with the same
# structured-result + friendly-error-mapping discipline favorites.py uses:
#   * Field length caps (title ≤200, description ≤1000, location ≤200) and the
#     Pacific→UTC datetime contract are validated BEFORE any MCP call, so a
#     malformed request never reaches AWS Events (Req 18.5, 18.2/18.3,
#     19.2/19.3). A validation failure names the specific violated constraint,
#     leaves Schedule_View state unchanged, and RETAINS the owner's input so the
#     UI can re-populate the form for retry.
#   * create / update return the created/updated PersonalTimeEntry on success.
#     update is FULL REPLACEMENT — every field is forwarded (empty optionals
#     clear their prior value); the EventsClient wrapper enforces that
#     (Req 19.1, 19.4).
#   * delete is IDEMPOTENT — a not-found / already-removed outcome is reported
#     as success rather than a false failure (Req 20.3); the EventsClient
#     wrapper already maps that case.
#   * MCP errors / timeouts map to a friendly failure that leaves Schedule_View
#     state unchanged and retains the submitted input for retry (Req 18.8,
#     19.5, 20.5, 20.6). AWS Events remains the sole source of truth; nothing is
#     cached or fabricated.

from dataclasses import dataclass

import config
from errors import (
    EventAccessDeniedError,
    McpError,
    McpTimeoutError,
    McpUnavailableError,
    ValidationError,
)
from mcp_client import EventsClient, McpAuthExpired
from logging_utils import log_event, log_fetch_summary


def _log_pt_failure(op: str, exc: Exception, personal_time_id: str = None) -> None:
    """Record the REAL MCP failure cause for a personal-time op so it is
    diagnosable in CloudWatch. The user still gets a generic friendly message;
    this line (scrubbed of any secret-looking values by log_event) is what an
    operator reads. Without it the true cause (e.g. an \"Unknown tool\" from the
    MCP server, a 403, or a 401) is lost."""
    detail = f"{type(exc).__name__}: {exc}"
    log_event(
        request_id=None,
        operation=f"personal_time/{op}",
        success=False,
        error_category=type(exc).__name__,
        extra={"cause": detail[:500],
               "personalTimeId": personal_time_id} if personal_time_id
              else {"cause": detail[:500]},
    )


@dataclass
class PersonalTimeResult:
    """Structured result of a personal-time create/update/delete.

    Analogous to models.ActionResult but shaped for personal time: it carries
    the resulting entry (on success) and, on a soft failure, a friendly message
    plus the retained input so the UI can re-offer it for retry without a hard
    error. ``status`` is one of ``success`` | ``failed`` | ``not_found``.
    """

    type: str                                    # create | update | delete
    status: str                                  # success | failed | not_found
    entry: Optional[PersonalTimeEntry] = None    # populated on create/update success
    personal_time_id: Optional[str] = None
    reason: Optional[str] = None                 # machine code for the UI
    message: Optional[str] = None                # friendly, user-facing text
    retained_input: Optional[dict] = None        # echoed field values for retry

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_public_dict(self) -> dict:
        d: dict = {"type": self.type, "status": self.status}
        if self.entry is not None:
            d["personalTime"] = self.entry.to_public_dict()
        if self.personal_time_id is not None:
            d["personalTimeId"] = self.personal_time_id
        if self.reason is not None:
            d["reason"] = self.reason
        if self.message is not None:
            d["message"] = self.message
        if self.retained_input is not None:
            d["retainedInput"] = self.retained_input
        return {k: v for k, v in d.items() if v is not None}


# Friendly failure messages per operation (Req 18.8 / 19.5 / 20.5 / 20.6).
_FAIL_MESSAGE = {
    "create": "The personal time block could not be created. Please try again.",
    "update": "The personal time block could not be updated. Please try again.",
    "delete": "The personal time block could not be deleted. Please try again.",
}
_TIMEOUT_MESSAGE = {
    "create": "The personal time block could not be created in time. Please try again.",
    "update": "The personal time block could not be updated in time. Please try again.",
    "delete": "The personal time block could not be deleted in time. Please try again.",
}


def _retained(title: Optional[str], start: Optional[str], end: Optional[str],
              description: Optional[str], location: Optional[str],
              personal_time_id: Optional[str] = None) -> dict:
    """Echo the owner's submitted values so the UI can re-populate the form for
    retry (Req 18.3/18.8, 19.5). Pacific-local start/end are returned as entered
    (NOT the UTC contract form) because that is what the owner typed."""
    retained: dict = {
        "title": title,
        "start": start,
        "end": end,
        "description": description,
        "location": location,
    }
    if personal_time_id is not None:
        retained["personalTimeId"] = personal_time_id
    return retained


def _check_length_caps(title: str, description: Optional[str],
                       location: Optional[str]) -> None:
    """Enforce the personal-time field length caps BEFORE any MCP call.

    Raises ValidationError (naming the field and its max length) when a cap is
    exceeded, so the caller rejects the input without calling AWS Events
    (Req 18.5 / 19 full-field-set). Empty / absent optionals are fine.
    """
    if title is not None and len(title) > config.PERSONAL_TIME_TITLE_MAX_LEN:
        raise ValidationError(
            f"The title must be {config.PERSONAL_TIME_TITLE_MAX_LEN} characters "
            f"or fewer.",
            field="title",
        )
    if description is not None and len(description) > config.PERSONAL_TIME_DESCRIPTION_MAX_LEN:
        raise ValidationError(
            f"The description must be {config.PERSONAL_TIME_DESCRIPTION_MAX_LEN} "
            f"characters or fewer.",
            field="description",
        )
    if location is not None and len(location) > config.PERSONAL_TIME_LOCATION_MAX_LEN:
        raise ValidationError(
            f"The location must be {config.PERSONAL_TIME_LOCATION_MAX_LEN} "
            f"characters or fewer.",
            field="location",
        )


def _require_title(title: Optional[str]) -> str:
    """Title is a required field (Req 18.4). Reject empty/blank before MCP."""
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("A title is required.", field="title")
    return title


def create_personal_time(events: EventsClient, title: str, start: str, end: str,
                         description: Optional[str] = None,
                         location: Optional[str] = None) -> PersonalTimeResult:
    """Create a personal-time block (Req 18.1, 18.6, 18.8).

    Validation order (all BEFORE the MCP call so a bad request never reaches AWS
    Events): required ``title`` present, length caps, then Pacific→UTC datetime
    contract (seconds ``00``, end strictly after start, positive whole 5-minute
    multiple). On any validation failure a ValidationError propagates to the
    handler, which turns it into a 400 that names the constraint and retains the
    owner's input — Schedule_View is untouched because no call was made.

    On success returns a PersonalTimeResult carrying the created
    PersonalTimeEntry (incl. its server-assigned personalTimeId). An MCP error /
    timeout becomes a soft ``failed`` result with a friendly message and the
    retained input, leaving Schedule_View unchanged for retry.
    """
    title = _require_title(title)
    _check_length_caps(title, description, location)
    # Pacific-local -> UTC contract (raises ValidationError on bad datetimes).
    start_utc, end_utc = to_utc_contract(start, end)

    try:
        created = events.create_personal_time(
            title=title, start=start_utc, end=end_utc,
            description=description, location=location,
        )
    except McpTimeoutError as exc:
        _log_pt_failure("create", exc)
        return PersonalTimeResult(
            type="create", status="failed", reason="timeout",
            message=_TIMEOUT_MESSAGE["create"],
            retained_input=_retained(title, start, end, description, location),
        )
    except (McpError, McpUnavailableError, EventAccessDeniedError, McpAuthExpired) as exc:
        _log_pt_failure("create", exc)
        return PersonalTimeResult(
            type="create", status="failed", reason="could_not_create",
            message=_FAIL_MESSAGE["create"],
            retained_input=_retained(title, start, end, description, location),
        )

    entry = PersonalTimeEntry.from_mcp(_entry_payload(created))
    return PersonalTimeResult(
        type="create", status="success", entry=entry,
        personal_time_id=entry.personal_time_id or None,
    )


def update_personal_time(events: EventsClient, personal_time_id: str,
                         title: str, start: str, end: str,
                         description: Optional[str] = None,
                         location: Optional[str] = None) -> PersonalTimeResult:
    """Update a personal-time block with FULL-REPLACEMENT semantics (Req 19).

    Every field is forwarded on every call; an optional left empty is sent empty
    so its prior value is cleared (the EventsClient wrapper enforces this). As
    with create, length caps and the datetime contract are validated BEFORE the
    MCP call. A referenced personalTimeId that no longer exists maps to a
    ``not_found`` result leaving Schedule_View unchanged (Req 19.6); an MCP
    error / timeout maps to a soft ``failed`` result that leaves the entry in its
    pre-edit appearance and retains the owner's input for retry (Req 19.5).
    """
    if not isinstance(personal_time_id, str) or not personal_time_id.strip():
        raise ValidationError(
            "A personal time id is required to update an entry.",
            field="personalTimeId",
        )
    title = _require_title(title)
    _check_length_caps(title, description, location)
    start_utc, end_utc = to_utc_contract(start, end)

    try:
        updated = events.update_personal_time(
            personal_time_id=personal_time_id, title=title,
            start=start_utc, end=end_utc,
            description=description, location=location,
        )
    except McpTimeoutError as exc:
        _log_pt_failure("update", exc, personal_time_id)
        return PersonalTimeResult(
            type="update", status="failed", reason="timeout",
            personal_time_id=personal_time_id,
            message=_TIMEOUT_MESSAGE["update"],
            retained_input=_retained(title, start, end, description, location,
                                     personal_time_id),
        )
    except (McpUnavailableError, EventAccessDeniedError, McpAuthExpired) as exc:
        _log_pt_failure("update", exc, personal_time_id)
        return PersonalTimeResult(
            type="update", status="failed", reason="could_not_update",
            personal_time_id=personal_time_id,
            message=_FAIL_MESSAGE["update"],
            retained_input=_retained(title, start, end, description, location,
                                     personal_time_id),
        )
    except McpError as exc:
        # An unknown personalTimeId surfaces as a not-found error -> not_found,
        # Schedule_View unchanged (Req 19.6); any other MCP error -> soft fail.
        if _is_not_found(exc):
            return PersonalTimeResult(
                type="update", status="not_found", reason="not_found",
                personal_time_id=personal_time_id,
                message="That personal time block no longer exists.",
                retained_input=_retained(title, start, end, description,
                                         location, personal_time_id),
            )
        _log_pt_failure("update", exc, personal_time_id)
        return PersonalTimeResult(
            type="update", status="failed", reason="could_not_update",
            personal_time_id=personal_time_id,
            message=_FAIL_MESSAGE["update"],
            retained_input=_retained(title, start, end, description, location,
                                     personal_time_id),
        )

    entry = PersonalTimeEntry.from_mcp(_entry_payload(updated))
    # Preserve the known id if the server echo omitted it.
    if not entry.personal_time_id:
        entry.personal_time_id = personal_time_id
    return PersonalTimeResult(
        type="update", status="success", entry=entry,
        personal_time_id=entry.personal_time_id or personal_time_id,
    )


def delete_personal_time(events: EventsClient,
                         personal_time_id: str) -> PersonalTimeResult:
    """Delete a personal-time block — IDEMPOTENT (Req 20.1-20.6).

    An empty / malformed id is rejected before any MCP call (Req 20.2). A
    not-found / already-removed outcome is reported as success because the
    operation is idempotent (Req 20.3) — the EventsClient wrapper already maps
    that case, so a normal return here is always success. A real error maps to a
    soft ``failed`` result and a timeout to a timeout failure, each leaving
    Schedule_View unchanged (Req 20.5, 20.6).
    """
    if not isinstance(personal_time_id, str) or not personal_time_id.strip():
        raise ValidationError(
            "A personal time id is required to delete an entry.",
            field="personalTimeId",
        )

    try:
        events.delete_personal_time(personal_time_id)
    except McpTimeoutError as exc:
        _log_pt_failure("delete", exc, personal_time_id)
        return PersonalTimeResult(
            type="delete", status="failed", reason="timeout",
            personal_time_id=personal_time_id,
            message=_TIMEOUT_MESSAGE["delete"],
        )
    except (McpError, McpUnavailableError, EventAccessDeniedError, McpAuthExpired) as exc:
        _log_pt_failure("delete", exc, personal_time_id)
        return PersonalTimeResult(
            type="delete", status="failed", reason="could_not_delete",
            personal_time_id=personal_time_id,
            message=_FAIL_MESSAGE["delete"],
        )

    return PersonalTimeResult(
        type="delete", status="success", personal_time_id=personal_time_id,
    )


def _entry_payload(resp: dict) -> dict:
    """Pull the personal-time entry object out of a create/update response.

    AWS Events may return the entry at the top level or nested under a
    ``personalTime`` / ``personalTimeEntry`` key; tolerate both without
    fabricating fields."""
    if not isinstance(resp, dict):
        return {}
    for key in ("personalTime", "personalTimeEntry", "entry"):
        nested = resp.get(key)
        if isinstance(nested, dict):
            return nested
    return resp


def _is_not_found(exc: Exception) -> bool:
    """Heuristic: did an UpdatePersonalTime fail because the id doesn't exist?
    Mirrors mcp_client._looks_like_not_found so update can report not_found
    (Req 19.6) rather than a generic failure."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(t in text for t in (
        "404", "not found", "notfound", "does not exist", "no such",
        "already removed", "already deleted",
    ))
