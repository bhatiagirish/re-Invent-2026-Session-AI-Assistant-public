"""Strands Agent for re:Invent 2026 natural-language session discovery (Phase 3).

Design reference: design.md "Agent System Prompt", "handleChat" pseudocode,
Properties 8 (no fabrication) and 9 (timeout bound).

Architecture note (why custom tools rather than raw MCP tool passthrough):
  The agent is given two @tool functions — `search_sessions` and
  `get_session_details` — that internally call the per-user, authenticated
  `EventsClient` (Phase 2). This lets us:
    * expose ONLY discovery + details (favorites stay hidden this phase),
    * enforce pagination limits and a result cap inside the tool (Req 10-12, 27),
    * RECORD the real tool results so the structured `sessions[]` returned to
      the client comes from genuine MCP data, never from parsing LLM prose
      (Req 13-14, Property 8),
    * keep the AWS Events event id fixed server-side (Req 7 — the model never
      guesses it).

The agent still performs genuine agentic tool selection from natural language
(Req 8/12): there is no hard-coded keyword->query mapping.
"""

from __future__ import annotations

import concurrent.futures
from typing import Optional

import config
from errors import McpError, McpTimeoutError, McpUnavailableError, ValidationError
from logging_utils import log_event
from mcp_client import EventsClient, McpAuthExpired
from models import ActionResult, ScheduleView, Session
import favorites as fav
import reservations as resv
import schedule as sched


SYSTEM_PROMPT = """You are the AWS re:Invent 2026 Session Assistant.
Your job is to help an authenticated re:Invent 2026 attendee discover and \
understand event sessions.

AWS re:Invent 2026 runs in Las Vegas (Pacific time). The event day-to-date map \
is EXACTLY (use it whenever the user names a weekday; do NOT guess a date from \
a day name):
  - Monday    = 2026-11-30
  - Tuesday   = 2026-12-01
  - Wednesday = 2026-12-02
  - Thursday  = 2026-12-03
  - Friday    = 2026-12-04
So "the Tuesday keynote" is 2026-12-01, "Wednesday" is 2026-12-02, etc.

Keynotes are NOT in the session catalog; add them as personal-time blocks. They \
are on Tuesday/Wednesday/Thursday only, at these Pacific times (use these \
directly when the user asks to add a keynote — do NOT ask them for the time):
  - Tuesday  (2026-12-01) CEO keynote: 08:30-10:30  -> start 2026-12-01T08:30, end 2026-12-01T10:30
  - Wednesday(2026-12-02) CTO keynote: 08:30-10:00  -> start 2026-12-02T08:30, end 2026-12-02T10:00
  - Thursday (2026-12-03) keynote:     08:30-10:00  -> start 2026-12-03T08:30, end 2026-12-03T10:00

The AWS Events MCP tools are the AUTHORITATIVE source of truth for all session \
information. ALWAYS call the relevant tool when the user asks about re:Invent \
sessions, their schedule, their favorites, or counts — on EVERY such request, \
even if you believe you already know the answer from an earlier turn. The \
underlying data changes between turns (the user favorites sessions and adds \
personal time), so an answer from memory is both stale and a fabrication. Never \
state session details, counts, or schedule contents that did not come from a \
tool result in the CURRENT turn.

You have these tools:
- search_sessions(query): find sessions relevant to a natural-language topic, \
service, level, session type, or day. Use this for any discovery request.
- get_session_details(reference): get full details for one session by its \
session code (e.g. "ANT301") or by an ordinal reference like "#2".
- favorite_session(reference): add ONE session to the user's AWS Events \
favorites, identified by session code or a resolved reference. Only say it was \
favorited if the tool reports success.
- unfavorite_session(reference): remove ONE session from the user's favorites.
- show_favorites(): list the user's current favorites from AWS Events.
- count_sessions(scope, group_by, filters): answer "how many" questions. Use \
scope="favorites" for the user's favorites (fast), scope="reserved" for the \
user's reserved seats (fast), and scope="catalog" only when they clearly ask \
about ALL re:Invent sessions (slower). group_by (date, venue, \
room, level, type, topic, service) gives a breakdown; filters (keyword, date, venue, \
service, level, session_type) narrow it. For "how many sessions ABOUT <topic>" \
(e.g. Kiro, Bedrock, serverless) always use the `keyword` filter - it matches \
the same fields as search, so counts agree with search results. Examples: "how \
many favorites do I have" -> favorites, no group; "favorites by date" -> \
favorites grouped by date; "how many have I reserved" -> reserved, no group; \
"my reserved by venue" -> reserved grouped by venue; "how many Kiro sessions" \
-> catalog, keyword=Kiro; "how many sessions at MGM Grand" -> catalog filtered \
by venue.
- show_schedule(date?): show the user's schedule (reserved sessions, \
favorites, personal-time blocks). Pass `date` ('YYYY-MM-DD', 2026-11-30 to \
2026-12-04) for one day, or omit for the full schedule. The individual items \
are rendered to the user as CARDS automatically; this tool returns ONLY counts \
to you. So DO NOT list, name, or describe individual sessions in your reply — \
you do not have their details and must never invent them. Just write a \
one-sentence summary of the counts (e.g. "Here's your Dec 2 schedule — 5 \
favorites and 1 personal-time block, shown below.") and optionally offer to \
check for conflicts. You MUST call show_schedule EXACTLY ONCE for every \
schedule request — ALWAYS call it fresh; NEVER answer a "show my schedule" \
request from memory or from an earlier turn, and NEVER say items are "shown \
below" unless you actually called show_schedule in THIS turn (the cards only \
render when the tool ran this turn). Counts you state must come from this \
turn's tool result, not from recollection.
- find_schedule_conflicts(date?): find time-clashing items. Overlaps are \
computed server-side and returned as explicit conflicting pairs — report \
exactly what it returns and never infer conflicts yourself. Use this ONLY when \
the user asks about conflicts/overlaps/double-booking.
- add_personal_time(title, start, end, description?, location?): add ONE \
personal-time block (e.g. a keynote not in the catalog, lunch, travel, a \
meeting). start and end are given in Pacific event-local time as \
YYYY-MM-DDTHH:MM (e.g. "2026-12-01T08:30"); the backend converts them to the \
required form and validates them before the call. When the user gives a \
WEEKDAY ("the Tuesday keynote"), resolve it with the event day-to-date map \
above (Tuesday = 2026-12-01) — never guess. Only say it was created if \
the tool reports success.
- update_personal_time(reference, title, start, end, description?, location?): \
edit an existing personal-time block identified by its id or an ordinal \
reference like "#2". This is a FULL replacement: pass every field; any optional \
field left empty is cleared.
- delete_personal_time(reference): remove a personal-time block by its id or an \
ordinal reference like "#2". Deletion is idempotent, so a block that is already \
gone still reports success.

Rules:
- NEVER fabricate or guess session information. Session IDs, codes, titles, \
speakers, dates, times, venues, rooms, levels, and availability must come only \
from tool results. If a field is not in the tool result, do not invent it.
- Use conversation context to resolve references such as "#1", "#2", "the first \
session", "the previous session", "that workshop", or "the Bedrock session you \
showed me". If a reference is ambiguous or out of range, ask a brief clarifying \
question instead of guessing.
- You CAN favorite sessions and show the user's favorites using the favorite \
tools. Favoriting and RESERVING a session are different operations: never claim \
the user has reserved a seat, and never offer to reserve (reservation is out of \
scope). Only confirm a favorite/unfavorite AFTER the tool reports success.
- You CAN view the user's schedule and create, update, and delete their \
personal-time blocks using the schedule and personal-time tools. When you \
present the schedule, label every item by its kind — a reserved session, a \
favorite, or a personal-time block — and never relabel one kind as another \
(e.g. never call a favorite a reservation or a personal-time block a reserved \
seat). Personal-time start/end are Pacific event-local; only confirm a \
create/update/delete AFTER the tool reports success, and if validation fails \
report the specific problem the tool names.
- Reservation is currently unavailable. Do NOT offer to reserve a seat, do NOT \
claim any session is reserved, and present the reserved-sessions list only from \
real schedule data (it is empty until the reservation capability is used).
- Keep answers concise and useful. When you list sessions, refer to them by \
their position (1, 2, 3, ...) and session code so the user can follow up.
- If a tool returns no matching sessions, say so plainly and suggest a broader \
topic. If a tool fails, tell the user you could not retrieve the information \
right now; do not answer from your own knowledge.
"""


