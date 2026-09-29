import logging

import pytest
from fastapi.testclient import TestClient

from main import app, quota_manager, registry
from app.quotas import QuotaPolicy
from app.versioning import VersionInfo, VersionStatus

logger = logging.getLogger("gateway.tests")

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
