"""Structured, secret-safe CloudWatch logging.

Emits one JSON log line per operation with: requestId, session id (anonymized),
operation, duration, success/failure, error category. NEVER logs tokens,
authorization codes, refresh tokens, or credentials (tasks.md section 19).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from typing import Any, Optional

_logger = logging.getLogger("reinvent_assistant")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
_logger.setLevel(logging.INFO)
_logger.propagate = False

# Patterns that must never leak into logs even if accidentally passed.
_SECRET_KEYS = re.compile(
    r"(access_token|refresh_token|id_token|authorization|code_verifier|"
    r"client_secret|secret|bearer|password|code)",
    re.IGNORECASE,
)


def anonymize(value: Optional[str]) -> Optional[str]:
    """Return a short, stable, non-reversible tag for an identifier."""
    if not value:
        return None
    return "sid_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _scrub(obj: Any) -> Any:
    """Recursively drop values whose key looks secret; truncate long strings."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)):
                clean[k] = "[REDACTED]"
            else:
                clean[k] = _scrub(v)
        return clean
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 256:
        return obj[:256] + "…[truncated]"
    return obj


def log_event(
    *,
    request_id: Optional[str],
    operation: str,
    success: bool,
    duration_ms: Optional[float] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    error_category: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    record = {
        "requestId": request_id or "unknown",
        "sessionRef": anonymize(session_id) or "none",
        "conversationId": conversation_id or "none",
        "operation": operation,
        "success": success,
        "durationMs": round(duration_ms, 2) if duration_ms is not None else None,
        "errorCategory": error_category or "none",
    }
    if extra:
        record["extra"] = _scrub(extra)
    _logger.info(json.dumps(record, default=str))


class Timer:
    """Context manager measuring elapsed milliseconds."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.duration_ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.duration_ms = (time.perf_counter() - self._start) * 1000.0
