"""Per-user AWS Events MCP client (Streamable HTTP + bearer token).

Design reference: design.md "MCPClientFactory" and "Lambda + MCP Lifecycle".

CRITICAL invariants (Phase 2 / Properties 5 & 6):
  * A FRESH client is created per invocation, bound to THIS user's bearer
    token. No token or client is cached in module globals -> no cross-user
    leakage under concurrency (Property 5).
  * On a 401/expired token, callers refresh once and retry once (Property 6);
    this module surfaces a typed McpAuthExpired so the caller can decide.

This phase uses only the READ-ONLY operations (list_sessions, get_session,
get_schedule) plus the registration probe. Favorite wrappers are defined for
later phases but not invoked by the Phase-1/2 debug endpoint.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
from typing import Optional

import config
from errors import (
    EventAccessDeniedError,
    McpError,
    McpTimeoutError,
    McpUnavailableError,
)
from models import Session


class McpAuthExpired(Exception):
    """Raised when the MCP server rejects the bearer token (401/expired)."""


def _looks_like_auth_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(t in text for t in ("401", "unauthorized", "invalid_token", "expired token", "forbidden token"))


def _looks_like_access_denied(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(t in text for t in ("403", "access denied", "not registered", "not authorized", "forbidden"))


def _looks_like_not_found(exc: Exception) -> bool:
    """Heuristic: did the tool fail because the target no longer exists?

    Used by delete_personal_time to treat an already-removed entry as an
    idempotent success rather than a failure (Req 20.3).
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(t in text for t in (
        "404", "not found", "notfound", "does not exist", "already removed",
        "already deleted", "no such",
    ))


def _body_says_not_found(body) -> bool:
    """A tool can surface a 404 as a NON-errored result whose JSON body carries
    httpStatus 404 / a not-found message (e.g. the live CancelReservation:
    {"message": "No reservation or favorite was found for the requested
    session", "httpStatus": 404}). In that case no exception is raised, so the
    exception-based _looks_like_not_found never sees it; callers that treat an
    absent target as idempotent success must also inspect the returned body.
    """
    if not isinstance(body, dict):
        return False
    status = body.get("httpStatus") or body.get("statusCode")
    try:
        if int(status) == 404:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(body.get("message") or "").lower()
    return any(t in msg for t in (
        "not found", "no such", "does not exist",
        "was found for the requested",  # live 404 phrasing ("...no ... was found...")
        "no reservation", "no favorite",
    ))


def _schedule_array(schedule: dict, keys: tuple[str, ...]) -> list:
    """Return the first present list-valued field among ``keys`` (empty list if
    none present / non-list), so get_schedule() always exposes a stable array."""
    for key in keys:
        value = schedule.get(key)
        if isinstance(value, list):
            return value
    return []


def make_mcp_client(access_token: str):
    """Create a fresh Strands MCPClient bound to a single user's bearer token.

    Imported lazily so unit tests (which inject a fake client) don't require
    the strands/mcp packages.

    Handles both MCP SDK APIs:
      * mcp >= 2.x: `streamable_http_client(url, http_client=<httpx client with
        headers>)` -- headers are set on the httpx client, not the factory.
      * older mcp: `streamablehttp_client(url=..., headers={...})`.
    """
    from strands.tools.mcp import MCPClient
    import mcp.client.streamable_http as shttp

    bearer = {"Authorization": f"Bearer {access_token}"}
    url = config.MCP_ENDPOINT

    if hasattr(shttp, "streamable_http_client"):
        # mcp >= 2.x: build an httpx client carrying the auth header.
        def factory():
            http_client = shttp.create_mcp_http_client(headers=bearer)
            return shttp.streamable_http_client(url, http_client=http_client)
        return MCPClient(factory)

    # Fallback for older mcp SDKs that accept headers on the factory.
    legacy = shttp.streamablehttp_client
    return MCPClient(lambda: legacy(url=url, headers=bearer))


