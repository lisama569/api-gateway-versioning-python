import logging

import pytest
from fastapi.testclient import TestClient

from main import app, quota_manager, registry
from app.quotas import QuotaPolicy
from app.versioning import VersionInfo, VersionStatus

logger = logging.getLogger("gateway.tests")

# 测试文件 -> 业务域，用于在单测日志中标注每条用例覆盖的业务。
_AREA_BY_FILE = {
    "test_versioning.py": "版本协商",
    "test_quotas.py": "版本级配额",
    "test_ratelimit.py": "单维限流(基础设施)",
    "test_admin.py": "运行时策略与管理端",
    "test_errors.py": "异常映射与链路标识",
}


def pytest_runtest_logreport(report):
    """逐条用例输出 业务域/用例/结果，便于从日志确认覆盖面与通过情况。"""
    if report.when != "call":
        return
    file_name = report.nodeid.split("::")[0].rsplit("/", 1)[-1]
    area = _AREA_BY_FILE.get(file_name, file_name)
    logger.info("business_area=%s case=%s outcome=%s", area, report.nodeid, report.outcome.upper())

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
