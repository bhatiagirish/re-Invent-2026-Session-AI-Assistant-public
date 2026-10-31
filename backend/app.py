"""Lambda handler + routing for the re:Invent Session Assistant (Phase 1 + 2).

Endpoints implemented this phase:
  GET  /auth/login     -> 302 to Cognito authorize (PKCE, state, nonce)
  GET  /auth/callback  -> exchange code, verify id token, create session,
                          registration probe, set HttpOnly cookie, 302 to SPA
  GET  /auth/status    -> safe {authenticated, eventAccess} (no OAuth internals)
  POST /logout         -> revoke refresh token, delete session, clear cookie
  GET  /debug/events   -> authenticated MCP connectivity test (read-only)

Chat / agent / favorites endpoints are intentionally NOT implemented here
(later phases). The browser never receives any token; only the opaque
appSessionId travels in an HttpOnly cookie.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from http import cookies as http_cookies
from typing import Optional
from urllib.parse import parse_qs

import config
from auth import (
    AuthProvider,
    code_challenge_s256,
    generate_code_verifier,
    generate_nonce,
    generate_state,
)
from errors import AppError, AuthFailedError, AuthRequiredError, InvalidRequestError
from logging_utils import Timer, log_event, safe_text
from mcp_client import EventsClient, McpAuthExpired, determine_registration
from models import ActionResult, AuthState, ChatRequest, ChatResponse, Session, TokenBundle
from session_manager import SessionManager
import agent as agent_mod
import catalog_cache
import favorites as fav
import reservations as resv
import schedule as sched
import session_context as ctx
from errors import McpError, McpTimeoutError, McpUnavailableError, EventAccessDeniedError


# Reusable, stateless helpers are fine at module scope; user tokens are NOT.
_auth = AuthProvider()


def _session_manager() -> SessionManager:
    return SessionManager(kms_key_id=os.environ.get("KMS_KEY_ID"), auth_provider=_auth)


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _method_path(event: dict) -> tuple[str, str]:
    rc = event.get("requestContext", {})
    http = rc.get("http", {})
    method = http.get("method") or event.get("httpMethod", "GET")
    path = http.get("path") or event.get("rawPath") or event.get("path", "/")
    return method.upper(), path


def _cors_headers() -> dict:
    origin = config.FRONTEND_ORIGIN
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Vary"] = "Origin"
    return headers


def _json_response(status: int, body: dict, extra_headers: Optional[dict] = None,
                   cookies: Optional[list[str]] = None) -> dict:
    resp = {
        "statusCode": status,
        "headers": {**_cors_headers(), **(extra_headers or {})},
        "body": json.dumps(body),
    }
    if cookies:
        resp["cookies"] = cookies  # API Gateway HTTP API (payload v2) multi-cookie
    return resp


def _redirect(location: str, cookies: Optional[list[str]] = None) -> dict:
    resp = {
        "statusCode": 302,
        "headers": {"Location": location},
        "body": "",
    }
    if cookies:
        resp["cookies"] = cookies
    return resp


def _get_cookie(event: dict, name: str) -> Optional[str]:
    # HTTP API v2 provides a "cookies" list; REST provides a Cookie header.
    raw_cookies = event.get("cookies")
    if raw_cookies:
        for c in raw_cookies:
            if "=" in c:
                k, _, v = c.partition("=")
                if k.strip() == name:
                    return v.strip()
    header = (event.get("headers") or {})
    cookie_header = header.get("cookie") or header.get("Cookie")
    if cookie_header:
        jar = http_cookies.SimpleCookie()
        jar.load(cookie_header)
        if name in jar:
            return jar[name].value
    return None


def _set_cookie(value: str, max_age: int) -> str:
    parts = [
        f"{config.COOKIE_NAME}={value}",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
        "Path=/",
        f"Max-Age={max_age}",
    ]
    if config.COOKIE_DOMAIN:
        parts.append(f"Domain={config.COOKIE_DOMAIN}")
    return "; ".join(parts)


def _clear_cookie() -> str:
    parts = [
        f"{config.COOKIE_NAME}=",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
        "Path=/",
        "Max-Age=0",
    ]
    if config.COOKIE_DOMAIN:
        parts.append(f"Domain={config.COOKIE_DOMAIN}")
    return "; ".join(parts)


def _query(event: dict) -> dict:
    q = event.get("queryStringParameters") or {}
    if q:
        return q
    raw = event.get("rawQueryString")
    if raw:
        return {k: v[0] for k, v in parse_qs(raw).items()}
    return {}


def _request_id(event: dict) -> Optional[str]:
    return (event.get("requestContext", {}) or {}).get("requestId")


# ── Handlers ─────────────────────────────────────────────────────────────────
def auth_login(event: dict) -> dict:
    """AWS Events uses a loopback-only (native) OAuth client, so a hosted
    browser redirect cannot complete (it would fail with redirect_mismatch).
    For this single-user personal app, the token is obtained LOCALLY via the
    native loopback flow and pushed to the backend (see POST /admin/connect and
    docs/spikes/connect.py). This endpoint just explains that.
    """
    return _json_response(200, {
        "connectMode": "native-loopback",
        "message": ("This is a single-user app. Connect by running the local "
                    "connect script (docs/spikes/connect.py), which signs in with "
                    "your AWS Builder ID via the AWS Events loopback flow and "
                    "links this app to your identity."),
    })


def _admin_key() -> Optional[str]:
    """Load the admin key from Secrets Manager (prod) or env (local dev)."""
    if config.ADMIN_KEY_SECRET_ARN:
        try:
            import boto3
            sm_client = boto3.client("secretsmanager", region_name=config.AWS_REGION)
            raw = sm_client.get_secret_value(SecretId=config.ADMIN_KEY_SECRET_ARN)["SecretString"]
            try:
                return json.loads(raw).get("admin_key", raw)
            except json.JSONDecodeError:
                return raw
        except Exception:
            return None
    return config.ADMIN_KEY_DEV or None


def admin_connect(event: dict) -> dict:
    """Single-user admin endpoint: accept a token obtained locally via the AWS
    Events native loopback flow, store it SERVER-SIDE, run the registration
    probe, and return the app session cookie. The browser never calls this;
    only the local connect script does, authenticated by the admin key.

    Body: {"accessToken","refreshToken"?,"expiresIn"?,"sub"?,"email"?}
    Header: X-Admin-Key: <key>
    """
    sm = _session_manager()
    expected = _admin_key()
    provided = (event.get("headers") or {}).get("x-admin-key") or (event.get("headers") or {}).get("X-Admin-Key")
    if not expected or not provided or not hmac.compare_digest(str(provided), str(expected)):
        raise AuthRequiredError()

    body = _parse_json_body(event)
    access_token = body.get("accessToken")
    if not access_token or not isinstance(access_token, str):
        raise InvalidRequestError("accessToken is required")
    expires_in = int(body.get("expiresIn", 3600))
    tokens = TokenBundle(
        access_token=access_token,
        refresh_token=body.get("refreshToken"),
        id_token_claims={"sub": body.get("sub"), "email": body.get("email")},
        access_expires_at=int(time.time()) + expires_in,
    )

    app_session_id = sm.create_session(tokens, AuthState.AUTHENTICATED)
    try:
        events = EventsClient(sm.get_valid_access_token(app_session_id))
        state_value = determine_registration(events)
    except Exception:
        state_value = AuthState.ERROR.value
    sm.set_auth_state(app_session_id, AuthState(state_value))

    # Issue a short-lived, single-use claim code the BROWSER exchanges for the
    # session cookie (so the cookie is set first-party on the site's origin).
    claim_code = sm.create_claim_code(app_session_id)
    return _json_response(200, {
        "connected": True,
        "eventAccess": state_value,
        "claimCode": claim_code,
    })


def admin_claim(event: dict) -> dict:
    """Browser exchanges a one-time claim code (from connect.py, delivered via
    the URL fragment) for the session cookie on THIS (CloudFront) origin."""
    sm = _session_manager()
    body = _parse_json_body(event)
    code = body.get("claimCode")
    if not code or not isinstance(code, str):
        raise InvalidRequestError("claimCode is required")
    app_session_id = sm.redeem_claim_code(code)
    if not app_session_id:
        raise AuthRequiredError()
    sess = sm.get_session(app_session_id)
    if not sess:
        raise AuthRequiredError()
    cookie = _set_cookie(app_session_id, config.SESSION_TTL_SECONDS)
    return _json_response(200, {"connected": True, "eventAccess": sess["authState"]},
                          cookies=[cookie])


def auth_callback(event: dict) -> dict:
    sm = _session_manager()
    q = _query(event)
    code = q.get("code")
    state = q.get("state")
    if q.get("error") or not code or not state:
        raise AuthFailedError("missing code/state or provider error")

    transient = sm.pop_oauth_transient(state)
    if not transient:
        # State did not match a stored value -> reject, set no cookie.
        raise AuthFailedError("state mismatch")

    tokens = _auth.exchange_code(code, transient["codeVerifier"],
                                 expected_nonce=transient["nonce"])

    # Create session first (authenticated), then probe registration.
    app_session_id = sm.create_session(tokens, AuthState.AUTHENTICATED)

    try:
        access_token = sm.get_valid_access_token(app_session_id)
        events = EventsClient(access_token)
        state_value = determine_registration(events)
    except Exception:
        state_value = AuthState.ERROR.value
    sm.set_auth_state(app_session_id, AuthState(state_value))

    cookie = _set_cookie(app_session_id, config.SESSION_TTL_SECONDS)
    spa = config.FRONTEND_ORIGIN or "/"
    return _redirect(spa, cookies=[cookie])


def auth_status(event: dict) -> dict:
    sm = _session_manager()
    app_session_id = _get_cookie(event, config.COOKIE_NAME)
    sess = sm.get_session(app_session_id)
    # The reservations feature flag is a non-secret boolean the SPA uses to decide
    # whether to render reservation controls (Req 21.7, 23.8). Surfaced in both the
    # unauthenticated and authenticated bodies so the browser always learns it.
    if not sess:
        return _json_response(200, {
            "authenticated": False,
            "eventAccess": "unauthenticated",
            "reservationsEnabled": config.RESERVATIONS_ENABLED,
        })
    state = sess["authState"]
    event_access = {
        AuthState.AUTHORIZED.value: "authorized",
        AuthState.NOT_REGISTERED.value: "not_registered",
        AuthState.ERROR.value: "error",
    }.get(state, "authenticated")
    body = {
        "authenticated": True,
        "eventAccess": event_access,
        "csrfToken": sess.get("csrfToken"),  # non-secret anti-CSRF token
        "reservationsEnabled": config.RESERVATIONS_ENABLED,
    }
    return _json_response(200, body)


def logout(event: dict) -> dict:
    sm = _session_manager()
    app_session_id = _get_cookie(event, config.COOKIE_NAME)
    if app_session_id:
        # CSRF: require matching token on state-changing request.
        sess = sm.get_session(app_session_id)
        if sess:
            _require_csrf(event, sess)
            refresh_token = sm.delete_session(app_session_id)
            if refresh_token:
                _auth.revoke(refresh_token)
    return _json_response(200, {"loggedOut": True}, cookies=[_clear_cookie()])


def debug_events(event: dict) -> dict:
    """Authenticated MCP connectivity test (read-only).

    Proves: Lambda -> per-user authenticated MCP client -> AWS Events MCP ->
    re:Invent 2026 data. Requires an authorized session. Returns a SMALL,
    sanitized summary; never raw tokens, never the full catalog.
    """
    sm = _session_manager()
    app_session_id = _get_cookie(event, config.COOKIE_NAME)
    sess = sm.get_session(app_session_id)
    if not sess:
        raise AuthRequiredError()
    if sess["authState"] == AuthState.NOT_REGISTERED.value:
        return _json_response(403, {
            "eventAccess": "not_registered",
            "message": ("Your AWS identity is authenticated, but AWS Events indicates "
                        "that this account is not registered for re:Invent 2026."),
        })
    if sess["authState"] != AuthState.AUTHORIZED.value:
        return _json_response(200, {"eventAccess": sess["authState"]})

    access_token = sm.get_valid_access_token(app_session_id)
    events = EventsClient(access_token)

    # One bounded refresh + retry on 401 (Property 6).
    try:
        sessions, next_token = _list_with_retry(sm, app_session_id, events)
    except McpAuthExpired:
        raise AuthRequiredError()

    sample = [
        {"sessionCode": s.abbreviation, "title": s.title, "type": s.type}
        for s in sessions[:3]
    ]
    return _json_response(200, {
        "eventAccess": "authorized",
        "eventId": config.EVENT_ID,
        "returnedCount": len(sessions),
        "hasMore": next_token is not None,
        "sample": sample,
    })


def _list_with_retry(sm: SessionManager, app_session_id: str, events: EventsClient):
    try:
        return events.list_sessions(include_abstracts=False)
    except McpAuthExpired:
        # refresh once, rebuild client, retry once
        access_token = sm.get_valid_access_token(app_session_id, skew_seconds=10_000)
        events2 = EventsClient(access_token)
        return events2.list_sessions(include_abstracts=False)


MAX_MESSAGE_CHARS = 2000


def chat(event: dict) -> dict:
    """POST /chat — natural-language session discovery (Phase 3).

    Flow (design.md handleChat): authenticate -> resolve session -> gate on
    registration -> resolve any \"#2\"/code reference from stored context ->
    build a per-user Strands agent over the authenticated MCP client -> run the
    turn -> return the assistant text PLUS structured sessions from real tool
    results -> persist compact conversation context. action is always null here
    (favorites are a later phase).
    """
    sm = _session_manager()
    app_session_id = _get_cookie(event, config.COOKIE_NAME)
    sess = sm.get_session(app_session_id)
    if not sess:
        raise AuthRequiredError()

    # CSRF on this state-affecting (context-updating) request.
    _require_csrf(event, sess)

    # Registration gate — no MCP calls for unauthorized users.
    if sess["authState"] == AuthState.NOT_REGISTERED.value:
        return _json_response(403, {
            "conversationId": _parse_chat(event).conversation_id,
            "message": ("Your AWS identity is authenticated, but AWS Events does not "
                        "currently authorize access to re:Invent 2026."),
            "sessions": [], "action": None, "authState": "not_registered",
        })
    if sess["authState"] != AuthState.AUTHORIZED.value:
        return _json_response(200, {
            "conversationId": _parse_chat(event).conversation_id,
            "message": "Please connect your AWS identity before searching re:Invent sessions.",
            "sessions": [], "action": None, "authState": sess["authState"],
        })

    req = _parse_chat(event)
    if not req.conversation_id or not req.message:
        raise InvalidRequestError("conversationId and message are required")
    if len(req.message) > MAX_MESSAGE_CHARS:
        raise InvalidRequestError("message too long")

    store = ctx.ConversationStore(sm.ddb)
    context = store.load(app_session_id, req.conversation_id)
    # Prior turns (multi-turn memory) seeded into the agent so it remembers
    # earlier context in this conversation.
    history = store.load_history_messages(app_session_id, req.conversation_id)

    # Resolve a reference ("#2" / code / "that session") to a concrete id BEFORE
    # the agent runs, so a details request targets the exact session.
    resolved_id = ctx.extract_reference(req.message, context)

    access_token = sm.get_valid_access_token(app_session_id)

    recorder = agent_mod.SessionRecorder()
    request_id = _request_id(event)

    def _resolver(reference: str):
        # Resolve "#2" / code / "that session" using the SAME per-user context.
        return ctx.extract_reference(reference, context) or (resolved_id
               if reference and reference.strip() in req.message else None)

    def build_and_run(token: str) -> str:
        events = EventsClient(token)
        agent = agent_mod.build_agent(events, recorder, resolve_reference=_resolver,
                                      history=history, catalog=_catalog_cache(sm))
        prompt = req.message
        if resolved_id:
            # Give the agent the resolved target explicitly (still its choice to
            # call the right tool, but no guessing of the id).
            prompt = f"{req.message}\n\n[Resolved session reference -> {resolved_id}]"
        return agent_mod.run_agent_turn(agent, prompt, request_id=request_id)

    try:
        try:
            message_text = build_and_run(access_token)
        except McpAuthExpired:
            # bounded single refresh + retry (Property 6)
            access_token = sm.get_valid_access_token(app_session_id, skew_seconds=10_000)
            recorder = agent_mod.SessionRecorder()
            message_text = build_and_run(access_token)
    except McpTimeoutError:
        return _json_response(504, {
            "conversationId": req.conversation_id,
            "message": "That took too long. Please try again.",
            "sessions": [], "action": None,
        })
    except (McpUnavailableError, McpError):
        return _json_response(200, {
            "conversationId": req.conversation_id,
            "message": ("I couldn't retrieve the re:Invent session information right now. "
                        "Please try again."),
            "sessions": [], "action": None,
        })

    structured = recorder.last_result

    # Persist compact conversation context from REAL tool results.
    if structured:
        if len(structured) == 1 and recorder.tools_invoked and \
                recorder.tools_invoked[-1] == "get_session_details":
            store.set_last_session(app_session_id, req.conversation_id,
                                   structured[0].session_id)
        else:
            store.save_sessions(app_session_id, req.conversation_id, structured)

    # Record this turn so the next turn in this conversation has memory of it.
    store.append_history(app_session_id, req.conversation_id, req.message, message_text)

    log_event(request_id=request_id, operation="POST /chat", success=True,
              session_id=app_session_id, conversation_id=req.conversation_id,
              extra={"toolsInvoked": recorder.tools_invoked,
                     "resultCount": len(structured),
                     "referenceResolved": bool(resolved_id),
                     "action": (recorder.action.type + ":" + recorder.action.status)
                               if recorder.action else None})

    action_dict = recorder.action.to_public_dict() if recorder.action else None
    # If show_schedule ran, surface the structured Schedule_View so the frontend
    # renders it as cards (the model then only writes a short summary, not all
    # ~N items -- which was overrunning the turn budget on dense days).
    schedule_dict = (recorder.schedule_view.to_public_dict()
                     if recorder.schedule_view is not None else None)
    resp = ChatResponse(conversation_id=req.conversation_id, message=message_text,
                        sessions=structured, action=action_dict, schedule=schedule_dict)
    return _json_response(200, resp.to_public_dict())


def _parse_chat(event: dict) -> ChatRequest:
    body = event.get("body")
    if isinstance(body, str) and body:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    if not isinstance(body, dict):
        body = {}
    return ChatRequest.from_body(body)


def _require_csrf(event: dict, sess: dict) -> None:
    headers = event.get("headers") or {}
    provided = headers.get("x-csrf-token") or headers.get("X-CSRF-Token")
    expected = sess.get("csrfToken")
    if not expected or provided != expected:
        raise InvalidRequestError("CSRF token missing or invalid")


# ── Favorites (Phase 4) ──────────────────────────────────────────────────────
def _authorized_session_or_raise(event: dict):
    """Return (SessionManager, app_session_id, session) for an AUTHORIZED user,
    or raise the appropriate controlled error. The AWS Events identity is always
    derived from the authenticated app session — never from client input."""
    sm = _session_manager()
    app_session_id = _get_cookie(event, config.COOKIE_NAME)
    sess = sm.get_session(app_session_id)
    if not sess:
        raise AuthRequiredError()
    if sess["authState"] == AuthState.NOT_REGISTERED.value:
        raise EventAccessDeniedError()
    if sess["authState"] != AuthState.AUTHORIZED.value:
        raise AuthRequiredError()
    return sm, app_session_id, sess


def _events_for(sm: SessionManager, app_session_id: str) -> EventsClient:
    return EventsClient(sm.get_valid_access_token(app_session_id))


def get_favorites(event: dict) -> dict:
    sm, app_session_id, _ = _authorized_session_or_raise(event)
    try:
        sessions = _favorites_with_retry(sm, app_session_id)
    except McpAuthExpired:
        raise AuthRequiredError()
    except McpTimeoutError:
        return _json_response(504, {"sessions": [],
                                    "message": "That took too long. Please try again."})
    except (McpUnavailableError, McpError):
        return _json_response(200, {"sessions": [],
                                    "message": "I couldn't retrieve your favorites right now. Please try again."})
    return _json_response(200, {"sessions": [s.to_public_dict() for s in sessions]})


def get_favorite_ids(event: dict) -> dict:
    """Fast endpoint: return just the user's favorite sessionIds (one GetSchedule
    call, no per-session details). Used by the UI to render star state quickly."""
    sm, app_session_id, _ = _authorized_session_or_raise(event)
    try:
        events = _events_for(sm, app_session_id)
        try:
            ids = fav.get_favorite_ids(events)
        except McpAuthExpired:
            events = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
            ids = fav.get_favorite_ids(events)
    except (McpAuthExpired,):
        raise AuthRequiredError()
    except (McpUnavailableError, McpError, McpTimeoutError):
        return _json_response(200, {"favoriteIds": []})
    return _json_response(200, {"favoriteIds": ids})


def _favorites_with_retry(sm: SessionManager, app_session_id: str):
    events = _events_for(sm, app_session_id)
    catalog = _catalog_cache(sm)
    try:
        return fav.get_favorites(events, catalog=catalog)
    except McpAuthExpired:
        events2 = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
        return fav.get_favorites(events2, catalog=catalog)


def post_favorite(event: dict) -> dict:
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    body = _parse_json_body(event)
    session_id = (body or {}).get("sessionId")
    if not session_id or not isinstance(session_id, str):
        raise InvalidRequestError("sessionId is required")
    action = _favorite_with_retry(sm, app_session_id, session_id, favorite=True)
    return _json_response(200, {"action": action.to_public_dict()})


def delete_favorite(event: dict, session_id: str) -> dict:
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    if not session_id:
        raise InvalidRequestError("sessionId is required")
    action = _favorite_with_retry(sm, app_session_id, session_id, favorite=False)
    return _json_response(200, {"action": action.to_public_dict()})


def _favorite_with_retry(sm: SessionManager, app_session_id: str, session_id: str,
                         *, favorite: bool) -> ActionResult:
    def run(token):
        events = EventsClient(token)
        if favorite:
            return fav.favorite_session(events, session_id)
        return fav.unfavorite_session(events, session_id)
    try:
        return run(sm.get_valid_access_token(app_session_id))
    except McpAuthExpired:
        return run(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
    except (McpUnavailableError, McpError, McpTimeoutError):
        return ActionResult(type="favorite" if favorite else "unfavorite",
                            status="failed", session_id=session_id,
                            reason="service_unavailable")


def _parse_json_body(event: dict) -> dict:
    body = event.get("body")
    if isinstance(body, str) and body:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return {}
    return body if isinstance(body, dict) else {}


# ── Reservations (Phase 11 — PHASED, gated on config.RESERVATIONS_ENABLED) ───
# These mirror post_favorite / delete_favorite exactly (authorized session +
# CSRF; one bounded refresh+retry on an expired MCP token; return the structured
# ActionResult). A reservation is NOT a favorite — the ActionResult types are
# "reserve" / "cancel-reservation". The routes are only registered / dispatched
# while RESERVATIONS_ENABLED is true; while disabled they fall through to 404
# (Req 21.1, 21.3, 21.10, 23.8). The sessionId for a cancel comes from the PATH,
# never the body.
def post_reservation(event: dict) -> dict:
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    body = _parse_json_body(event)
    session_id = (body or {}).get("sessionId")
    if not session_id or not isinstance(session_id, str):
        raise InvalidRequestError("sessionId is required")
    action = _reservation_with_retry(sm, app_session_id, session_id, reserve=True)
    return _json_response(200, {"action": action.to_public_dict()})


def delete_reservation_route(event: dict, session_id: str) -> dict:
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    if not session_id:
        raise InvalidRequestError("sessionId is required")
    action = _reservation_with_retry(sm, app_session_id, session_id, reserve=False)
    return _json_response(200, {"action": action.to_public_dict()})


def _reservation_with_retry(sm: SessionManager, app_session_id: str, session_id: str,
                            *, reserve: bool) -> ActionResult:
    def run(token):
        events = EventsClient(token)
        if reserve:
            return resv.reserve_session(events, session_id)
        return resv.cancel_reservation(events, session_id)
    try:
        return run(sm.get_valid_access_token(app_session_id))
    except McpAuthExpired:
        return run(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
    except (McpUnavailableError, McpError, McpTimeoutError):
        return ActionResult(type="reserve" if reserve else "cancel-reservation",
                            status="failed", session_id=session_id,
                            reason="service_unavailable")


# ── Reserved-list reads (Phase 11 — PHASED, gated on RESERVATIONS_ENABLED) ────
# These mirror get_favorites / get_favorite_ids exactly: an AUTHORIZED app
# session is required, identity is always derived from that session (never from
# client input), and reads carry NO CSRF (they change no state — Req 11.3,
# 11.5). The reserved ids come solely from AWS Events (GetSchedule reserved[]),
# never a local cache. Error discipline follows the favorites reads: one bounded
# refresh+retry on an expired MCP token; timeout -> 504 with a friendly body;
# MCP unavailable/other -> soft 200 with an empty list + friendly message so
# reservation state is never misrepresented (Req 10.*). The handlers are named
# with a `_route` suffix where they would otherwise shadow the reservations
# service functions (resv.get_reserved_ids), keeping the app layer unambiguous.
def get_reservations(event: dict) -> dict:
    """GET /reservations — the owner's reserved sessions as resolved Session
    objects, derived solely from AWS Events (GetSchedule reserved[] -> GetSession
    fan-out). Mirrors get_favorites: honest degradation messaging only, with the
    true reserved total available for an honest "showing N of M" when the detail
    fan-out is capped."""
    sm, app_session_id, _ = _authorized_session_or_raise(event)
    try:
        sessions, total = _reserved_sessions_with_retry(sm, app_session_id)
    except McpAuthExpired:
        raise AuthRequiredError()
    except McpTimeoutError:
        return _json_response(504, {"sessions": [],
                                    "message": "That took too long. Please try again."})
    except (McpUnavailableError, McpError):
        return _json_response(200, {"sessions": [],
                                    "message": "I couldn't retrieve your reserved sessions right now. Please try again."})
    body = {"sessions": [s.to_public_dict() for s in sessions], "total": total}
    # Honest totals: `total` is the real number of reserved ids from AWS Events.
    # When some (or all) of them could not be resolved to session details, say
    # so instead of letting the UI read an empty list as "no reservations".
    if total > len(sessions):
        if sessions:
            body["message"] = (f"Showing {len(sessions)} of your {total} reserved "
                               f"sessions; the rest couldn't be loaded right now.")
        else:
            body["message"] = (f"You have {total} reserved seat"
                               f"{'s' if total != 1 else ''}, but their session "
                               f"details couldn't be loaded right now.")
    return _json_response(200, body)


def get_reserved_ids_route(event: dict) -> dict:
    """GET /reservations/ids — fast endpoint returning just the owner's reserved
    sessionIds (one GetSchedule call, no per-session details), used by the UI to
    seed per-card reserve state quickly. Mirrors get_favorite_ids, including its
    soft-200 empty-list degradation. Named with a `_route` suffix so it does not
    shadow the reservations service function ``resv.get_reserved_ids``."""
    sm, app_session_id, _ = _authorized_session_or_raise(event)
    try:
        events = _events_for(sm, app_session_id)
        try:
            ids = resv.get_reserved_ids(events)
        except McpAuthExpired:
            events = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
            ids = resv.get_reserved_ids(events)
    except (McpAuthExpired,):
        raise AuthRequiredError()
    except (McpUnavailableError, McpError, McpTimeoutError):
        return _json_response(200, {"reservedIds": []})
    return _json_response(200, {"reservedIds": ids})


def _reserved_sessions_with_retry(sm: SessionManager, app_session_id: str):
    events = _events_for(sm, app_session_id)
    try:
        return resv.get_reserved_sessions_with_total(events)
    except McpAuthExpired:
        events2 = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
        return resv.get_reserved_sessions_with_total(events2)


# ── Schedule + personal time (Phase 9) ───────────────────────────────────────
def get_schedule(event: dict) -> dict:
    """GET /schedule — the owner's ordered Schedule_View derived solely from
    AWS Events state (reserved + favorite sessions resolved to details, plus
    personal-time blocks). Mirrors get_favorites' one bounded refresh+retry on
    an expired MCP token and its friendly degradation on timeout/unavailable."""
    sm, app_session_id, _ = _authorized_session_or_raise(event)
    try:
        view = _schedule_with_retry(sm, app_session_id)
    except McpAuthExpired:
        raise AuthRequiredError()
    except McpTimeoutError as exc:
        _log_soft_failure(event, "GET /schedule", exc)
        return _json_response(504, {"reserved": [], "favorites": [], "personalTime": [],
                                    "message": "That took too long. Please try again."})
    except (McpUnavailableError, McpError) as exc:
        _log_soft_failure(event, "GET /schedule", exc)
        return _json_response(200, {"reserved": [], "favorites": [], "personalTime": [],
                                    "message": "I couldn't retrieve your schedule right now. Please try again."})
    return _json_response(200, view.to_public_dict())


def _log_soft_failure(event: dict, operation: str, exc: Exception) -> None:
    """Record WHY a request degraded to a friendly message. Those responses are
    returned (not raised), so the dispatcher's log line would otherwise call them
    successes and the cause would be invisible in CloudWatch."""
    # The typed MCP errors are raised "from" the original transport/SDK error;
    # that original is the real reason (HTTP 429, connection reset, ...).
    cause = exc.__cause__ or exc.__context__
    extra = {"degraded": True, "exception": type(exc).__name__,
             "message": safe_text(exc)}
    if cause is not None:
        extra["cause"] = safe_text(f"{type(cause).__name__}: {cause}")
    log_event(request_id=_request_id(event), operation=operation, success=False,
              error_category=getattr(exc, "category", type(exc).__name__),
              session_id=_get_cookie(event, config.COOKIE_NAME), extra=extra)


_MIN_RETRY_BUDGET_SECONDS = 8.0


def _build_view_with_transient_retry(events, catalog=None):
    """Build the schedule view within the whole-request budget. If AWS Events
    fails (unavailable, a tool error, or a call that timed out), try once more
    -- but only when enough of the budget is left for the retry to have a real
    chance. The retry is given only what remains, so the two attempts together
    can never run past the Lambda limit (the old code could, and was killed at
    29 s with a 500)."""
    budget = float(config.SCHEDULE_REQUEST_BUDGET_SECONDS)
    started = time.monotonic()
    try:
        return sched.build_schedule_view(events, time_budget=budget, catalog=catalog)
    except (McpUnavailableError, McpError, McpTimeoutError) as first:
        left = budget - (time.monotonic() - started)
        if left < _MIN_RETRY_BUDGET_SECONDS:
            raise
        if not isinstance(first, McpTimeoutError):
            time.sleep(1.0)               # let a brief outage / throttle pass
            left -= 1.0
        return sched.build_schedule_view(events, time_budget=left, catalog=catalog)


def _schedule_with_retry(sm: SessionManager, app_session_id: str):
    events = _events_for(sm, app_session_id)
    catalog = _catalog_cache(sm)
    try:
        return _build_view_with_transient_retry(events, catalog=catalog)
    except McpAuthExpired:
        events2 = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
        return _build_view_with_transient_retry(events2, catalog=catalog)


def post_personal_time(event: dict) -> dict:
    """POST /personal-time — create a personal-time block for the authorized
    owner. Identity derives from the app session, never from client input.
    Validation (length caps + Pacific→UTC datetime contract) runs inside the
    service BEFORE any MCP call, so a bad request raises ValidationError (→ 400)
    with Schedule_View untouched."""
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    body = _parse_json_body(event)
    result = _personal_time_with_retry(
        sm, app_session_id,
        lambda events: sched.create_personal_time(
            events,
            title=body.get("title"),
            start=body.get("start"),
            end=body.get("end"),
            description=body.get("description"),
            location=body.get("location"),
        ),
    )
    return _json_response(200, {"action": result.to_public_dict()})


def put_personal_time(event: dict, personal_time_id: str) -> dict:
    """PUT /personal-time/{personalTimeId} — full-replacement update. The id is
    sliced from the path (never trusted from the body). An unknown id yields a
    not_found result leaving Schedule_View unchanged."""
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    body = _parse_json_body(event)
    result = _personal_time_with_retry(
        sm, app_session_id,
        lambda events: sched.update_personal_time(
            events,
            personal_time_id=personal_time_id,
            title=body.get("title"),
            start=body.get("start"),
            end=body.get("end"),
            description=body.get("description"),
            location=body.get("location"),
        ),
    )
    return _json_response(200, {"action": result.to_public_dict()})


def delete_personal_time_route(event: dict, personal_time_id: str) -> dict:
    """DELETE /personal-time/{personalTimeId} — idempotent delete. The id comes
    from the path only. An already-removed / not-found block reports success."""
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    result = _personal_time_with_retry(
        sm, app_session_id,
        lambda events: sched.delete_personal_time(events, personal_time_id),
    )
    return _json_response(200, {"action": result.to_public_dict()})


def _personal_time_with_retry(sm: SessionManager, app_session_id: str, call):
    """Run a personal-time service call with one bounded refresh + retry on an
    expired MCP token (Property 6). Validation errors raised by the service
    before any MCP call propagate out (→ 400) and are NOT retried; the service
    itself maps MCP errors/timeouts to a soft failure result."""
    events = _events_for(sm, app_session_id)
    try:
        return call(events)
    except McpAuthExpired:
        events2 = EventsClient(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
        return call(events2)


# ── Catalog index (search over the whole catalog) ────────────────────────────
def _catalog_cache(sm: SessionManager):
    """The catalog index cache, or None when the feature is switched off."""
    if not config.CATALOG_CACHE_ENABLED:
        return None
    return catalog_cache.CatalogCache(sm.ddb)


def post_catalog_warm(event: dict) -> dict:
    """POST /catalog/warm -- advance (or verify) the catalog index.

    The page calls this in the background after load, repeatedly, until the index
    reports ``ready``. Each call does a bounded amount of work and saves its
    progress, so a build finishes across a few calls and never eats into a chat
    turn. Requires an authorized session and CSRF (it makes MCP calls and writes
    to DynamoDB). It exposes no session data: only counts and ages.
    """
    sm, app_session_id, sess = _authorized_session_or_raise(event)
    _require_csrf(event, sess)
    cache = _catalog_cache(sm)
    if cache is None:
        return _json_response(200, {"enabled": False, "ready": False})

    def run(token: str) -> dict:
        return cache.warm(EventsClient(token))

    try:
        try:
            status = run(sm.get_valid_access_token(app_session_id))
        except McpAuthExpired:
            status = run(sm.get_valid_access_token(app_session_id, skew_seconds=10_000))
    except McpAuthExpired:
        raise AuthRequiredError()
    except (McpTimeoutError, McpUnavailableError, McpError) as exc:
        # Progress already saved stays; report where things stand and let the
        # caller try again later. Search keeps working in its fallback mode.
        # This is returned as a 200, so record the cause explicitly (otherwise
        # the request log calls it a success and the reason is lost).
        _log_soft_failure(event, "POST /catalog/warm", exc)
        status = cache.status()
        status["error"] = True
    # One line per warm call showing where the build stands, so the logs show
    # exactly when the index became ready relative to other requests.
    log_event(request_id=_request_id(event), operation="catalog/warm_status",
              success=not status.get("error"),
              session_id=_get_cookie(event, config.COOKIE_NAME),
              extra={k: status.get(k) for k in ("ready", "building", "pagesBuilt",
                                               "sessions", "ageSeconds", "error")
                     if k in status})
    return _json_response(200, status)


# ── Dispatch ─────────────────────────────────────────────────────────────────
_ROUTES = {
    ("GET", "/auth/login"): auth_login,
    ("GET", "/auth/callback"): auth_callback,
    ("GET", "/auth/status"): auth_status,
    ("POST", "/logout"): logout,
    ("POST", "/chat"): chat,
    ("POST", "/admin/connect"): admin_connect,
    ("POST", "/admin/claim"): admin_claim,
    ("POST", "/catalog/warm"): post_catalog_warm,
    ("GET", "/favorites"): get_favorites,
    ("GET", "/favorites/ids"): get_favorite_ids,
    ("POST", "/favorites"): post_favorite,
    ("GET", "/schedule"): get_schedule,
    ("POST", "/personal-time"): post_personal_time,
    ("GET", "/debug/events"): debug_events,
}

# Phased reservation routes are registered ONLY while the capability is enabled.
# While disabled the key is absent, so the existing 404 path handles any request
# to it — the endpoint is not merely rejected, it does not exist (Req 21.10,
# 23.8). DELETE /reservations/{sessionId} is handled as a flag-gated path-prefix
# branch inside lambda_handler (the id lives in the path, never the body).
if config.RESERVATIONS_ENABLED:
    _ROUTES[("POST", "/reservations")] = post_reservation
    # Reserved-list reads live in the SAME flag-gated block as the write route,
    # so they 404 identically while the capability is disabled (Req 4.1, 11.3).
    # Reads carry no CSRF; identity is derived from the app session.
    _ROUTES[("GET", "/reservations")] = get_reservations
    _ROUTES[("GET", "/reservations/ids")] = get_reserved_ids_route


def lambda_handler(event: dict, context=None) -> dict:
    method, path = _method_path(event)
    request_id = _request_id(event)

    # CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers(), "body": ""}

    handler = _ROUTES.get((method, path))
    # Path-prefix routes carry their id in the PATH, never the body. The sliced
    # id is passed positionally to the handler.
    path_arg = None
    operation_template = None
    if handler is None and method == "DELETE" and path.startswith("/favorites/"):
        path_arg = path[len("/favorites/"):]
        handler = delete_favorite
        operation_template = "/favorites/{id}"
    elif handler is None and path.startswith("/personal-time/"):
        if method == "PUT":
            path_arg = path[len("/personal-time/"):]
            handler = put_personal_time
            operation_template = "/personal-time/{id}"
        elif method == "DELETE":
            path_arg = path[len("/personal-time/"):]
            handler = delete_personal_time_route
            operation_template = "/personal-time/{id}"
    elif (handler is None and config.RESERVATIONS_ENABLED and method == "DELETE"
          and path.startswith("/reservations/")):
        # Only dispatched while the phased reservation capability is enabled;
        # otherwise it falls through to the 404 below. The sessionId is sliced
        # from the PATH, never the body.
        path_arg = path[len("/reservations/"):]
        handler = delete_reservation_route
        operation_template = "/reservations/{id}"
    if handler is None:
        return _json_response(404, {"error": {"category": "INVALID_REQUEST",
                                               "message": "Not found."}})

    operation = f"{method} {operation_template or path}"
    with Timer() as timer:
        try:
            resp = handler(event, path_arg) if path_arg is not None else handler(event)
            # Record the HTTP status too: a handler can return a 5xx (e.g. a
            # timeout mapped to 504) without raising, which is otherwise
            # indistinguishable from success in the logs. (Key is "status", not
            # "statusCode": the scrubber redacts any key containing "code".)
            status = resp.get("statusCode") if isinstance(resp, dict) else None
            log_event(request_id=request_id, operation=operation,
                      success=not (isinstance(status, int) and status >= 500),
                      duration_ms=timer.duration_ms,
                      session_id=_get_cookie(event, config.COOKIE_NAME),
                      extra={"status": status})
            return resp
        except AppError as e:
            log_event(request_id=request_id, operation=operation, success=False,
                      duration_ms=timer.duration_ms, error_category=e.category,
                      session_id=_get_cookie(event, config.COOKIE_NAME))
            body = {"error": {"category": e.category, "message": e.client_message}}
            # For auth failures during the browser flow, redirect to SPA with a flag.
            if isinstance(e, AuthFailedError) and path == "/auth/callback":
                dest = (config.FRONTEND_ORIGIN or "/") + "?auth_error=1"
                return _redirect(dest)
            return _json_response(e.http_status, body)
        except Exception:
            log_event(request_id=request_id, operation=operation, success=False,
                      duration_ms=timer.duration_ms, error_category="INTERNAL_ERROR",
                      session_id=_get_cookie(event, config.COOKIE_NAME))
            return _json_response(500, {"error": {"category": "INTERNAL_ERROR",
                                                  "message": "Something went wrong."}})
