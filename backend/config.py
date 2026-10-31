"""Configuration for the re:Invent Session Assistant backend.

All values are drawn from environment variables / SSM so they can vary per
stage. NO secrets (client_secret, tokens) appear here; those are loaded from
Secrets Manager at runtime in auth.py / session_manager.py.

Design reference: design.md "Components and Interfaces" — config.py section.
"""

from __future__ import annotations

import os


# ── AWS Events OAuth (Amazon Cognito) ──────────────────────────────────────
OAUTH_AUTHORIZE_URL: str = os.environ.get(
    "OAUTH_AUTHORIZE_URL",
    "https://oauth.awsevents.com/oauth2/authorize",
)
OAUTH_TOKEN_URL: str = os.environ.get(
    "OAUTH_TOKEN_URL",
    "https://oauth.awsevents.com/oauth2/token",
)
OAUTH_REVOKE_URL: str = os.environ.get(
    "OAUTH_REVOKE_URL",
    "https://oauth.awsevents.com/oauth2/revoke",
)
OAUTH_USERINFO_URL: str = os.environ.get(
    "OAUTH_USERINFO_URL",
    "https://oauth.awsevents.com/oauth2/userInfo",
)
OAUTH_LOGOUT_URL: str = os.environ.get(
    "OAUTH_LOGOUT_URL",
    "https://oauth.awsevents.com/logout",
)
OAUTH_JWKS_URI: str = os.environ.get(
    "OAUTH_JWKS_URI",
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_NdvQjp0vF/.well-known/jwks.json",
)
OAUTH_ISSUER: str = os.environ.get(
    "OAUTH_ISSUER",
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_NdvQjp0vF",
)
OAUTH_CLIENT_ID: str = os.environ.get(
    "OAUTH_CLIENT_ID",
    "7vmom55m1qstvq8i71ph127bfq",
)
OAUTH_SCOPES: str = os.environ.get(
    "OAUTH_SCOPES",
    "openid email events/access",
)
# Redirect URI for the hosted callback (verified in Phase 0).
# In production this points to the API Gateway /auth/callback.
OAUTH_REDIRECT_URI: str = os.environ.get("OAUTH_REDIRECT_URI", "")

# If the app client is CONFIDENTIAL, the backend loads the secret from
# this Secrets Manager ARN at runtime (never from env vars / source).
OAUTH_CLIENT_SECRET_ARN: str = os.environ.get("OAUTH_CLIENT_SECRET_ARN", "")

# ── MCP Server ─────────────────────────────────────────────────────────────
MCP_ENDPOINT: str = os.environ.get(
    "MCP_ENDPOINT",
    "https://api.awsevents.com/mcp",
)
EVENT_ID: str = os.environ.get("EVENT_ID", "reinvent2026")

# ── DynamoDB ───────────────────────────────────────────────────────────────
DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "ReInventAssistant")

# ── Session / cookie ──────────────────────────────────────────────────────
SESSION_TTL_SECONDS: int = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))
OAUTH_TRANSIENT_TTL_SECONDS: int = int(os.environ.get("OAUTH_TRANSIENT_TTL_SECONDS", "600"))  # 10 min
CONVERSATION_CTX_TTL_SECONDS: int = int(os.environ.get("CONVERSATION_CTX_TTL_SECONDS", "3600"))

COOKIE_NAME: str = "appSessionId"
COOKIE_DOMAIN: str = os.environ.get("COOKIE_DOMAIN", "")

# ── Frontend origin for CORS + cookie SameSite ───────────────────────────
FRONTEND_ORIGIN: str = os.environ.get("FRONTEND_ORIGIN", "")

# ── AWS region ────────────────────────────────────────────────────────────
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")

# ── Timeouts (seconds) ────────────────────────────────────────────────────
OAUTH_EXCHANGE_TIMEOUT: int = int(os.environ.get("OAUTH_EXCHANGE_TIMEOUT", "10"))
# Per-call ceiling for one AWS Events MCP call. A normal call takes 1-3 s, so 10 s
# is generous. It must stay well under Lambda's 29 s limit: a call that hangs is
# abandoned at this limit (the request then fails fast and can be retried)
# instead of holding the whole request until Lambda kills it.
MCP_CALL_TIMEOUT: int = int(os.environ.get("MCP_CALL_TIMEOUT", "10"))
# Whole-request time budget for building the schedule view (GET /schedule):
# GetSchedule plus the GetSession fan-out, retries included. Lambda stops at 29 s.
SCHEDULE_REQUEST_BUDGET_SECONDS: int = int(os.environ.get("SCHEDULE_REQUEST_BUDGET_SECONDS", "24"))
# The same, when the schedule is built inside a chat turn (the agent turn has a
# 26 s budget that also pays for the model calls).
CHAT_SCHEDULE_BUDGET_SECONDS: int = int(os.environ.get("CHAT_SCHEDULE_BUDGET_SECONDS", "16"))

