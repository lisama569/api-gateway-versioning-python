import logging

import pytest
from fastapi.testclient import TestClient

from main import app, limiter

logger = logging.getLogger("gateway.tests")


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)
