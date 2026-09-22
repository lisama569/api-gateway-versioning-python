"""Unified error structure, correlation ids, and exception mapping."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.config import Settings, RateLimitConfig
from app.main import create_app


def make_client(limit: int = 10_000, raise_server_exceptions: bool = True) -> TestClient:
    app = create_app(Settings(rate_limit=RateLimitConfig(limit=limit, window_seconds=60)))
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _assert_unified_shape(body: dict, expected_code: str, request_id: str):
    assert set(body) == {"error"}
    err = body["error"]
    assert err["code"] == expected_code
    assert err["request_id"] == request_id
    assert isinstance(err["message"], str) and err["message"]
    return err


def test_business_upstream_error_maps_to_502_without_leak(caplog):
    client = make_client()
    with caplog.at_level(logging.WARNING, logger="app.gateway"):
        resp = client.get(
            "/api/upstream-fail",
            headers={"X-API-Version": "v3", "X-Request-ID": "rid-upstream"},
        )
    assert resp.status_code == 502
    err = _assert_unified_shape(resp.json(), "UPSTREAM_ERROR", "rid-upstream")
    # External body must not expose the raw ConnectionError / network address.
    assert "10.0.0.9" not in str(err)
    assert "cart-service" not in str(err)
    # Server-side log keeps the diagnostic detail and the correlation id.
    log_blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "rid-upstream" in log_blob
    assert "UPSTREAM_ERROR" in log_blob


def test_unexpected_error_maps_to_500_without_traceback(caplog):
    client = make_client(raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        resp = client.get(
            "/api/boom",
            headers={"X-API-Version": "v3", "X-Request-ID": "rid-boom"},
        )
    assert resp.status_code == 500
    err = _assert_unified_shape(resp.json(), "INTERNAL_ERROR", "rid-boom")
    assert err["message"] == "internal server error"
    serialized = str(resp.json())
    assert "TOP-SECRET-KEY" not in serialized
    assert "Traceback" not in serialized
    # Full diagnostic stays server-side and remains correlated.
    log_blob = "\n".join(
        r.getMessage() + "\n" + (r.exc_text or "") for r in caplog.records
    )
    assert "rid-boom" in log_blob
    assert "TOP-SECRET-KEY" in log_blob  # logged for operators, never returned


def test_body_validation_error_maps_to_422():
    client = make_client()
    resp = client.post(
        "/api/echo",
        json={"wrong": 1},
        headers={"X-Request-ID": "rid-422-body"},
    )
    assert resp.status_code == 422
    err = _assert_unified_shape(resp.json(), "VALIDATION_ERROR", "rid-422-body")
    fields = [e["field"] for e in err["details"]["errors"]]
    assert "message" in fields
    # no raw pydantic internals
    assert all("ctx" not in e for e in err["details"]["errors"])


def test_query_validation_error_maps_to_422():
    client = make_client()
    resp = client.get(
        "/api/items?limit=999",
        headers={"X-Request-ID": "rid-422-query"},
    )
    assert resp.status_code == 422
    err = _assert_unified_shape(resp.json(), "VALIDATION_ERROR", "rid-422-query")
    assert err["details"]["errors"][0]["field"] == "query.limit"


def test_malformed_json_body_maps_to_422():
    client = make_client()
    resp = client.post(
        "/api/echo",
        content="{not-json",
        headers={"Content-Type": "application/json", "X-Request-ID": "rid-422-json"},
    )
    assert resp.status_code == 422
    err = _assert_unified_shape(resp.json(), "VALIDATION_ERROR", "rid-422-json")
    assert err["code"] == "VALIDATION_ERROR"


def test_version_errors_are_distinct_types():
    client = make_client()
    r1 = client.get("/api/ping", headers={"X-API-Version": "v9", "X-Request-ID": "r1"})
    r2 = client.get("/api/ping", headers={"X-API-Version": "v1", "X-Request-ID": "r2"})
    assert (r1.status_code, r1.json()["error"]["code"]) == (404, "VERSION_UNKNOWN")
    assert (r2.status_code, r2.json()["error"]["code"]) == (410, "VERSION_RETIRED")


def test_rate_limited_error_shape(caplog):
    client = make_client(limit=1)
    client.get("/api/ping", headers={"X-Caller-ID": "c", "X-Request-ID": "r-ok"})
    with caplog.at_level(logging.INFO):
        resp = client.get(
            "/api/ping",
            headers={"X-Caller-ID": "c", "X-Request-ID": "r-limited"},
        )
    assert resp.status_code == 429
    err = _assert_unified_shape(resp.json(), "RATE_LIMITED", "r-limited")
    assert err["details"]["retry_after_seconds"] > 0
    assert resp.headers["Retry-After"] == str(err["details"]["retry_after_seconds"])
    decision = [r.getMessage() for r in caplog.records if "r-limited" in r.getMessage()]
    assert decision and "allowed=False" in decision[0]


def test_request_id_consistency_body_and_header_on_every_error():
    client = make_client(raise_server_exceptions=False)
    for url, rid in (
        ("/api/ping", "z1"),
        ("/api/ping", "z2"),
        ("/api/boom", "z3"),
    ):
        version = "v9" if rid == "z1" else "v1" if rid == "z2" else "v3"
        resp = client.get(url, headers={"X-API-Version": version, "X-Request-ID": rid})
        assert resp.headers["X-Request-ID"] == rid
        assert resp.json()["error"]["request_id"] == rid