# ── Bedrock / Strands Agent (Phase 3) ─────────────────────────────────────
BEDROCK_MODEL_ID: str = os.environ.get(
    "BEDROCK_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_REGION: str = os.environ.get("BEDROCK_REGION", AWS_REGION)

# Number of sessions returned to the user by default for a broad query.
DEFAULT_SESSION_RESULT_LIMIT: int = int(os.environ.get("DEFAULT_SESSION_RESULT_LIMIT", "5"))
# Hard cap even if the user asks for "more".
MAX_SESSION_RESULT_LIMIT: int = int(os.environ.get("MAX_SESSION_RESULT_LIMIT", "25"))
# Max ListSessions pages the agent tool will walk in one search (pagination guard).
MAX_LIST_PAGES: int = int(os.environ.get("MAX_LIST_PAGES", "3"))
# Overall agent turn budget (seconds), inside the 29s API Gateway limit.
AGENT_TURN_TIMEOUT: int = int(os.environ.get("AGENT_TURN_TIMEOUT", "26"))

# ── Single-user admin connect (native/loopback token push) ────────────────
# The deployed browser cannot complete AWS Events' loopback-only OAuth, so the
# single user obtains a token locally (docs/spikes/get_token.py) and pushes it
# to the backend via POST /admin/connect, authenticated by this key. The key is
# read from Secrets Manager (ARN) at runtime, NOT hardcoded. Empty = disabled.
ADMIN_KEY_SECRET_ARN: str = os.environ.get("ADMIN_KEY_SECRET_ARN", "")
# Local-dev fallback only (never set in production); lets you test the flow.
ADMIN_KEY_DEV: str = os.environ.get("ADMIN_KEY_DEV", "")

# ── Counting / aggregation ────────────────────────────────────────────────
# Counting the full catalog (by date/venue/topic) must page through ALL
# sessions, unlike search which stops early. This caps the scan so a runaway
# never blows the 29s budget (~2000 sessions / page size ~ well under this).
MAX_COUNT_PAGES: int = int(os.environ.get("MAX_COUNT_PAGES", "60"))
# Max favorites to fetch full details for when grouping favorites by date/venue.
MAX_FAVORITE_DETAILS: int = int(os.environ.get("MAX_FAVORITE_DETAILS", "100"))
# Max favorites / reserved sessions to resolve to FULL details when showing
# the Schedule_View or a "show my favorites" list. The id list itself is
# never capped (counts stay truthful); this only bounds the per-session
# GetSession fan-out so a large plan can't blow the agent-turn budget. When
# a list is longer than this, callers report "showing N of M" rather than
# implying the shown set is complete.
MAX_SCHEDULE_DETAILS: int = int(os.environ.get("MAX_SCHEDULE_DETAILS", "100"))
# Concurrency for the GetSession detail fan-out used by the Schedule_View and
# "show my favorites". Each favorite/reserved id needs its own GetSession; a
# wider pool means fewer sequential waves and keeps a large plan inside the
# agent-turn budget. Bounded so we never open an unreasonable number of
# simultaneous MCP connections.
SCHEDULE_FETCH_WORKERS: int = int(os.environ.get("SCHEDULE_FETCH_WORKERS", "20"))
# Hard overall deadline (seconds) for the GetSession detail fan-out. Python
# threads can't be force-killed, so without a wall the fan-out could run to the
# 29s API Gateway limit (hard 500). We instead collect whatever resolved within
# this budget and skip stragglers -> a complete-as-possible schedule, never a
# hang. Kept well under AGENT_TURN_TIMEOUT (26s).
SCHEDULE_FETCH_DEADLINE: int = int(os.environ.get("SCHEDULE_FETCH_DEADLINE", "15"))

# ── Catalog index cache (search over the WHOLE catalog) ───────────────────
# ListSessions can only be paged (no filter), and each page is slow, so reading
# the whole ~2,400-session catalog cannot fit inside one chat turn. A compact
# INDEX of the catalog is therefore built in the background (POST /catalog/warm,
# called by the page after load) and stored in DynamoDB. The index is used ONLY
# to decide which sessions match a search/count; the sessions actually shown are
# re-read live from AWS Events, so room/time/availability are never stale.
# User state (favorites, reservations, schedule, personal time) is never cached.
CATALOG_CACHE_ENABLED: bool = os.environ.get("CATALOG_CACHE_ENABLED", "true").lower() not in ("0", "false", "no")
# After this long the catalog's session COUNT is re-checked against AWS Events
# (one cheap call); if it changed, the index is rebuilt.
CATALOG_RECHECK_SECONDS: int = int(os.environ.get("CATALOG_RECHECK_SECONDS", "900"))
# SOFT limit: once the index is this old, the next warm call rebuilds it in the
# background WHILE the old index keeps serving searches, then switches over when
# the new one is complete, so there is no gap of partial searches. It is set to
# about the length of a connection (the user's token lasts ~1 hour), because a
# warm call only happens while a session is valid: this way every fresh
# connection rebuilds an index that is over an hour old. That also bounds how
# long a same-size swap of sessions (one added, one removed), which the
# totalCount check cannot see, can go unnoticed. Keep it well below
# CATALOG_MAX_AGE_SECONDS.
CATALOG_REFRESH_SECONDS: int = int(os.environ.get("CATALOG_REFRESH_SECONDS", "3600"))
# HARD limit: an index older than this is never used for searching. It only
# matters when nobody kept the page open (nothing can rebuild without the user's
# token, which lasts about an hour), so the index quietly expires.
CATALOG_MAX_AGE_SECONDS: int = int(os.environ.get("CATALOG_MAX_AGE_SECONDS", "21600"))
# Time budget (seconds) for one warm call; the build resumes across calls.
CATALOG_WARM_BUDGET_SECONDS: int = int(os.environ.get("CATALOG_WARM_BUDGET_SECONDS", "15"))
# The FIRST step of a brand-new build reads only this many pages, so the page
# learns within a few seconds that a build is under way (a full step can take
# ~15 s, during which the schedule dialog would not know whether to wait).
# Later steps continue the build at full size. 0 disables the short first step.
CATALOG_FIRST_STEP_PAGES: int = int(os.environ.get("CATALOG_FIRST_STEP_PAGES", "1"))
# A partially built index older than this is abandoned and restarted.
CATALOG_BUILD_MAX_AGE_SECONDS: int = int(os.environ.get("CATALOG_BUILD_MAX_AGE_SECONDS", "1800"))
# Safety cap on pages read while building (catalog is ~10 pages of 250).
CATALOG_MAX_PAGES: int = int(os.environ.get("CATALOG_MAX_PAGES", "40"))
# DynamoDB items are limited to 400 KB; index chunks are split to stay under this.
CATALOG_CHUNK_BYTES: int = int(os.environ.get("CATALOG_CHUNK_BYTES", "300000"))
# Deadline (seconds) for re-reading the shown sessions live from AWS Events.
CATALOG_LIVE_REFRESH_DEADLINE: float = float(os.environ.get("CATALOG_LIVE_REFRESH_DEADLINE", "8"))

# Where FAVORITES get their session details (title, time, room, speakers) for
# the schedule view and the favorites list. Reading ~70 of them live means ~70
# fresh connections to AWS Events, which is slow and gets throttled (the schedule
# took 16-20 s and showed "51 of 70"). With this on, favorites take their details
# from the catalog index (one DynamoDB read, no AWS Events calls), the response
# says how old that index is, and only favorites missing from the index are read
# live. RESERVED sessions are always re-read live: they are real commitments.
# Set to "false" to read everything live, as before.
FAVORITE_DETAILS_FROM_INDEX: bool = os.environ.get("FAVORITE_DETAILS_FROM_INDEX", "true").lower() not in ("0", "false", "no")

# ── Session-code lookup ───────────────────────────────────────────────────
# ListSessions has no search/filter, only paging (250 per page, ~2400 sessions =
# ~10 pages), while topic search deliberately reads only MAX_LIST_PAGES pages.
# Looking up ONE session by its code (e.g. "IND422") must therefore walk the
# catalog until it finds the code. These bound that walk so it can never blow
# the agent-turn budget (AGENT_TURN_TIMEOUT).
MAX_CODE_LOOKUP_PAGES: int = int(os.environ.get("MAX_CODE_LOOKUP_PAGES", "14"))
CODE_LOOKUP_BUDGET_SECONDS: int = int(os.environ.get("CODE_LOOKUP_BUDGET_SECONDS", "14"))
# How long a code->session index built from walked pages is reused (seconds).
CODE_INDEX_TTL_SECONDS: int = int(os.environ.get("CODE_INDEX_TTL_SECONDS", "3600"))

# ── Conversation memory (multi-turn history) ──────────────────────────────
# How many prior turns (user+assistant pairs) to replay into the agent so it
# remembers earlier context. Kept small to bound prompt size / latency.
MAX_HISTORY_TURNS: int = int(os.environ.get("MAX_HISTORY_TURNS", "8"))
# Truncate each stored message to keep history compact.
MAX_HISTORY_MSG_CHARS: int = int(os.environ.get("MAX_HISTORY_MSG_CHARS", "1500"))

# ── Reservations feature flag (Phase 8+) ──────────────────────────────────
# Single switch gating the phased reservation capability. Default false; while
# disabled the agent must not offer reservation or claim any seat is reserved.
RESERVATIONS_ENABLED: bool = os.environ.get("RESERVATIONS_ENABLED", "").lower() in ("1", "true", "yes")

# ── Personal-time field length caps ───────────────────────────────────────
# Max input lengths for personal-time entries, enforced before any MCP call.
PERSONAL_TIME_TITLE_MAX_LEN: int = 200
PERSONAL_TIME_DESCRIPTION_MAX_LEN: int = 1000
PERSONAL_TIME_LOCATION_MAX_LEN: int = 200
