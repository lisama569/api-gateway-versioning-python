"""Gateway exception hierarchy and unified error mapping."""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError

from app.context import get_context
from app.models import ErrorCode

logger = logging.getLogger("app.gateway")


class GatewayError(Exception):
    """Base class for all gateway-mapped errors."""

    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    safe_message: str = "internal server error"

    def __init__(self, message: str | None = None, *, details: dict | None = None):
        super().__init__(message or self.safe_message)
        self.message = message or self.safe_message
        self.details = details


class VersionUnknownError(GatewayError):
    status_code = 404
    code = ErrorCode.VERSION_UNKNOWN
    safe_message = "unknown api version"


class VersionRetiredError(GatewayError):
    status_code = 410
    code = ErrorCode.VERSION_RETIRED
    safe_message = "api version retired"


class RateLimitExceededError(GatewayError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    safe_message = "rate limit exceeded"


class UpstreamError(GatewayError):
    """Raised by business logic when a downstream dependency fails."""

    status_code = 502
    code = ErrorCode.UPSTREAM_ERROR
    safe_message = "upstream dependency failure"


def _request_id_from(request: Request | None) -> str:
    ctx = get_context()
    if ctx is not None:
        return ctx.request_id
    if request is not None:
        rid = request.headers.get("x-request-id")
        if rid:
            return rid
    return "unknown"


def _body(code: ErrorCode, message: str, request_id: str, details: dict | None) -> dict:
    body: dict = {
        "error": {
            "code": code.value,
            "message": message,
            "request_id": request_id,
        }
    }
    if details:
        body["error"]["details"] = details
    return body


def gateway_error_response(exc: GatewayError, request: Request | None = None) -> tuple[int, dict]:
    request_id = _request_id_from(request)
    # Log full detail server-side; expose only the safe message externally.
    logger.warning(
        "gateway error request_id=%s code=%s message=%s details=%s",
        request_id,
        exc.code.value,
        exc.message,
        exc.details,
    )
    return exc.status_code, _body(exc.code, exc.safe_message, request_id, exc.details)


def validation_error_response(exc: RequestValidationError, request: Request | None) -> tuple[int, dict]:
    request_id = _request_id_from(request)
    normalized = [
        {
            "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
            "message": err.get("msg", "invalid value"),
            "type": err.get("type", "value_error"),
        }
        for err in exc.errors()
    ]
    logger.warning(
        "validation error request_id=%s errors=%s", request_id, normalized
    )
    return 422, _body(
        ErrorCode.VALIDATION_ERROR,
        "request validation failed",
        request_id,
        {"errors": normalized},
    )


def unhandled_error_response(exc: Exception, request: Request | None = None) -> tuple[int, dict]:
    request_id = _request_id_from(request)
    # Full stacktrace stays in server logs only.
    logger.exception("unhandled error request_id=%s: %s", request_id, exc)
    return 500, _body(
        ErrorCode.INTERNAL_ERROR,
        "internal server error",
        request_id,
        None,
    )
