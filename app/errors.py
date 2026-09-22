"""统一对外错误结构与异常映射。"""
from __future__ import annotations

import enum
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.middleware import get_request_id
from app.ratelimit import RateLimitExceeded
from app.versioning import DisabledVersionError, UnknownVersionError

logger = logging.getLogger("gateway.errors")


class ErrorCode(enum.Enum):
    VERSION_UNKNOWN = "VERSION_UNKNOWN"
    VERSION_DISABLED = "VERSION_DISABLED"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BusinessRuleViolation(Exception):
    """业务规则冲突。"""


class DependencyFailure(Exception):
    """底层依赖调用失败。"""


def error_body(code: ErrorCode, message: str, request_id: str) -> dict:
    return {
        "error": {
            "code": code.value,
            "message": message,
            "request_id": request_id,
        }
    }


def _respond(status_code: int, code: ErrorCode, message: str) -> JSONResponse:
    request_id = get_request_id()
    logger.info(
        "request_id=%s mapped_error code=%s status=%s message=%s",
        request_id, code.value, status_code, message,
    )
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, request_id),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把内部异常映射为统一错误结构，原始堆栈不透出。"""

    @app.exception_handler(UnknownVersionError)
    async def _unknown_version(request: Request, exc: UnknownVersionError):
        return _respond(400, ErrorCode.VERSION_UNKNOWN, f"未知接口版本: {exc.requested}")

    @app.exception_handler(DisabledVersionError)
    async def _disabled_version(request: Request, exc: DisabledVersionError):
        return _respond(410, ErrorCode.VERSION_DISABLED, f"接口版本已停用: {exc.requested}")

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        d = exc.decision
        return _respond(429, ErrorCode.RATE_LIMITED, f"调用方 {d.caller} 超出限流额度")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _respond(422, ErrorCode.VALIDATION_FAILED, "请求参数校验失败")

    @app.exception_handler(BusinessRuleViolation)
    async def _business(request: Request, exc: BusinessRuleViolation):
        return _respond(409, ErrorCode.BUSINESS_RULE_VIOLATION, str(exc) or "业务规则冲突")

    @app.exception_handler(DependencyFailure)
    async def _dependency(request: Request, exc: DependencyFailure):
        return _respond(502, ErrorCode.DEPENDENCY_FAILURE, "底层依赖调用失败")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("request_id=%s unhandled_exception", get_request_id())
        return _respond(500, ErrorCode.INTERNAL_ERROR, "服务内部错误")
