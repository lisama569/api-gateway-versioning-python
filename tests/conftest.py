import logging

import pytest
from fastapi.testclient import TestClient

from main import app, quota_manager, registry
from app.quotas import QuotaPolicy
from app.versioning import VersionInfo, VersionStatus

logger = logging.getLogger("gateway.tests")

# 静音 HTTPX 测试客户端逐条请求日志，突出业务判定与用例结果。
logging.getLogger("httpx").setLevel(logging.WARNING)

# 初始版本表与配额策略，测试间恢复用。
_INITIAL_VERSIONS = {
    "v1": VersionInfo("v1", VersionStatus.DISABLED),
    "v2": VersionInfo("v2", VersionStatus.DEPRECATED, sunset="2026-12-31"),
    "v3": VersionInfo("v3", VersionStatus.ACTIVE),
}
_INITIAL_POLICIES = {
    "v1": QuotaPolicy(limit=1, window_seconds=60.0),
    "v2": QuotaPolicy(limit=2, window_seconds=60.0),
    "v3": QuotaPolicy(limit=5, window_seconds=60.0),
}


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    yield
    # 恢复配额桶、策略与版本状态，避免运行时策略类用例污染其它用例。
    quota_manager.reset()
    for name, policy in _INITIAL_POLICIES.items():
        quota_manager.update_policy(name, policy)
    for name, info in _INITIAL_VERSIONS.items():
        registry.set_status(name, info.status)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


# 测试文件 -> 业务域：让每条用例开始时都能在日志里看出覆盖的是哪块业务。
_BUSINESS_BY_FILE = {
    "test_versioning.py": "版本协商",
    "test_ratelimit.py": "调用方限流（旧组件）/ 网关配额",
    "test_quotas.py": "版本级配额",
    "test_admin.py": "运行时策略调整与管理端",
    "test_errors.py": "统一异常映射与链路标识",
}


def pytest_runtest_logstart(nodeid, location):
    path = location[0]
    fname = path.rsplit("/", 1)[-1]
    business = _BUSINESS_BY_FILE.get(fname, "其它")
    logger.info(">>> [业务域:%s] 开始用例 %s", business, nodeid)


def pytest_runtest_logreport(report):
    if report.when == "call":
        outcome = "通过" if report.passed else "失败" if report.failed else "跳过"
        logger.info(
            "<<< 用例结束 %s -> %s (%.3fs)",
            report.nodeid,
            outcome,
            report.duration,
        )