class SessionRecorder:
    """Collects the real Session objects returned by tool calls during a turn.

    The structured `sessions[]` in the API response is built from THIS record,
    not from the assistant's text (Req 14, Property 8).
    """

    def __init__(self):
        self.last_result: list[Session] = []
        self.tools_invoked: list[str] = []
        self.total_result_count: int = 0
        self.action = None  # ActionResult / PersonalTimeResult from a mutating tool
        # Phase 9: the structured ScheduleView from a show_schedule call, and the
        # PersonalTimeResult from a personal-time create/update/delete. Both are
        # surfaced alongside sessions[]/action, built from REAL tool results
        # (never parsed from the assistant's prose) — same discipline as record()
        # (Req 23.7, Property 8).
        self.schedule_view: Optional[ScheduleView] = None
        self.personal_time_result = None  # PersonalTimeResult

    def record(self, tool: str, sessions: list[Session]) -> None:
        self.tools_invoked.append(tool)
        self.last_result = sessions
        self.total_result_count = len(sessions)

    def record_action(self, tool: str, action) -> None:
        self.tools_invoked.append(tool)
        self.action = action

    def record_schedule(self, tool: str, schedule_view: ScheduleView) -> None:
        """Record the structured schedule from a show_schedule call."""
        self.tools_invoked.append(tool)
        self.schedule_view = schedule_view

    def record_personal_time(self, tool: str, result) -> None:
        """Record a personal-time create/update/delete outcome.

        The PersonalTimeResult is action-shaped (``.type``/``.status``/
        ``.to_public_dict()``), so it is also surfaced through the shared
        ``action`` slot the /chat response already serializes — reusing the
        ActionResult pathway rather than inventing a new response field."""
        self.tools_invoked.append(tool)
        self.personal_time_result = result
        self.action = result


