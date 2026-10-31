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
MCP_CALL_TIMEOUT: int = int(os.environ.get("MCP_CALL_TIMEOUT", "20"))

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
MAX_SCHEDULE_DETAILS: int = int(os.environ.get("MAX_SCHEDULE_DETAILS", "30"))
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
