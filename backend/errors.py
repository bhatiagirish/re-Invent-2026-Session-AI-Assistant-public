"""Controlled error taxonomy for the backend.

Error CATEGORIES are safe to return to the browser and to log. Underlying
exception details / tokens are never surfaced. See tasks.md section 20.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for controlled application errors.

    `category` is a stable, safe machine code. `http_status` is the response
    status. `client_message` is a user-friendly message with no internals.
    """

    category = "INTERNAL_ERROR"
    http_status = 500
    client_message = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None, *, category: str | None = None,
                 http_status: int | None = None, client_message: str | None = None):
        super().__init__(message or self.__class__.client_message)
        if category:
            self.category = category
        if http_status:
            self.http_status = http_status
        if client_message:
            self.client_message = client_message


# ── Authentication ─────────────────────────────────────────────────────────
class AuthRequiredError(AppError):
    category = "AUTH_REQUIRED"
    http_status = 401
    client_message = "Please sign in to continue."


class AuthFailedError(AppError):
    category = "AUTH_FAILED"
    http_status = 401
    client_message = "Sign-in could not be completed. Please try again."


class TokenExpiredError(AppError):
    category = "TOKEN_EXPIRED"
    http_status = 401
    client_message = "Your session expired. Please sign in again."


class TokenRefreshFailedError(AppError):
    category = "TOKEN_REFRESH_FAILED"
    http_status = 401
    client_message = "Your session expired. Please sign in again."


# ── AWS Events / MCP ─────────────────────────────────────────────────────────
class EventAccessDeniedError(AppError):
    category = "EVENT_ACCESS_DENIED"
    http_status = 403
    client_message = (
        "Your AWS identity is authenticated, but AWS Events indicates that "
        "this account is not registered for re:Invent 2026."
    )


class McpUnavailableError(AppError):
    category = "MCP_UNAVAILABLE"
    http_status = 503
    client_message = "The re:Invent session service is temporarily unavailable."


class McpTimeoutError(AppError):
    category = "MCP_TIMEOUT"
    http_status = 504
    client_message = "That took too long. Please try again."


class McpError(AppError):
    category = "MCP_ERROR"
    http_status = 502
    client_message = "The re:Invent session service returned an error."


# ── Application ───────────────────────────────────────────────────────────
class InvalidRequestError(AppError):
    category = "INVALID_REQUEST"
    http_status = 400
    client_message = "The request was invalid."


class ValidationError(InvalidRequestError):
    """A field-level validation failure caught BEFORE any MCP call.

    `client_message` names the specific violated constraint so the UI can show
    it and retain the owner's entered values (Req 18.3 / 19.3). Carries the
    offending field name for the handler/UI when available.
    """

    category = "VALIDATION_ERROR"
    http_status = 400
    client_message = "The input was invalid."

    def __init__(self, message: str | None = None, *, field: str | None = None,
                 **kwargs):
        # The validation message IS user-facing: name the violated constraint.
        super().__init__(message, client_message=message or self.client_message,
                         **kwargs)
        self.field = field


class InternalError(AppError):
    category = "INTERNAL_ERROR"
    http_status = 500
    client_message = "Something went wrong. Please try again."
