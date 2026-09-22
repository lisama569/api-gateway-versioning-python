"""Version negotiation: missing / unknown / retired / deprecated / active."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, RateLimitConfig
from app.errors import VersionRetiredError, VersionUnknownError
from app.main import create_app
from app.versioning import VersionRegistry, build_default_registry, normalize_version


def make_client() -> TestClient:
    # Generous quota so negotiation behavior is never masked by 429.
    app = create_app(Settings(rate_limit=RateLimitConfig(limit=10_000, window_seconds=60)))
    return TestClient(app)


# ------------------------------------------------------------------ unit tests
def test_normalize_version():
    assert normalize_version(None) is None
    assert normalize_version("   ") is None
    assert normalize_version(" V3 ") == "3"
    assert normalize_version("v2") == "2"
    assert normalize_version("3") == "3"
    assert normalize_version("V10") == "10"


def test_registry_requires_exactly_one_default():
    from app.models import VersionInfo, VersionStatus

    with pytest.raises(ValueError):
        VersionRegistry([VersionInfo(version="1", status=VersionStatus.ACTIVE)])
    with pytest.raises(ValueError):
        VersionRegistry(
            [
                VersionInfo(version="1", status=VersionStatus.ACTIVE, default=True),
                VersionInfo(version="2", status=VersionStatus.ACTIVE, default=True),
            ]
        )


def test_negotiation_unit_level_distinctions():
    reg = build_default_registry()

    missing = reg.negotiate(None)
    assert missing.used_default is True
    assert missing.resolved == "2"  # stable registry default
    assert missing.info.deprecated is True

    explicit = reg.negotiate("v3")
    assert explicit.used_default is False
    assert explicit.resolved == "3"

    with pytest.raises(VersionUnknownError):
        reg.negotiate("v9")
    with pytest.raises(VersionRetiredError):
        reg.negotiate("v1")


# --------------------------------------------------------------- http-level tests
def test_missing_version_uses_default(caplog):
    client = make_client()
    with caplog.at_level(logging.INFO, logger="app.gateway"):
        resp = client.get("/api/ping", headers={"X-Caller-ID": "u", "X-Request-ID": "rid-default"})
    assert resp.status_code == 200
    assert resp.headers["X-API-Version"] == "2"
    # deprecated-but-usable: observable deprecation hints
    assert resp.headers["Deprecation"] == "true"
    assert resp.headers["X-API-Deprecated"] == "true"
    assert resp.headers["X-API-Successor-Version"] == "3"
    assert "deprecated" in resp.headers["Warning"]
    assert resp.json()["version"] == "2"
    decision_logs = [r.message for r in caplog.records if "resolved=2" in r.message]
    assert decision_logs and "used_default=True" in decision_logs[0]
    assert "normalized=None" in decision_logs[0]


def test_unknown_version_rejected_404(caplog):
    client = make_client()
    with caplog.at_level(logging.INFO):
        resp = client.get("/api/ping", headers={"X-API-Version": "v9", "X-Request-ID": "rid-unknown"})
    assert resp.status_code == 404
    body = resp.json()["error"]
    assert body["code"] == "VERSION_UNKNOWN"
    assert body["request_id"] == "rid-unknown"
    assert "9" in body["details"]["known"] or body["details"]["requested"] == "9"
    assert resp.headers["X-Request-ID"] == "rid-unknown"


def test_retired_version_rejected_410():
    client = make_client()
    resp = client.get("/api/ping", headers={"X-API-Version": "v1", "X-Request-ID": "rid-retired"})
    assert resp.status_code == 410
    body = resp.json()["error"]
    assert body["code"] == "VERSION_RETIRED"
    assert body["request_id"] == "rid-retired"
    assert body["details"]["successor"] == "2"


def test_active_version_has_no_deprecation_headers():
    client = make_client()
    resp = client.get("/api/ping", headers={"X-API-Version": "v3", "X-Caller-ID": "u2"})
    assert resp.status_code == 200
    assert resp.headers["X-API-Version"] == "3"
    assert "Deprecation" not in resp.headers
    assert "Warning" not in resp.headers


def test_blank_header_is_treated_as_missing():
    client = make_client()
    resp = client.get("/api/ping", headers={"X-API-Version": "   "})
    assert resp.status_code == 200
    assert resp.headers["X-API-Version"] == "2"


def test_request_id_generated_when_absent_and_echoed_when_present():
    client = make_client()
    resp = client.get("/api/ping")
    generated = resp.headers["X-Request-ID"]
    assert len(generated) >= 16
    resp2 = client.get("/api/ping", headers={"X-Request-ID": "corr-42"})
    assert resp2.headers["X-Request-ID"] == "corr-42"