def _normalize(name: str) -> str:
    """Lowercase, strip non-alphanumerics so ListSessions == list_sessions == listsessions."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# Logical operation -> candidate server names (any casing/separator). The
# resolver matches the server's ACTUAL tool list against these by normalization,
# so we don't depend on a single guessed spelling.
_TOOL_CANDIDATES = {
    "list_sessions": ["ListSessions", "listSessions", "list_sessions", "searchSessions", "sessions"],
    "get_session": ["GetSession", "getSession", "get_session", "session"],
    "get_schedule": ["GetSchedule", "getSchedule", "get_schedule", "schedule", "getMySchedule", "myschedule"],
    "associate_favorites": ["AssociateFavorites", "associateFavorites", "associate_favorites",
                             "addFavorites", "favorite", "favoriteSessions", "addFavorite"],
    "disassociate_favorite": ["DisassociateFavorite", "disassociateFavorite", "disassociate_favorite",
                               "removeFavorite", "unfavorite", "deleteFavorite"],
    "create_personal_time": ["CreatePersonalTime", "createPersonalTime", "create_personal_time"],
    "update_personal_time": ["UpdatePersonalTime", "updatePersonalTime", "update_personal_time"],
    "delete_personal_time": ["DeletePersonalTime", "deletePersonalTime", "delete_personal_time"],
    "reserve_sessions": ["ReserveSessions", "reserveSessions", "reserve_sessions", "reserve"],
    "cancel_reservation": ["CancelReservation", "cancelReservation", "cancel_reservation", "cancelReservations"],
}

# Cache of {normalized -> actual server tool name}, keyed nothing (server is stable
# within a process); refreshed lazily per EventsClient if empty.
_server_tool_names: dict[str, str] = {}


class EventsClient:
    """Typed, read-focused wrappers over the AWS Events MCP tools.

    `client_factory(access_token) -> context-manager MCP client` is injectable
    so tests can supply a fake. In production it defaults to make_mcp_client.

    Every public method opens a fresh client for the call (per-invocation
    lifecycle) and runs it under a timeout to respect the API Gateway budget.
    """

    EVENT_ID = config.EVENT_ID

    def __init__(self, access_token: str, client_factory=None, call_timeout: Optional[int] = None):
        self._access_token = access_token
        self._factory = client_factory or make_mcp_client
        self._timeout = call_timeout or config.MCP_CALL_TIMEOUT
        self._tool_map: dict[str, str] = {}

    def _ensure_tool_map(self) -> None:
        """Populate {normalized -> actual} from the server's real tool list.

        Tool names are server-global and non-sensitive, so they are cached at
        MODULE level (`_server_tool_names`) and reused across invocations / warm
        Lambda containers. This avoids an extra `list_tools()` MCP round trip on
        every request. The per-user bearer token is NEVER cached — only names.
        """
        if self._tool_map:
            return
        if _server_tool_names:
            self._tool_map = dict(_server_tool_names)
            return
        try:
            names = self.list_tools()
        except Exception:
            names = []
        self._tool_map = {_normalize(n): n for n in names}
        if self._tool_map:
            _server_tool_names.update(self._tool_map)

    def _resolve_tool(self, logical: str) -> str:
        """Map a logical op name to the server's actual tool name.

        Falls back to the first candidate (PascalCase) if discovery is
        unavailable, so behavior is unchanged when tests inject fakes.
        """
        self._ensure_tool_map()
        for cand in _TOOL_CANDIDATES.get(logical, [logical]):
            actual = self._tool_map.get(_normalize(cand))
            if actual:
                return actual
        # Nothing matched exactly; if we discovered tools, try a contains-match.
        if self._tool_map:
            key = logical.replace("_", "")
            for norm, actual in self._tool_map.items():
                if key in norm or norm in key:
                    return actual
        return _TOOL_CANDIDATES.get(logical, [logical])[0]

    # ---- low-level tool invocation ----
    def _call_tool(self, tool_name: str, arguments: dict) -> dict:
        def run() -> dict:
            client = self._factory(self._access_token)
            with client:
                result = client.call_tool_sync(
                    tool_use_id=f"{tool_name}-call",
                    name=tool_name,
                    arguments=arguments,
                )
                _raise_if_tool_error(tool_name, result)
                return _extract_tool_json(result)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(run)
                return future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            raise McpTimeoutError(f"{tool_name} timed out") from None
        except (McpAuthExpired, EventAccessDeniedError, McpError,
                McpUnavailableError, McpTimeoutError):
            raise
        except Exception as exc:
            if _looks_like_auth_error(exc):
                raise McpAuthExpired(str(exc)) from exc
            if _looks_like_access_denied(exc):
                raise EventAccessDeniedError() from exc
            text = f"{exc}".lower()
            if any(t in text for t in ("connection", "timeout", "unavailable", "503")):
                raise McpUnavailableError() from exc
            # Preserve the real cause; include a sanitized detail when debugging.
            raise McpError(_detail(tool_name, exc)) from exc

    def list_tools(self) -> list[str]:
        def run() -> list[str]:
            client = self._factory(self._access_token)
            with client:
                tools = client.list_tools_sync()
                names = []
                for t in tools:
                    name = getattr(t, "tool_name", None) or getattr(t, "name", None)
                    if name:
                        names.append(name)
                return names

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(run).result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            raise McpTimeoutError("list_tools timed out") from None
        except Exception as exc:
            if _looks_like_auth_error(exc):
                raise McpAuthExpired(str(exc)) from None
            raise McpUnavailableError() from None

    # ---- read-only operations (Phase 2) ----
    def list_sessions(self, include_abstracts: bool = False,
                       locale: Optional[str] = None,
                       next_token: Optional[str] = None) -> tuple[list[Session], Optional[str]]:
        args: dict = {"eventId": self.EVENT_ID, "includeAbstracts": include_abstracts}
        if locale:
            args["locale"] = locale
        if next_token:
            args["nextToken"] = next_token
        data = self._call_tool(self._resolve_tool("list_sessions"), args)
        items = [Session.from_mcp(i) for i in data.get("items", [])]
        return items, data.get("nextToken")

    def get_session(self, session_id: str, locale: Optional[str] = None) -> Optional[Session]:
        args: dict = {"eventId": self.EVENT_ID, "sessionId": session_id}
        if locale:
            args["locale"] = locale
        data = self._call_tool(self._resolve_tool("get_session"), args)
        sess = data.get("session")
        return Session.from_mcp(sess) if sess else None

    def get_schedule(self) -> dict:
        """Return the owner's schedule as a dict exposing the three AWS Events
        arrays the Schedule_View consumes:

          * ``favorites``    -> favorite sessionIds (already consumed by Phase 4)
          * ``reserved``     -> reserved sessionIds / reserved-session entries
          * ``personalTime`` -> personal-time entries (incl. personalTimeId)

        The raw GetSchedule payload may nest these under a ``schedule`` key and
        may spell the reserved/personal-time arrays with different casings; this
        normalizes so every array is always present (empty list when absent),
        and preserves any additional fields the server returned.
        """
        data = self._call_tool(self._resolve_tool("get_schedule"), {"eventId": self.EVENT_ID})
        # Some AWS Events tools wrap the payload in a {"result": {...}} envelope;
        # peel it before reading the schedule arrays (defensive -- a no-op when
        # the key is absent, as it is for the GetSchedule shape we see today).
        if isinstance(data, dict) and set(data.keys()) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
        schedule = data.get("schedule", data)
        if not isinstance(schedule, dict):
            return {"favorites": [], "reserved": [], "personalTime": []}
        out = dict(schedule)
        out["favorites"] = _schedule_array(schedule, ("favorites", "favorite", "favoriteSessions"))
        out["reserved"] = _schedule_array(schedule, ("reserved", "reservedSessions", "reservations"))
        out["personalTime"] = _schedule_array(schedule, ("personalTime", "personalTimes", "personal_time"))
        return out

    # ---- favorite wrappers (defined for later phases; not used this phase) ----
    def associate_favorites(self, session_ids: list[str]) -> dict:
        return self._call_tool(
            self._resolve_tool("associate_favorites"),
            {"eventId": self.EVENT_ID, "sessionIds": session_ids},
        )

    def disassociate_favorite(self, session_id: str) -> dict:
        return self._call_tool(
            self._resolve_tool("disassociate_favorite"),
            {"eventId": self.EVENT_ID, "sessionId": session_id},
        )

    # ---- personal-time wrappers (Phase 8) ----
    def create_personal_time(self, title: str, start: str, end: str,
                             description: Optional[str] = None,
                             location: Optional[str] = None) -> dict:
        """CreatePersonalTime(eventId, title, startDateTime, endDateTime,
        description, location).

        ``start``/``end`` are already in the UTC contract form
        ``YYYY-MM-DDTHH:MM:SS`` (seconds == ``00``); Pacific->UTC conversion and
        validation happen in the handler/schedule layer before this call.

        The live AWS Events API requires ``description`` and ``location`` to be
        PRESENT **and non-empty** (schema: required + minLength 1). Omitting a
        field -> "Missing required field(s)"; sending "" -> "minLength ... at
        least 1 characters". So when the user supplies no value we fall back to a
        sensible NON-EMPTY default (the block's own title for description, a
        generic "Personal time" for location) rather than omit or send "". These
        defaults describe the user's own block; no event data is fabricated.
        Returns the created entry (including its ``personalTimeId``).
        """
        args: dict = {
            "eventId": self.EVENT_ID,
            "title": title,
            "startDateTime": start,
            "endDateTime": end,
            "description": (description or "").strip() or (title or "Personal time"),
            "location": (location or "").strip() or "Personal time",
        }
        return self._call_tool(self._resolve_tool("create_personal_time"), args)

    def update_personal_time(self, personal_time_id: str, title: str,
                             start: str, end: str,
                             description: Optional[str] = None,
                             location: Optional[str] = None) -> dict:
        """UpdatePersonalTime — FULL replacement semantics.

        ALL fields are ALWAYS sent (full replacement). The live API requires
        ``description`` and ``location`` to be non-empty (minLength 1), so an
        absent/blank optional falls back to a NON-EMPTY default (the title for
        description, "Personal time" for location) rather than ""; this both
        satisfies the schema and avoids a confusing blank on the server.
        ``start``/``end`` are already UTC contract form. Returns the updated
        entry.
        """
        args: dict = {
            "eventId": self.EVENT_ID,
            "personalTimeId": personal_time_id,
            "title": title,
            "startDateTime": start,
            "endDateTime": end,
            "description": (description or "").strip() or (title or "Personal time"),
            "location": (location or "").strip() or "Personal time",
        }
        return self._call_tool(self._resolve_tool("update_personal_time"), args)

    def delete_personal_time(self, personal_time_id: str) -> dict:
        """DeletePersonalTime — idempotent.

        A not-found / already-removed outcome is the desired end state, so it is
        reported as success rather than a false failure. Any other error from
        the tool propagates as a typed McpError (handled upstream).
        """
        try:
            return self._call_tool(
                self._resolve_tool("delete_personal_time"),
                {"eventId": self.EVENT_ID, "personalTimeId": personal_time_id},
            )
        except (EventAccessDeniedError, McpAuthExpired, McpTimeoutError,
                McpUnavailableError):
            # auth / availability problems are real failures, not idempotency.
            raise
        except McpError as exc:
            if _looks_like_not_found(exc):
                # already removed -> idempotent success (Req 20.3).
                return {"status": "success", "alreadyRemoved": True,
                        "personalTimeId": personal_time_id}
            raise

    # ---- reservation wrappers (Phase 11 — PHASED, gated on MCP availability) ----
    # These mirror the favorite wrappers exactly: the AWS Events ReserveSessions /
    # CancelReservation operations may not be live yet, so these are built behind
    # config.RESERVATIONS_ENABLED and are not invoked while the flag is false.
    def reserve_sessions(self, session_ids: list[str]) -> dict:
        """ReserveSessions(eventId, sessionIds) — mirrors associate_favorites.

        Sends ``eventId`` + ``sessionIds`` and returns the RAW tool dict so the
        service layer can read ``failed[]`` to derive per-session outcomes
        (success iff a sessionId is absent from ``failed``; already-reserved vs
        other-failure disambiguated upstream) exactly as the favorites path does
        (Req 21.1, 21.2, 21.3).
        """
        return self._call_tool(
            self._resolve_tool("reserve_sessions"),
            {"eventId": self.EVENT_ID, "sessionIds": session_ids},
        )

    def cancel_reservation(self, session_id: str) -> dict:
        """CancelReservation(eventId, sessionId) — mirrors disassociate_favorite.

        Sends ``eventId`` + ``sessionId``. A not-found / not-currently-reserved
        outcome is the desired end state, so it is reported as an idempotent
        success rather than a false failure (consistent with the disassociate
        404 pattern); any other error propagates as a typed error for the
        service layer to leave reservation state unchanged (Req 21.5, 21.9).
        """
        try:
            body = self._call_tool(
                self._resolve_tool("cancel_reservation"),
                {"eventId": self.EVENT_ID, "sessionId": session_id},
            )
        except (EventAccessDeniedError, McpAuthExpired, McpTimeoutError,
                McpUnavailableError):
            # auth / availability problems are real failures, not idempotency.
            raise
        except McpError as exc:
            if _looks_like_not_found(exc):
                # not currently reserved -> idempotent success (Req 21.9).
                return {"status": "success", "notReserved": True,
                        "sessionId": session_id}
            raise
        # The server may ALSO surface "not currently reserved" as a non-errored
        # body carrying httpStatus 404 (observed live), in which case no McpError
        # was raised. Treat that the same idempotent way (Req 21.9).
        if _body_says_not_found(body):
            return {"status": "success", "notReserved": True,
                    "sessionId": session_id}
        return body


import os as _os

_MCP_DEBUG = _os.environ.get("MCP_DEBUG", "").lower() in ("1", "true", "yes")


def _detail(tool_name: str, exc: Exception) -> str:
    """Safe error string. Token values never appear in MCP exceptions, but we
    strip any Authorization-looking substrings defensively."""
    base = f"{tool_name} failed"
    if _MCP_DEBUG:
        msg = f"{type(exc).__name__}: {exc}"
        # defensive: never echo a bearer value even in debug
        import re as _re
        msg = _re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", msg)
        return f"{base} :: {msg}"
    return base


def _raise_if_tool_error(tool_name: str, result) -> None:
    """Strands/MCP may return a result object with an error status instead of
    raising. Detect that and raise a typed error with a sanitized message."""
    status = None
    is_error = None
    content = None
    if isinstance(result, dict):
        status = result.get("status")
        is_error = result.get("isError")
        content = result.get("content")
    else:
        status = getattr(result, "status", None)
        is_error = getattr(result, "isError", None)
        content = getattr(result, "content", None)

    errored = (status in ("error", "failed")) or (is_error is True)
    if not errored:
        return

    # Pull a text hint from the content blocks (no tokens present here).
    hint = ""
    if isinstance(content, list):
        for b in content:
            t = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            if t:
                hint = str(t)[:300]
                break
    low = hint.lower()
    if any(k in low for k in ("401", "unauthorized", "invalid_token", "expired")):
        raise McpAuthExpired(hint or "unauthorized")
    if any(k in low for k in ("403", "not registered", "not authorized", "access denied")):
        raise EventAccessDeniedError()
    # Preserve a sanitized NOT-FOUND marker in the raised message regardless of
    # MCP_DEBUG, so callers that treat an absent target as an idempotent success
    # (cancel_reservation, disassociate_favorite, delete_personal_time via
    # _looks_like_not_found) classify it correctly in production. Without this,
    # the hint -- the only text carrying "404"/"not found" -- is dropped unless
    # debugging is on, and e.g. the live CancelReservation 404 ("No reservation
    # or favorite was found for the requested session", httpStatus 404) is
    # mis-reported as a hard failure instead of "not currently reserved".
    not_found = any(k in low for k in (
        "404", "not found", "notfound", "does not exist", "no such",
        "was found for the requested",  # live GetSchedule/Cancel 404 phrasing
        "no reservation", "no favorite",
    ))
    detail = f" :: {hint}" if (_MCP_DEBUG and hint) else ""
    if not_found:
        # A stable, non-sensitive token _looks_like_not_found matches on.
        detail = (detail or " :: not found") if not _MCP_DEBUG else detail + " (not found)"
    raise McpError(f"{tool_name} tool error" + detail)


def _extract_tool_json(result) -> dict:
    """Normalize a Strands MCP call result into a dict.

    Strands returns a result dict with a 'content' list of blocks; JSON tool
    output is typically a text block containing a JSON string.
    """
    import json

    # Result may already be a plain dict (fakes in tests).
    if isinstance(result, dict) and "content" not in result:
        return result

    content = None
    if isinstance(result, dict):
        content = result.get("content")
    else:
        content = getattr(result, "content", None)

    if content is None:
        return {}

    for block in content:
        text = None
        if isinstance(block, dict):
            text = block.get("text")
            if text is None and block.get("json") is not None:
                return block["json"]
        else:
            text = getattr(block, "text", None)
        if text:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                return json.loads(text)
    return {}


def determine_registration(events: EventsClient) -> str:
    """Registration probe = source of truth (design.md determineRegistration).

    Returns one of: 'authorized' | 'not_registered' | 'error'.
    Never infers authorization from login alone.
    """
    from models import AuthState

    try:
        events.get_schedule()
        return AuthState.AUTHORIZED.value
    except EventAccessDeniedError:
        return AuthState.NOT_REGISTERED.value
    except McpAuthExpired:
        # token problem, not a registration signal
        return AuthState.ERROR.value
    except (McpUnavailableError, McpTimeoutError, McpError):
        return AuthState.ERROR.value
    except Exception:
        return AuthState.ERROR.value
