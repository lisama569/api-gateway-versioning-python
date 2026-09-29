import logging

import pytest
from fastapi.testclient import TestClient

from main import app, limiter, registry

logger = logging.getLogger("gateway.tests")


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    # 保存注册表（版本状态/配额）与配额桶，测试后还原，避免运行时
    # 策略调整类用例污染其他用例。
    versions, quotas = registry.snapshot_state()
    limiter.reset()
    yield
    registry.restore_state(versions, quotas)
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)