def _search_sessions_impl(events: EventsClient, recorder: SessionRecorder,
                          query: str, limit: int) -> str:
    """Walk paginated ListSessions, filter by relevance, cap results.

    Pagination strategy (Req 10): fetch up to MAX_LIST_PAGES pages, stop early
    once we have enough relevant matches or run out of pages. We do NOT pull the
    entire ~2000-session catalog for every request.
    """
    limit = max(1, min(limit or config.DEFAULT_SESSION_RESULT_LIMIT,
                       config.MAX_SESSION_RESULT_LIMIT))
    terms = [t for t in _tokenize(query) if len(t) > 2]

    matches: list[Session] = []
    next_token = None
    pages = 0
    while pages < config.MAX_LIST_PAGES:
        page, next_token = events.list_sessions(include_abstracts=False,
                                                next_token=next_token)
        pages += 1
        for s in page:
            if _relevant(s, terms):
                matches.append(s)
                if len(matches) >= limit:
                    break
        if len(matches) >= limit or not next_token:
            break

    recorder.record("search_sessions", matches[:limit])
    if not matches:
        return ("NO_RESULTS: no sessions matched. Suggest the user try a broader "
                "topic or a specific AWS service.")
    return _summarize_for_model(matches[:limit])


def _get_details_impl(events: EventsClient, recorder: SessionRecorder,
                      session_id: str) -> str:
    session = events.get_session(session_id)
    if not session:
        recorder.record("get_session_details", [])
        return "NOT_FOUND: no session found for that reference."
    recorder.record("get_session_details", [session])
    return _summarize_one_for_model(session)


def _favorite_impl(events: EventsClient, recorder: SessionRecorder,
                   session_id: str, session_code: str = None) -> str:
    action = fav.favorite_session(events, session_id, session_code)
    recorder.record_action("favorite_session", action)
    if action.status == "success":
        code = action.session_code or session_id
        if action.reason == "already_favorited":
            return f"ALREADY_FAVORITED: {code} was already in the user's favorites."
        return f"FAVORITED: {code} was added to the user's favorites."
    return "FAVORITE_FAILED: the favorite operation did not succeed; do not claim success."


def _unfavorite_impl(events: EventsClient, recorder: SessionRecorder,
                     session_id: str, session_code: str = None) -> str:
    action = fav.unfavorite_session(events, session_id, session_code)
    recorder.record_action("unfavorite_session", action)
    if action.status == "success":
        code = action.session_code or session_id
        if action.reason == "not_currently_favorited":
            return f"NOT_FAVORITED: {code} was not in the user's favorites."
        return f"UNFAVORITED: {code} was removed from the user's favorites."
    return "UNFAVORITE_FAILED: the unfavorite operation did not succeed."


def _show_favorites_impl(events: EventsClient, recorder: SessionRecorder) -> str:
    sessions, total = fav.get_favorites_with_total(events)
    recorder.record("show_favorites", sessions)
    if total == 0:
        return "NO_FAVORITES: the user has no favorite sessions yet."
    shown = len(sessions)
    summary = _summarize_for_model(sessions)
    # Honesty guard (Property 8 / no fabrication): the id count from GetSchedule
    # is the TRUE total. Detail resolution is bounded, so when fewer sessions
    # were resolved than the user actually has, say so explicitly instead of
    # letting the model imply the shown list is complete.
    if shown < total:
        return (f"PARTIAL_FAVORITES: showing {shown} of {total} favorite "
                f"session(s). Tell the user these are the first {shown} of their "
                f"{total} favorites (not all of them), then list them.\n{summary}")
    return (f"ALL_FAVORITES: showing all {total} favorite session(s).\n{summary}")


# ---------------------------------------------------------------------------
# Reservation tool implementations (Phase 11 — PHASED, gated on the flag)
# ---------------------------------------------------------------------------
#
# These mirror the favorite/unfavorite impls: call reservations.py over the
# per-user authenticated EventsClient, RECORD the real ActionResult, and return
# a short status token the model narrates. A reservation is NOT a favorite — the
# wording and ActionResult types ("reserve" / "cancel-reservation") are kept
# distinct, and we never claim success unless AWS Events confirmed it (Req 21.1,
# 21.3, 23.8). These impls and their @tool wrappers are only exposed to the
# agent while config.RESERVATIONS_ENABLED is true.


def _reserve_impl(events: EventsClient, recorder: SessionRecorder,
                  session_id: str, session_code: str = None) -> str:
    # Walk-up gate: most breakout sessions are WALK-UP ONLY (AWS Events marks
    # them isReservable=false) and should not be reserved. Decline up front
    # rather than sending a ReserveSessions the catalog says isn't allowed. We
    # suppress ONLY on an explicit false; if the session can't be fetched or the
    # flag is unknown, fall through to the normal reserve (don't block a
    # legitimate reservation on a data hiccup).
    try:
        sess = events.get_session(session_id)
    except (McpError, McpTimeoutError, McpUnavailableError, McpAuthExpired):
        sess = None  # best-effort pre-check only; real errors surface below
    if sess is not None and sess.is_reservable is False:
        code = session_code or (sess.abbreviation or session_id)
        return (f"NOT_RESERVABLE: {code} is walk-up only and cannot be reserved "
                "(seats are first-come at the room). Do NOT claim a seat was "
                "reserved; tell the user it's walk-up only.")
    action = resv.reserve_session(events, session_id, session_code)
    recorder.record_action("reserve_session", action)
    if action.status == "success":
        code = action.session_code or session_id
        if action.reason == "already_reserved":
            return f"ALREADY_RESERVED: {code} was already reserved for the user."
        return f"RESERVED: a seat for {code} was reserved for the user."
    code = action.session_code or session_id
    if action.reason == "session_full":
        return (f"SESSION_FULL: {code} is full, so no seat was reserved. Tell the "
                "user it's full (walk-up seating may be possible at the room); "
                "do NOT claim a seat was reserved.")
    if action.reason == "schedule_conflict":
        return (f"SCHEDULE_CONFLICT: {code} clashes with something already on the "
                "user's schedule, so no seat was reserved. Do NOT claim success.")
    if action.reason == "not_confirmed":
        return (f"RESERVE_UNCONFIRMED: AWS Events did not confirm a seat for {code}, "
                "so no reservation was made. Do NOT claim a seat was reserved; "
                "suggest the user try again or check the AWS Events catalog.")
    return "RESERVE_FAILED: the reservation did not succeed; do not claim a seat was reserved."


