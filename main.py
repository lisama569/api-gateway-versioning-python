import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import (
    BusinessRuleViolation,
    DependencyFailure,
    register_exception_handlers,
)
from app.middleware import REQUEST_ID_HEADER, RequestIDMiddleware, get_request_id
from app.quotas import (
    QuotaExceeded,
    QuotaPolicy,
    VersionQuotaManager,
)
from app.versioning import (
    UnknownVersionError,
    VersionInfo,
    VersionRegistry,
    VersionStatus,
)

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
# 每个仍在服务（及停用但保留可观测性）的版本各自一套额度与窗口：
# 主推版本 v3 放宽，弃用版本 v2 收紧以推动迁移。
quota_manager = VersionQuotaManager(
    {
        "v1": QuotaPolicy(limit=1, window_seconds=60.0),
        "v2": QuotaPolicy(limit=2, window_seconds=60.0),
        "v3": QuotaPolicy(limit=5, window_seconds=60.0),
    }
)
# 兼容旧代码/测试中的 limiter 名字。
limiter = quota_manager

app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)

VERSION_HEADER = "X-API-Version"
CALLER_HEADER = "X-Caller-ID"
DEPRECATION_HEADER = "X-API-Deprecated"


class VersionPolicyUpdate(BaseModel):
    limit: int | None = None
    window_seconds: float | None = None
    status: VersionStatus | None = None


def _version_admin_view(name: str) -> dict:
    info = registry.get(name)
    if info is None:
        raise UnknownVersionError(name)
    now = time.monotonic()
    snapshot = quota_manager.usage(name, now=now)
    return {
        "version": info.name,
        "default": registry.default == info.name,
        "status": info.status.value,
        "sunset": info.sunset,
        "policy": {"limit": snapshot.limit, "window_seconds": snapshot.window_seconds},
        "callers": [
            {
                "caller": usage.caller,
                "used": usage.used,
                "remaining": usage.remaining,
                "window_seconds": usage.window_seconds,
                "reset_in_seconds": round(max(0.0, usage.reset_at - now), 3),
            }
            for usage in sorted(snapshot.callers.values(), key=lambda u: u.caller)
        ],
    }


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/api/items")
def list_items(request: Request, fail: str | None = None):
    request_id = get_request_id()

    # 版本协商先于配额判定：未声明/未知/停用/弃用的判定逻辑保持不变，
    # 协商失败（未知/停用）不消耗任何版本的额度；停用版本在运行时切换后
    # 从下一个请求起立即在此被拒。
    negotiated = registry.negotiate(request.headers.get(VERSION_HEADER))
    caller = request.headers.get(CALLER_HEADER) or "anonymous"
    version_name = negotiated.info.name
    decision = quota_manager.check(caller, version_name)
    logger.info(
        "request_id=%s caller=%s version=%s used_default=%s "
        "quota allowed=%s limit=%s remaining=%s retry_after=%.3f",
        request_id, caller, version_name, negotiated.used_default,
        decision.allowed, decision.limit, decision.remaining, decision.retry_after,
    )
    if not decision.allowed:
        raise QuotaExceeded(decision)

    if fail == "business":
        raise BusinessRuleViolation("条目状态不允许该操作")
    if fail == "dependency":
        raise DependencyFailure("上游库存服务超时")

    payload = {
        "version": negotiated.info.name,
        "used_default": negotiated.used_default,
        "caller": caller,
        "rate_limit": {
            "version": decision.version,
            "limit": decision.limit,
            "remaining": decision.remaining,
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


@app.get("/admin/versions/{name}")
def get_version_policy(name: str):
    """查看某版本当前生效的状态、配额策略与各调用方用量。"""
    return _version_admin_view(name)


@app.patch("/admin/versions/{name}")
def update_version_policy(name: str, update: VersionPolicyUpdate):
    """运行时调整版本额度/窗口/状态，无需重启；从下一个请求起生效。"""
    info = registry.get(name)
    if info is None:
        raise UnknownVersionError(name)

    current = quota_manager.usage(name)
    new_policy = QuotaPolicy(
        limit=update.limit if update.limit is not None else current.limit,
        window_seconds=(
            update.window_seconds
            if update.window_seconds is not None
            else current.window_seconds
        ),
    )

    if update.status is VersionStatus.DISABLED:
        # 先关版本再换策略：切换瞬间之后的请求在协商阶段即被 410 拒绝，
        # 不会有请求带着新策略进入配额判定。
        registry.set_status(name, VersionStatus.DISABLED)
        quota_manager.update_policy(name, new_policy)
    else:
        # 重新启用或仅改额度：策略先就位，状态最后放开。
        quota_manager.update_policy(name, new_policy)
        if update.status is not None:
            registry.set_status(name, update.status)
    logger.info(
        "request_id=%s version_policy_updated version=%s limit=%s window=%s status=%s",
        get_request_id(), name, new_policy.limit, new_policy.window_seconds,
        (update.status or info.status).value,
    )
    return _version_admin_view(name)
