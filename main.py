import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import (
    BusinessRuleViolation,
    DependencyFailure,
    register_exception_handlers,
)
from app.middleware import RequestIDMiddleware, get_request_id
from app.ratelimit import RateLimitExceeded, VersionedRateLimiter
from app.versioning import (
    QuotaPolicy,
    VersionInfo,
    VersionRegistry,
    VersionStatus,
)

logger = logging.getLogger("gateway")

app = FastAPI(title="api-gateway-versioning")

VERSION_HEADER = "X-API-Version"
CALLER_HEADER = "X-Caller-ID"
DEPRECATION_HEADER = "X-API-Deprecated"

# 每个仍在服务的版本各有自己的额度与统计窗口：
# 主推版本 v3 额度更宽；已弃用 v2 收紧，推动调用方迁移。
limiter = VersionedRateLimiter()


def _on_status_changed(name: str, old: VersionStatus, new: VersionStatus) -> None:
    if new is VersionStatus.DISABLED:
        # 停用即刻生效：配额器丢弃该版本所有桶，停用后请求不占任何额度。
        limiter.drop_version(name)
    logger.info(
        "request_id=%s version=%s status_changed %s -> %s",
        get_request_id(), name, old.value, new.value,
    )


registry = VersionRegistry(
    versions={
        "v1": VersionInfo("v1", VersionStatus.DISABLED),
        "v2": VersionInfo("v2", VersionStatus.DEPRECATED, sunset="2026-12-31"),
        "v3": VersionInfo("v3", VersionStatus.ACTIVE),
    },
    default="v3",
    quotas={
        "v2": QuotaPolicy(limit=3, window_seconds=60.0),
        "v3": QuotaPolicy(limit=10, window_seconds=60.0),
    },
    default_quota=QuotaPolicy(limit=5, window_seconds=60.0),
    status_listener=_on_status_changed,
)

app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/api/items")
def list_items(request: Request, fail: str | None = None):
    request_id = get_request_id()

    # 协商与配额策略取自同一份快照；协商失败（未知/停用）直接抛错，不扣额度。
    negotiated = registry.negotiate(request.headers.get(VERSION_HEADER))
    caller = request.headers.get(CALLER_HEADER) or "anonymous"
    decision = limiter.check(negotiated, caller)
    logger.info(
        "request_id=%s caller=%s version=%s used_default=%s "
        "quota allowed=%s used=%s/%s remaining=%s",
        request_id, caller, negotiated.info.name, negotiated.used_default,
        decision.allowed, decision.used, decision.limit, decision.remaining,
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
        "quota": {
            "limit": decision.limit,
            "remaining": decision.remaining,
            "used": decision.used,
            "window_seconds": decision.window_seconds,
        },
        "items": [],
        "request_id": request_id,
    }
    headers = {}
    if negotiated.info.status is VersionStatus.DEPRECATED:
        headers[DEPRECATION_HEADER] = "true"
        headers["Sunset"] = negotiated.info.sunset or ""
        payload["deprecation"] = (
            f"版本 {negotiated.info.name} 已弃用，请迁移到 {registry.default}"
        )
    return JSONResponse(content=payload, headers=headers)


class PolicyUpdateBody(BaseModel):
    """运行时策略调整，所有字段可选；至少传一个。"""

    limit: int | None = Field(default=None, gt=0)
    window_seconds: float | None = Field(default=None, gt=0)
    status: VersionStatus | None = None
    sunset: str | None = None


@app.patch("/admin/versions/{name}/policy")
def update_version_policy(name: str, body: PolicyUpdateBody):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        # 空更新无意义，走参数校验错误分类。
        from fastapi.exceptions import RequestValidationError

        raise RequestValidationError([])
    snapshot = registry.update_policy(
        name,
        limit=body.limit,
        window_seconds=body.window_seconds,
        status=body.status,
        sunset=body.sunset,
    )
    return {
        "version": snapshot.info.name,
        "status": snapshot.info.status.value,
        "sunset": snapshot.info.sunset,
        "quota": {
            "limit": snapshot.quota.limit,
            "window_seconds": snapshot.quota.window_seconds,
        },
    }


@app.get("/admin/versions/{name}")
def get_version_policy(name: str):
    snapshot = registry.describe(name)
    usage = limiter.usage(snapshot)
    return {
        "version": snapshot.info.name,
        "status": snapshot.info.status.value,
        "sunset": snapshot.info.sunset,
        "quota": {
            "limit": snapshot.quota.limit,
            "window_seconds": snapshot.quota.window_seconds,
        },
        "usage": {
            "active_callers": usage.active_callers,
            "callers": [
                {
                    "caller": c.caller,
                    "used": c.used,
                    "remaining": c.remaining,
                    "reset_after_seconds": round(
                        max(0.0, c.reset_at - time.monotonic()), 3
                    ),
                }
                for c in usage.callers
            ],
        },
    }