def _cancel_reservation_impl(events: EventsClient, recorder: SessionRecorder,
                             session_id: str, session_code: str = None) -> str:
    action = resv.cancel_reservation(events, session_id, session_code)
    recorder.record_action("cancel_reservation", action)
    if action.status == "success":
        code = action.session_code or session_id
        if action.reason == "not_currently_reserved":
            return f"NOT_RESERVED: {code} was not currently reserved for the user."
        return f"RESERVATION_CANCELLED: the reservation for {code} was cancelled."
    return "CANCEL_RESERVATION_FAILED: the cancellation did not succeed."


# ---------------------------------------------------------------------------
# Schedule + personal-time tool implementations (Phase 9 / Req 18, 19, 20, 23)
# ---------------------------------------------------------------------------
#
# These mirror the favorites impls: they call the per-user authenticated
# EventsClient (via schedule.py), RECORD the real structured result on the
# recorder, and return a short status string for the model to narrate. The
# structured ScheduleView / PersonalTimeResult surfaced to the client always
# comes from genuine tool results, never from the assistant's prose (Property 8,
# Req 23.7). Personal-time start/end arrive as Pacific-local phrasing; the
# conversion + validation happen inside schedule.py BEFORE any MCP call, so a
# malformed datetime never reaches AWS Events (Req 18.2/18.3, 19.2/19.3).


def _show_schedule_impl(events: EventsClient, recorder: SessionRecorder,
                        date: str = None) -> str:
    try:
        view = sched.build_schedule_view(events)
    except McpAuthExpired:
        # Expired token for the whole fan-out. Fail fast with a controlled
        # signal rather than letting ~67 calls stall toward the 29s wall. The
        # model relays this; the user reconnects (connect.sh). We re-raise so
        # the chat handler's bounded refresh+retry can also attempt a refresh.
        raise
    # Optional single-day scope: narrow the (already-resolved) view to one
    # Pacific day so a day-scoped reply is short and focused (Nov 30 - Dec 4).
    day_label = ""
    if date:
        view = sched.filter_view_to_date(view, date)
        day_label = f" for {date}"
    recorder.record_schedule("show_schedule", view)
    n_reserved = len(view.reserved)
    n_fav = len(view.favorites)
    n_personal = len(view.personal_time)
    if not (n_reserved or n_fav or n_personal):
        if date:
            return (f"EMPTY_SCHEDULE: nothing on the user's schedule{day_label}. "
                    "Say so plainly for that day.")
        return ("EMPTY_SCHEDULE: the user has no reserved sessions, favorites, or "
                "personal-time blocks yet.")
    # The raw GetSchedule id arrays are the TRUE counts; the Schedule_View only
    # holds the sessions whose details were resolved (bounded fan-out). When the
    # FULL list was truncated, surface "showing N of M" so the model never
    # implies the shown schedule is complete (Property 8 / no fabrication).
    # Skip this entirely for a day-scoped view: there the lower count is just
    # other days being filtered out, not truncation -- a "showing N of M" there
    # would be misleading.
    partial = ""
    if not date:
        try:
            raw = events.get_schedule()
            total_reserved = len(sched._extract_session_ids(raw.get("reserved") or []))
            total_fav = len(sched._extract_session_ids(raw.get("favorites") or []))
        except Exception:
            total_reserved, total_fav = n_reserved, n_fav
        notes = []
        if n_reserved < total_reserved:
            notes.append(f"reserved: showing {n_reserved} of {total_reserved}")
        if n_fav < total_fav:
            notes.append(f"favorites: showing {n_fav} of {total_fav}")
        partial = (" PARTIAL (" + "; ".join(notes) + ") - tell the user some items "
                   "are not shown.") if notes else ""
    header = ("SCHEDULE{}: {} reserved session(s), {} favorite(s), {} personal-time "
              "block(s).{} Reserved seats, favorites, and personal time are distinct "
              "kinds.").format(day_label, n_reserved, n_fav, n_personal, partial)
    # The full item list (kind, code/title, Pacific start-end window) is given to
    # YOU for reasoning (e.g. conflict questions), but the user ALREADY SEES these
    # items rendered as cards in the UI. So DO NOT transcribe or re-list every
    # session in your reply -- that is slow and redundant. Instead write a SHORT
    # summary (a sentence or two: counts, the day span, any notable clustering),
    # and only enumerate specific sessions if the user explicitly asked to list
    # them or asked a question that requires naming them (e.g. conflicts). Items
    # marked "time unknown" have no scheduled time.
    # IMPORTANT: we do NOT hand the model the item list. The user already sees
    # every item rendered as cards (the structured ScheduleView is returned to
    # the frontend). Giving the model the list led it to re-transcribe all items
    # (slow -> turn timeouts) and sometimes fabricate/mis-attribute them. So the
    # tool returns ONLY counts + the day label; the model writes a one-sentence
    # summary and points the user at the cards. For conflicts, the model must
    # call find_schedule_conflicts (server-side), never eyeball a list.
    return (header +
            " The individual items are shown to the user as cards below -- do NOT "
            "list them in your reply; give a one-sentence summary and, if useful, "
            "offer to check for conflicts (use find_schedule_conflicts).")


