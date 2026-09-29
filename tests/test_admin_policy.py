"""运行时策略管理：查询策略/用量、动态调整额度窗口状态、参数校验。"""
import logging

logger = logging.getLogger("gateway.tests.admin")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def test_get_policy_and_usage(client):
    # 先在 v2 上产生一次调用（v2 limit=3）
    r = client.get("/api/items", headers={"X-API-Version": "v2", "X-Caller-ID": "admin-1"})
    assert r.status_code == 200

    r = client.get("/admin/versions/v2")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "v2"
    assert body["status"] == "deprecated"
    assert body["sunset"] == "2026-12-31"
    assert body["quota"] == {"limit": 3, "window_seconds": 60.0}
    assert body["usage"]["active_callers"] == 1
    caller = body["usage"]["callers"][0]
    assert caller["caller"] == "admin-1"
    assert caller["used"] == 1 and caller["remaining"] == 2
    assert 0 < caller["reset_after_seconds"] <= 60
    _log("admin-get", "查询 v2 当前策略与用量", body["quota"])


def test_get_policy_unknown_version(client):
    r = client.get("/admin/versions/v9")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VERSION_UNKNOWN"
    _log("admin-get-unknown", "查询未知版本", "VERSION_UNKNOWN")


def test_get_policy_includes_disabled(client):
    r = client.get("/admin/versions/v1")
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    _log("admin-get-disabled", "停用版本仍可查询策略", "disabled")


def test_patch_limit_takes_effect_next_request(client):
    # v3 默认 limit=10，收紧到 2
    r = client.patch("/admin/versions/v3/policy", json={"limit": 2})
    assert r.status_code == 200
    assert r.json()["quota"]["limit"] == 2

    codes = [
        client.get("/api/items", headers={"X-Caller-ID": "patch-l"}).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429], "新额度从下一个请求开始生效"

    # 查询接口也反映新策略
    r = client.get("/admin/versions/v3")
    assert r.json()["quota"]["limit"] == 2
    _log("admin-patch-limit", "v3 limit 10->2 即时生效", codes)


def test_patch_window_takes_effect(client):
    r = client.patch("/admin/versions/v3/policy", json={"window_seconds": 30.0})
    assert r.status_code == 200
    assert client.get("/admin/versions/v3").json()["quota"]["window_seconds"] == 30.0
    _log("admin-patch-window", "v3 window 60->30", 30.0)


def test_patch_status_to_disabled_blocks_next_request(client):
    client.patch("/admin/versions/v3/policy", json={"status": "active"})  # 基线
    r = client.patch("/admin/versions/v3/policy", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"

    # 停用后下一请求立刻 410（包括默认版本回落路径）
    assert client.get("/api/items").status_code == 410
    assert client.get(
        "/api/items", headers={"X-API-Version": "v3"}
    ).status_code == 410
    _log("admin-patch-disable", "v3 停用，协商路径立即 410", 410)


def test_patch_invalid_limit_rejected(client):
    r = client.patch("/admin/versions/v3/policy", json={"limit": 0})
    assert r.status_code == 422
    r = client.patch("/admin/versions/v3/policy", json={"window_seconds": -1})
    assert r.status_code == 422
    r = client.patch("/admin/versions/v3/policy", json={"status": "frozen"})
    assert r.status_code == 422
    # 非法调整不得改变现有策略
    assert client.get("/admin/versions/v3").json()["quota"]["limit"] == 10
    _log("admin-invalid", "limit=0 / window<0 / 非法状态 全部 422 且策略不变", 422)


def test_patch_unknown_version_rejected(client):
    r = client.patch("/admin/versions/v9/policy", json={"limit": 5})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VERSION_UNKNOWN"
    _log("admin-patch-unknown", "调整未知版本", "VERSION_UNKNOWN")
