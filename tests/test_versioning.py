"""版本协商场景：默认版本、未知版本、停用版本、弃用版本。"""
import logging

logger = logging.getLogger("gateway.tests.versioning")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def test_default_version_is_stable(client):
    r1 = client.get("/api/items")
    r2 = client.get("/api/items")
    assert r1.status_code == 200
    body = r1.json()
    assert body["version"] == "v3" and body["used_default"] is True
    assert r2.json()["version"] == body["version"], "默认版本选择必须稳定"
    _log(body["request_id"], "未声明版本，回落默认版本", body["version"])


def test_unknown_version_rejected(client):
    r = client.get("/api/items", headers={"X-API-Version": "v9"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VERSION_UNKNOWN"
    _log(err["request_id"], "请求版本 v9 不在注册表", err["code"])


def test_disabled_version_rejected(client):
    r = client.get("/api/items", headers={"X-API-Version": "v1"})
    assert r.status_code == 410
    err = r.json()["error"]
    assert err["code"] == "VERSION_DISABLED"
    _log(err["request_id"], "v1 状态为 disabled", err["code"])


def test_deprecated_version_served_with_observable_hint(client):
    r = client.get("/api/items", headers={"X-API-Version": "v2"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "v2"
    assert r.headers["X-API-Deprecated"] == "true"
    assert r.headers["Sunset"] == "2026-12-31"
    assert "deprecation" in body
    _log(body["request_id"], "v2 状态为 deprecated，仍可用", body["deprecation"])


def test_active_version_has_no_deprecation_hint(client):
    r = client.get("/api/items", headers={"X-API-Version": "v3"})
    assert r.status_code == 200
    assert "X-API-Deprecated" not in r.headers
    assert "deprecation" not in r.json()
    _log(r.json()["request_id"], "v3 状态为 active", "无弃用提示")


def test_unknown_and_disabled_do_not_consume_quota(client):
    for _ in range(10):
        client.get("/api/items", headers={"X-API-Version": "v9", "X-Caller-ID": "v-tester"})
        client.get("/api/items", headers={"X-API-Version": "v1", "X-Caller-ID": "v-tester"})
    r = client.get("/api/items", headers={"X-Caller-ID": "v-tester"})
    assert r.status_code == 200, "协商失败的请求不得消耗限流额度"
    _log(r.json()["request_id"], "版本协商失败先于限流判定", r.status_code)