def _find_conflicts_impl(events: EventsClient, recorder: SessionRecorder,
                         date: str = None) -> str:
    """Compute overlapping schedule items SERVER-SIDE and return a compact,
    authoritative list of conflicting pairs for the model to relay. The model
    does not infer overlaps from a transcribed list (which risked fabrication);
    it just reports what this returns."""
    try:
        view = sched.build_schedule_view(events)
    except McpAuthExpired:
        raise
    day_label = ""
    if date:
        view = sched.filter_view_to_date(view, date)
        day_label = f" for {date}"
    # Keep the cards in sync with the (possibly day-filtered) view.
    recorder.record_schedule("find_schedule_conflicts", view)
    conflicts = sched.find_conflicts(view)
    if not conflicts:
        return (f"NO_CONFLICTS: no overlapping items on the user's schedule"
                f"{day_label}. Tell them their schedule is clear of time clashes"
                f"{day_label}.")
    lines = [f"CONFLICTS{day_label}: {len(conflicts)} overlapping pair(s). "
             "Report these to the user:"]
    for c in conflicts:
        a, b = c["a"], c["b"]
        lines.append(f"- [{a['kind']}] {a['label']} ({a['window']})  CLASHES WITH  "
                     f"[{b['kind']}] {b['label']} ({b['window']})")
    return "\n".join(lines)


def _personal_time_status_line(result) -> str:
    """Turn a PersonalTimeResult into a short status string for the model.

    Mirrors the favorite/unfavorite status strings: a controlled token the model
    narrates, never a success claim unless the tool actually succeeded."""
    op = (result.type or "").upper()
    if result.status == "success":
        label = result.entry.title if result.entry and result.entry.title else "the block"
        if result.type == "delete":
            return "PERSONAL_TIME_DELETED: the personal-time block was removed."
        verb = "created" if result.type == "create" else "updated"
        return f"PERSONAL_TIME_{verb.upper()}: '{label}' was {verb}."
    if result.status == "not_found":
        msg = result.message or "that personal-time block no longer exists."
        return f"PERSONAL_TIME_NOT_FOUND: {msg} The schedule is unchanged."
    # failed
    msg = result.message or "the operation did not succeed."
    return (f"PERSONAL_TIME_{op}_FAILED: {msg} Do not claim success; "
            f"the schedule is unchanged.")


def _add_personal_time_impl(events: EventsClient, recorder: SessionRecorder,
                            title: str, start: str, end: str,
                            description: Optional[str] = None,
                            location: Optional[str] = None) -> str:
    try:
        result = sched.create_personal_time(
            events, title=title, start=start, end=end,
            description=description, location=location,
        )
    except ValidationError as exc:
        # Validation fails BEFORE any MCP call (seconds/5-min/order, length caps,
        # required title). Report the specific violated constraint; schedule is
        # untouched (Req 18.3/18.4/18.5). Nothing recorded as an action.
        recorder.tools_invoked.append("add_personal_time")
        return f"PERSONAL_TIME_INVALID: {exc}"
    recorder.record_personal_time("add_personal_time", result)
    return _personal_time_status_line(result)


def _update_personal_time_impl(events: EventsClient, recorder: SessionRecorder,
                               personal_time_id: str, title: str,
                               start: str, end: str,
                               description: Optional[str] = None,
                               location: Optional[str] = None) -> str:
    try:
        result = sched.update_personal_time(
            events, personal_time_id=personal_time_id, title=title,
            start=start, end=end, description=description, location=location,
        )
    except ValidationError as exc:
        recorder.tools_invoked.append("update_personal_time")
        return f"PERSONAL_TIME_INVALID: {exc}"
    recorder.record_personal_time("update_personal_time", result)
    return _personal_time_status_line(result)


def _delete_personal_time_impl(events: EventsClient, recorder: SessionRecorder,
                               personal_time_id: str) -> str:
    try:
        result = sched.delete_personal_time(events, personal_time_id)
    except ValidationError as exc:
        recorder.tools_invoked.append("delete_personal_time")
        return f"PERSONAL_TIME_INVALID: {exc}"
    recorder.record_personal_time("delete_personal_time", result)
    return _personal_time_status_line(result)


def _session_group_value(session: Session, group_by: str):
    """Return the grouping key value(s) for a session. Multi-valued dimensions
    (topics/services) return a list so a session counts under each."""
    gb = (group_by or "").lower()
    if gb == "date":
        return [session.session_time.date] if session.session_time and session.session_time.date else []
    if gb == "venue":
        return [session.venue] if session.venue else []
    if gb == "room":
        return [session.room] if session.room else []
    if gb == "level":
        return [session.level] if session.level else []
    if gb in ("type", "sessiontype"):
        return [session.type] if session.type else []
    if gb in ("topic", "topics"):
        return list(session.topics or [])
    if gb in ("service", "services"):
        return list(session.services or [])
    return []


