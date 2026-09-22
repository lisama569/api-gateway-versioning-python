import logging

from fastapi import FastAPI, Request

from app.errors import (
    BusinessRuleViolation,
    DependencyFailure,
    register_exception_handlers,
)
from app.middleware import REQUEST_ID_HEADER, RequestIDMiddleware, get_request_id
from app.ratelimit import RateLimitExceeded, RateLimiter
from app.versioning import VersionInfo, VersionRegistry, VersionStatus

logger = logging.getLogger("gateway")

app = FastAPI(title="api-gateway-versioning")

registry = VersionRegistry(
    versions={
        "v1": VersionInfo("v1", VersionStatus.DISABLED),
        "v2": VersionInfo("v2", VersionStatus.DEPRECATED, sunset="2026-12-31"),
        "v3": VersionInfo("v3", VersionStatus.ACTIVE),
    },
    default="v3",
)
limiter = RateLimiter(limit=5, window_seconds=60.0)

app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)

VERSION_HEADER = "X-API-Version"
CALLER_HEADER = "X-Caller-ID"
DEPRECATION_HEADER = "X-API-Deprecated"


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/api/items")
def list_items(request: Request, fail: str | None = None):
    request_id = get_request_id()

    negotiated = registry.negotiate(request.headers.get(VERSION_HEADER))
    caller = request.headers.get(CALLER_HEADER) or "anonymous"
    decision = limiter.check(caller)
    logger.info(
        "request_id=%s caller=%s version=%s used_default=%s "
        "rate_limit allowed=%s remaining=%s",
        request_id, caller, negotiated.info.name, negotiated.used_default,
        decision.allowed, decision.remaining,
    )
    if not decision.allowed:
        raise RateLimitExceeded(decision)

    if fail == "business":
        raise BusinessRuleViolation("条目状态不允许该操作")
    if fail == "dependency":
        raise DependencyFailure("上游库存服务超时")

    payload = {
        "version": negotiated.info.name,
        "used_default": negotiated.used_default,
        "caller": caller,
        "rate_limit": {"limit": decision.limit, "remaining": decision.remaining},
        "items": [],
        "request_id": request_id,
    }
    from fastapi.responses import JSONResponse

    headers = {}
    if negotiated.info.status is VersionStatus.DEPRECATED:
        headers[DEPRECATION_HEADER] = "true"
        headers["Sunset"] = negotiated.info.sunset or ""
        payload["deprecation"] = (
            f"版本 {negotiated.info.name} 已弃用，请迁移到 {registry.default}"
        )
    return JSONResponse(content=payload, headers=headers)
