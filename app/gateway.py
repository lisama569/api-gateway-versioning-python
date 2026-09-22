"""Gateway middleware: request id, version negotiation, rate limiting.

Pipeline for requests under ``/api``:

1. resolve/assign ``X-Request-ID`` (correlation id);
2. resolve caller from ``X-Caller-ID`` (defaults to ``"anonymous"``);
3. version negotiation via ``X-API-Version``
   (missing -> default, unknown -> 404, retired -> 410);
4. fixed-window quota check keyed by ``(caller, version)``
   (exceeded -> 429, never queued);
5. attach ``RequestContext`` (visible to logs/handlers), call the app;
6. annotate the response with correlation, quota and deprecation headers.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from email.utils import formatdate

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.context import RequestContext, new_request_id, reset_context, set_context
from app.errors import (
    GatewayError,
    gateway_error_response,
    unhandled_error_response,
    validation_error_response,
)
from app.ratelimit import RateLimiter
from app.versioning import VersionRegistry, normalize_version

logger = logging.getLogger("app.gateway")

API_PREFIX = "/api"
MAX_REQUEST_ID_LEN = 128


def _http_date_from_iso(iso_day: str) -> str | None:
    try:
        day = date.fromisoformat(iso_day)
    except ValueError:
        return None
    return formatdate(datetime(day.year, day.month, day.day).timestamp(), usegmt=True)


def _is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


class Gateway:
    def __init__(
        self,
        registry: VersionRegistry,
        settings: Settings,
        limiter: RateLimiter | None = None,
    ):
        self.registry = registry
        self.settings = settings
        self.limiter = limiter or RateLimiter(
            limit=settings.rate_limit.limit,
            window_seconds=settings.rate_limit.window_seconds,
        )

    # ------------------------------------------------------------------ hooks
    def install(self, app: FastAPI) -> None:
        app.add_exception_handler(GatewayError, self._handle_gateway_error)
        app.add_exception_handler(RequestValidationError, self._handle_validation_error)
        app.add_exception_handler(Exception, self._handle_unhandled)
        app.middleware("http")(self._dispatch)

    async def _handle_gateway_error(self, request: Request, exc: GatewayError) -> Response:
        status, body = gateway_error_response(exc, request)
        return JSONResponse(status_code=status, content=body)

    async def _handle_validation_error(
        self, request: Request, exc: RequestValidationError
    ) -> Response:
        status, body = validation_error_response(exc, request)
        return JSONResponse(status_code=status, content=body)

    async def _handle_unhandled(self, request: Request, exc: Exception) -> Response:
        status, body = unhandled_error_response(exc, request)
        return JSONResponse(status_code=status, content=body)

    # ------------------------------------------------------------- middleware
    async def _dispatch(self, request: Request, call_next) -> Response:
        request_id = self._resolve_request_id(request)
        path = request.url.path

        if not _is_api_path(path):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response

        caller = request.headers.get("x-caller-id", "").strip() or "anonymous"
        raw_version = request.headers.get("x-api-version")
        normalized = normalize_version(raw_version)

        token = None
        try:
            # Context is established before negotiation so that even the
            # 404/410 error paths carry a correlation id in body and logs.
            placeholder = RequestContext(
                request_id=request_id, caller=caller, version=normalized or ""
            )
            token = set_context(placeholder)
            try:
                negotiation = self.registry.negotiate(raw_version)
            except GatewayError as exc:
                logger.info(
                    "request_id=%s caller=%s version_header=%r normalized=%r "
                    "decision=rejected code=%s reason=%s",
                    request_id,
                    caller,
                    raw_version,
                    normalized,
                    exc.code.value,
                    exc.message,
                )
                status, body = gateway_error_response(exc, request)
                response = JSONResponse(status_code=status, content=body)
                self._add_correlation_headers(response, request_id, None)
                return response

            version = negotiation.resolved
            ctx = RequestContext(request_id=request_id, caller=caller, version=version)
            reset_context(token)
            token = set_context(ctx)

            allowed, remaining, limit, retry_after = self.limiter.check(caller, version)
            logger.info(
                "request_id=%s caller=%s version_header=%r normalized=%r "
                "resolved=%s used_default=%s allowed=%s remaining=%s/%s reason=%s",
                request_id,
                caller,
                raw_version,
                normalized,
                version,
                negotiation.used_default,
                allowed,
                remaining,
                limit,
                negotiation.reason,
            )
            if not allowed:
                from app.errors import RateLimitExceededError

                exc = RateLimitExceededError(
                    details={
                        "caller": caller,
                        "version": version,
                        "limit": limit,
                        "retry_after_seconds": math.ceil(retry_after),
                    },
                )
                status, body = gateway_error_response(exc, request)
                response = JSONResponse(status_code=status, content=body)
                response.headers["Retry-After"] = str(math.ceil(retry_after))
                response.headers["X-RateLimit-Limit"] = str(limit)
                response.headers["X-RateLimit-Remaining"] = "0"
                self._add_correlation_headers(response, request_id, version)
                self._add_deprecation_headers(response, negotiation)
                return response

            try:
                response = await call_next(request)
            except GatewayError as exc:
                status, body = gateway_error_response(exc, request)
                response = JSONResponse(status_code=status, content=body)
            except Exception as exc:  # noqa: BLE001 - final safety net
                status, body = unhandled_error_response(exc, request)
                response = JSONResponse(status_code=status, content=body)
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            self._add_correlation_headers(response, request_id, version)
            self._add_deprecation_headers(response, negotiation)
            return response
        finally:
            if token is not None:
                reset_context(token)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _resolve_request_id(request: Request) -> str:
        provided = request.headers.get("x-request-id", "").strip()
        if provided:
            return provided[:MAX_REQUEST_ID_LEN]
        return new_request_id()

    @staticmethod
    def _add_correlation_headers(response: Response, request_id: str, version: str | None) -> None:
        response.headers["X-Request-ID"] = request_id
        if version is not None:
            response.headers["X-API-Version"] = version

    @staticmethod
    def _add_deprecation_headers(response: Response, negotiation) -> None:
        info = negotiation.info
        if not info.deprecated:
            return
        response.headers["Deprecation"] = "true"
        response.headers["X-API-Deprecated"] = "true"
        warning = f'299 - "API version {info.version} is deprecated'
        if info.successor:
            warning += f"; please upgrade to version {info.successor}"
            response.headers["X-API-Successor-Version"] = info.successor
        warning += '"'
        response.headers["Warning"] = warning
        if info.sunset:
            sunset_http = _http_date_from_iso(info.sunset)
            if sunset_http:
                response.headers["Sunset"] = sunset_http