def _session_matches_filters(session: Session, filters: dict) -> bool:
    """True if the session matches ALL provided filters (case-insensitive
    substring for text dimensions; membership for multi-valued)."""
    if not filters:
        return True
    date = filters.get("date")
    if date and (not session.session_time or (session.session_time.date or "") != date):
        return False
    venue = filters.get("venue")
    if venue and venue.lower() not in (session.venue or "").lower():
        return False
    level = filters.get("level")
    if level and level.lower() not in (session.level or "").lower():
        return False
    stype = filters.get("type")
    if stype and stype.lower() not in (session.type or "").lower():
        return False
    topic = filters.get("topic")
    if topic and not any(topic.lower() in t.lower() for t in (session.topics or [])):
        return False
    service = filters.get("service")
    if service and not any(service.lower() in sv.lower() for sv in (session.services or [])):
        return False
    # Free-text "about X" keyword: match the SAME fields as search relevance
    # (title/type/level/services/topics/features/code) so counting and searching
    # agree. This fixes e.g. "how many Kiro sessions" (Kiro is in the title, not
    # the topics array) returning 0 while a search finds them.
    keyword = filters.get("keyword")
    if keyword:
        terms = [t for t in _tokenize(keyword) if len(t) > 2]
        if not _relevant(session, terms):
            return False
    return True


def _count_sessions_impl(events: EventsClient, recorder: SessionRecorder,
                         scope: str = "favorites", group_by: str = None,
                         filters: dict = None) -> str:
    """Count sessions in the user's FAVORITES (fast), their RESERVED sessions
    (fast), or the full CATALOG (slow; pages through everything). Optionally
    filter and/or group_by a dimension.

    scope:    "favorites" (default) | "reserved" | "catalog"
    group_by: date | venue | room | level | type | topic | service  (optional)
    filters:  {date, venue, level, type, topic, service}            (optional)
    """
    filters = filters or {}
    scope = (scope or "favorites").lower()

    # Do we actually need per-session details? Only when grouping or filtering
    # by a session field. A plain total never does -- so we avoid a GetSession
    # per favorite (which would be dozens of MCP calls and blow the timeout).
    needs_details = bool(group_by) or bool(filters)

    # Gather the session set for the requested scope.
    if scope == "favorites":
        if not needs_details:
            # FAST PATH: the count of favorite ids from GetSchedule is the answer.
            fav_ids = fav.get_favorite_ids(events)
            recorder.record("count_sessions", [])
            return f"COUNT: {len(fav_ids)} session(s) in your favorites."
        sessions = fav.get_favorites(events, max_details=config.MAX_FAVORITE_DETAILS)
        scope_label = "your favorites"
    elif scope == "reserved":
        # Mirrors the favorites fast-path: a plain total needs only the reserved
        # id list from GetSchedule (no GetSession per seat); grouping/filtering
        # resolves details via the shared bounded fan-out, same as favorites.
        if not needs_details:
            reserved_ids = resv.get_reserved_ids(events)
            recorder.record("count_sessions", [])
            return f"COUNT: {len(reserved_ids)} session(s) in your reserved sessions."
        sessions = resv.get_reserved_sessions(events, max_details=config.MAX_FAVORITE_DETAILS)
        scope_label = "your reserved sessions"
    else:
        sessions = []
        next_token = None
        pages = 0
        while pages < config.MAX_COUNT_PAGES:
            page, next_token = events.list_sessions(include_abstracts=False,
                                                    next_token=next_token)
            pages += 1
            sessions.extend(page)
            if not next_token:
                break
        scope_label = "the re:Invent 2026 catalog"

    # Apply filters.
    filtered = [s for s in sessions if _session_matches_filters(s, filters)]
    recorder.record("count_sessions", filtered[:config.MAX_SESSION_RESULT_LIMIT])

    filter_desc = ""
    if filters:
        parts = [f"{k}={v}" for k, v in filters.items() if v]
        if parts:
            filter_desc = " matching " + ", ".join(parts)

    # No grouping -> a single total.
    if not group_by:
        return f"COUNT: {len(filtered)} session(s) in {scope_label}{filter_desc}."

    # Grouped counts.
    from collections import Counter
    counter = Counter()
    ungrouped = 0
    for sess in filtered:
        values = _session_group_value(sess, group_by)
        if not values:
            ungrouped += 1
        for v in values:
            counter[v] += 1

    if not counter and not ungrouped:
        return f"COUNT: 0 session(s) in {scope_label}{filter_desc}."

    lines = [f"COUNT by {group_by} in {scope_label}{filter_desc} "
             f"(total {len(filtered)}):"]
    for value, n in counter.most_common(30):
        lines.append(f"  - {value}: {n}")
    if ungrouped:
        lines.append(f"  - (no {group_by}): {ungrouped}")
    return "\n".join(lines)


def _tokenize(text: str) -> list[str]:
    import re
    return [t.lower() for t in re.findall(r"[A-Za-z0-9]+", text or "")]


_STOPWORDS = {"find", "show", "me", "sessions", "session", "about", "on", "for",
              "the", "a", "an", "of", "related", "to", "with", "please", "any",
              "some", "and", "or", "get", "want", "like", "more",
              # Too generic to be discriminating: nearly every session mentions
              # these, so matching on them would flood irrelevant results.
              "amazon", "aws", "reinvent", "invent"}


def _relevant(session: Session, terms: list[str]) -> bool:
    if not terms:
        return True
    hay = " ".join([
        session.title or "", session.type or "", session.level or "",
        " ".join(session.services), " ".join(session.topics),
        " ".join(session.features), session.abbreviation or "",
    ]).lower()
    meaningful = [t for t in terms if t not in _STOPWORDS]
    if not meaningful:
        return True
    # Relevant if ANY meaningful term appears (broad match; the model then
    # interprets/explains). Keeps MCP-side filtering cheap and predictable.
    return any(t in hay for t in meaningful)


def _summarize_for_model(sessions: list[Session]) -> str:
    lines = []
    for i, s in enumerate(sessions, start=1):
        when = ""
        if s.session_time:
            when = f" | {s.session_time.date or ''} {s.session_time.time or ''}".rstrip()
        lines.append(
            f"{i}. [{s.abbreviation}] {s.title}"
            f" | type: {s.type or 'n/a'} | level: {s.level or 'n/a'}"
            f" | services: {', '.join(s.services) or 'n/a'}{when}"
        )
    return "FOUND {} session(s):\n{}".format(len(sessions), "\n".join(lines))


def _summarize_one_for_model(s: Session) -> str:
    parts = [f"[{s.abbreviation}] {s.title}"]
    if s.type:
        parts.append(f"Type: {s.type}")
    if s.level:
        parts.append(f"Level: {s.level}")
    if s.session_time:
        parts.append(f"When: {s.session_time.date or ''} {s.session_time.time or ''}".strip())
    if s.venue or s.room:
        parts.append(f"Where: {s.venue or ''} {s.room or ''}".strip())
    if s.speakers:
        parts.append(f"Speakers: {', '.join(s.speakers)}")
    if s.services:
        parts.append(f"Services: {', '.join(s.services)}")
    if s.abstract:
        parts.append(f"Abstract: {s.abstract}")
    return "\n".join(parts)


def build_agent(events: EventsClient, recorder: SessionRecorder,
                default_limit: Optional[int] = None,
                resolve_reference=None, history=None):
    """Create the Strands Agent with Bedrock + the two discovery tools.

    Imported lazily so unit tests can exercise the tool implementations and
    orchestration without the strands/bedrock packages installed.
    """
    from strands import Agent, tool
    from strands.models import BedrockModel

    limit = default_limit or config.DEFAULT_SESSION_RESULT_LIMIT

    @tool
    def search_sessions(query: str) -> str:
        """Search AWS re:Invent 2026 sessions by topic, AWS service, level,
        session type, or day. Returns a numbered list of matching sessions."""
        return _search_sessions_impl(events, recorder, query, limit)

    @tool
    def get_session_details(reference: str) -> str:
        """Get full details for a single re:Invent 2026 session, identified by
        its session code (e.g. 'ANT301'). Pass the resolved session id/code."""
        target = _resolve(reference)
        return _get_details_impl(events, recorder, target)

    @tool
    def favorite_session(reference: str) -> str:
        """Add ONE session to the user's AWS Events favorites. `reference` may be
        a session code, sessionId, or an ordinal like '#2'."""
        target = _resolve(reference)
        return _favorite_impl(events, recorder, target, session_code=reference)

    @tool
    def unfavorite_session(reference: str) -> str:
        """Remove ONE session from the user's AWS Events favorites."""
        target = _resolve(reference)
        return _unfavorite_impl(events, recorder, target, session_code=reference)

    @tool
    def show_favorites() -> str:
        """List the user's current favorite sessions from AWS Events."""
        return _show_favorites_impl(events, recorder)

    @tool
    def show_schedule(date: str = None) -> str:
        """Show the user's re:Invent 2026 schedule (reserved sessions,
        favorites, and personal-time blocks). Pass `date` as a Pacific
        'YYYY-MM-DD' (the event runs 2026-11-30 through 2026-12-04) to show ONLY
        that day; omit it for the full schedule.

        The individual items are rendered to the user as CARDS automatically.
        This tool returns ONLY counts to you -- do NOT list or describe the
        individual sessions in your reply (you don't have their details here and
        must not invent them). Write a one-sentence summary of the counts and,
        if helpful, offer to check conflicts. To find time clashes, call
        find_schedule_conflicts -- never guess overlaps yourself."""
        return _show_schedule_impl(events, recorder, date=date)

    @tool
    def find_schedule_conflicts(date: str = None) -> str:
        """Find overlapping (time-clashing) items on the user's schedule. The
        overlap is computed server-side and returned as explicit conflicting
        pairs -- report exactly what it returns; do NOT infer conflicts
        yourself. Pass `date` ('YYYY-MM-DD', 2026-11-30..2026-12-04) to check a
        single day, or omit it to check the whole schedule. Use this whenever
        the user asks about conflicts/overlaps/double-booking."""
        return _find_conflicts_impl(events, recorder, date=date)

    @tool
    def add_personal_time(title: str, start: str, end: str,
                          description: str = None, location: str = None) -> str:
        """Add ONE personal-time block to the user's schedule (e.g. a keynote not
        in the catalog, lunch, travel, or a meeting).

        title: required, up to 200 characters.
        start/end: required, Pacific event-local time as 'YYYY-MM-DDTHH:MM'
                   (e.g. '2026-12-01T08:30'). The backend converts them to the
                   required UTC form and validates (seconds 00, end strictly
                   after start, whole 5-minute duration) BEFORE calling AWS
                   Events; if invalid the tool reports the specific problem.
        description/location: optional (up to 1000 / 200 characters).
        Only confirm the block was added if the tool reports success."""
        return _add_personal_time_impl(events, recorder, title, start, end,
                                       description=description, location=location)

    @tool
    def update_personal_time(reference: str, title: str, start: str, end: str,
                             description: str = None, location: str = None) -> str:
        """Edit an existing personal-time block. `reference` is its id or an
        ordinal like '#2'. FULL replacement: pass every field; any optional field
        left empty clears its prior value. start/end are Pacific event-local
        'YYYY-MM-DDTHH:MM' and are validated before the call (same rules as
        add_personal_time). Only confirm the edit if the tool reports success; a
        reference that no longer exists reports not-found and changes nothing."""
        target = _resolve(reference)
        return _update_personal_time_impl(events, recorder, target, title,
                                          start, end, description=description,
                                          location=location)

    @tool
    def delete_personal_time(reference: str) -> str:
        """Remove a personal-time block from the user's schedule. `reference` is
        its id or an ordinal like '#2'. Deletion is idempotent, so a block that
        is already gone still reports success."""
        target = _resolve(reference)
        return _delete_personal_time_impl(events, recorder, target)

    @tool
    def count_sessions(scope: str = "favorites", group_by: str = None,
                       keyword: str = None, date: str = None, venue: str = None,
                       topic: str = None, service: str = None, level: str = None,
                       session_type: str = None) -> str:
        """Count sessions, optionally grouped and/or filtered. Use this for
        "how many" questions.

        scope: "favorites" (the user's favorites, fast), "reserved" (the
               user's reserved seats, fast), or "catalog" (ALL re:Invent 2026
               sessions, slower - only when the user clearly asks about the
               whole event).
        group_by: one of date, venue, room, level, type, topic, service - to get
               a breakdown (e.g. favorites by date, reserved by venue). Omit for
               a single total.
        keyword: free-text "about X" topic (e.g. "Kiro", "Bedrock", "serverless").
               Use THIS for "how many sessions about <thing>" - it matches the
               same fields as search (title, service, topic, level, code), so it
               agrees with search results. Do NOT put a free-text topic in the
               `topic` filter (that only checks the structured topics field).
        date/venue/service/level/session_type: optional exact-ish filters.

        Examples: "how many favorites do I have" -> scope=favorites;
        "how many favorites on 2026-12-02" -> scope=favorites, date=2026-12-02;
        "my favorites by venue" -> scope=favorites, group_by=venue;
        "how many sessions have I reserved" -> scope=reserved;
        "my reserved sessions by venue" -> scope=reserved, group_by=venue;
        "how many reserved on 2026-12-02" -> scope=reserved, date=2026-12-02;
        "how many Kiro sessions" -> scope=catalog, keyword=Kiro;
        "how many Bedrock sessions on Dec 3" -> scope=catalog, keyword=Bedrock, date=2026-12-03;
        "how many sessions at MGM Grand" -> scope=catalog, venue=MGM Grand."""
        filters = {"keyword": keyword, "date": date, "venue": venue,
                   "topic": topic, "service": service, "level": level,
                   "type": session_type}
        filters = {k: v for k, v in filters.items() if v}
        return _count_sessions_impl(events, recorder, scope=scope,
                                    group_by=group_by, filters=filters)

    @tool
    def reserve_session(reference: str) -> str:
        """Reserve a seat in ONE re:Invent 2026 session for the user via AWS
        Events. `reference` may be a session code, sessionId, or an ordinal like
        '#2'. Reserving a seat is DIFFERENT from favoriting: only say the seat
        was reserved if the tool reports success."""
        target = _resolve(reference)
        return _reserve_impl(events, recorder, target, session_code=reference)

    @tool
    def cancel_reservation(reference: str) -> str:
        """Cancel the user's reservation for ONE session via AWS Events.
        `reference` may be a session code, sessionId, or an ordinal like '#2'.
        Cancellation is idempotent: a session that is not currently reserved
        still reports success."""
        target = _resolve(reference)
        return _cancel_reservation_impl(events, recorder, target, session_code=reference)

    def _resolve(reference: str) -> str:
        if resolve_reference:
            resolved = resolve_reference(reference)
            if resolved:
                return resolved
        return reference

    model = BedrockModel(model_id=config.BEDROCK_MODEL_ID,
                         region_name=config.BEDROCK_REGION)
    tools = [search_sessions, get_session_details,
             favorite_session, unfavorite_session, show_favorites,
             show_schedule, find_schedule_conflicts,
             add_personal_time, update_personal_time,
             delete_personal_time, count_sessions]
    # Reservation tools are exposed ONLY while the phased capability is enabled.
    # While disabled the agent has no reservation tool at all and the
    # SYSTEM_PROMPT already tells it reservation is unavailable (Req 21.10, 23.8).
    if config.RESERVATIONS_ENABLED:
        tools.extend([reserve_session, cancel_reservation])
    agent_kwargs = {
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
    if history:
        # Seed prior turns so the agent remembers earlier context (multi-turn).
        agent_kwargs["messages"] = history
    agent = Agent(**agent_kwargs)
    return agent


def run_agent_turn(agent, message: str, request_id: Optional[str] = None) -> str:
    """Run one agent turn under the turn-timeout budget (Property 9).

    Returns the assistant's natural-language text. Raises McpTimeoutError if the
    budget is exceeded so the caller can return a controlled timeout state.
    """
    def run() -> str:
        result = agent(message)
        return _extract_text(result)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(run).result(timeout=config.AGENT_TURN_TIMEOUT)
    except concurrent.futures.TimeoutError:
        raise McpTimeoutError("agent turn exceeded time budget") from None


def _extract_text(result) -> str:
    """Pull assistant text out of a Strands AgentResult (version-tolerant)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # AgentResult commonly stringifies to the final assistant text.
    for attr in ("message", "output", "text"):
        val = getattr(result, attr, None)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            content = val.get("content")
            if isinstance(content, list):
                texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("text")]
                if texts:
                    return "\n".join(texts)
    return str(result)
