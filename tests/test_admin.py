"""HTTP 层：拒绝依据与恢复信息、运行时策略调整、停用即时生效、管理端可观测。"""
import logging

logger = logging.getLogger("gateway.tests.admin")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def test_429_exposes_basis_and_recovery_matching_window(client):
    headers = {"X-Caller-ID": "adm-429", "X-API-Version": "v2"}  # v2 额度 2/60s
    client.get("/api/items", headers=headers)
    client.get("/api/items", headers=headers)
    r = client.get("/api/items", headers=headers)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "RATE_LIMITED"
    d = err["details"]
    assert d["caller"] == "adm-429" and d["version"] == "v2"
    assert d["limit"] == 2 and d["remaining"] == 0
    assert d["window_seconds"] == 60.0
    # 恢复时间是“当前窗口剩余时间”，窗口 60s 且请求都在窗口起点附近发出
    assert 59.0 <= d["retry_after_seconds"] <= 60.0, d["retry_after_seconds"]
    # Retry-After 为向上取整的秒数，且绝不用下一窗口起点（120s 后）误导
    assert 59 <= int(r.headers["Retry-After"]) <= 60
    _log(err["request_id"], "v2 第 3 次请求超配额", d)


def test_runtime_limit_tightening_takes_effect_next_request(client):
    r = client.patch("/admin/versions/v3", json={"limit": 1})
    assert r.status_code == 200 and r.json()["policy"]["limit"] == 1
    h = {"X-Caller-ID": "adm-tighten"}
    assert client.get("/api/items", headers=h).status_code == 200
    second = client.get("/api/items", headers=h)
    assert second.status_code == 429
    body = second.json()["error"]["details"]
    assert body["limit"] == 1 and body["remaining"] == 0
    _log("adm-tighten", "运行时把 v3 收到 1", body)


def test_limit_change_does_not_forget_consumed_quota(client):
    h = {"X-Caller-ID": "adm-keep"}
    for _ in range(3):  # v3 默认 5
        assert client.get("/api/items", headers=h).status_code == 200
    client.patch("/admin/versions/v3", json={"limit": 5})  # 同值，用量保留
    client.patch("/admin/versions/v3", json={"limit": 2})  # 已用 3 > 2，立即拒绝
    r = client.get("/api/items", headers=h)
    assert r.status_code == 429
    assert r.json()["error"]["details"]["remaining"] == 0
    _log(r.json()["error"]["request_id"], "收紧到 2 时已用 3 不清零", "429")


def test_runtime_disable_rejects_immediately_and_releases_quota(client):
    h = {"X-Caller-ID": "adm-disable", "X-API-Version": "v2"}
    assert client.get("/api/items", headers=h).status_code == 200
    view_before = client.get("/admin/versions/v2").json()
    assert view_before["callers"][0]["used"] == 1
    r = client.patch("/admin/versions/v2", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    # 停用即释放：累计用量与调用方桶立即清空，停用版本不再占任何额度
    assert r.json()["callers"] == []
    assert client.get("/admin/versions/v2").json()["callers"] == []
    # 停用后立刻 410
    blocked = client.get("/api/items", headers=h)
    assert blocked.status_code == 410
    assert blocked.json()["error"]["code"] == "VERSION_DISABLED"
    # 重新启用：从干净状态开始，v2 的 2 个额度完整可用，不被历史用量限流
    client.patch("/admin/versions/v2", json={"status": "deprecated"})
    ok1 = client.get("/api/items", headers=h)
    ok2 = client.get("/api/items", headers=h)
    again = client.get("/api/items", headers=h)
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert again.status_code == 429, "重新启用后按全新 2 个额度计数，第 3 次拒绝"
    _log(blocked.json()["error"]["request_id"], "停用 v2：即时 410 且清空用量；启用后全新 2 额度", "410->200->200->429")


def test_runtime_window_change_recovery_reflects_new_window(client):
    import time

    h = {"X-Caller-ID": "adm-window", "X-API-Version": "v2"}
    # v2 原 60s 窗口，用满后收紧窗口到 1s
    client.get("/api/items", headers=h)
    client.get("/api/items", headers=h)
    denied_before = client.get("/api/items", headers=h)
    assert denied_before.status_code == 429
    before = denied_before.json()["error"]["details"]
    assert before["window_seconds"] == 60.0
    assert 59.0 <= before["retry_after_seconds"] <= 60.0
    # 换窗口本身不清零：请求立刻进来时新窗口尚未走完，仍拒绝，
    # 但恢复依据已经换成新窗口（reset_in 约 1s，绝不再报 60s）。
    client.patch("/admin/versions/v2", json={"window_seconds": 1.0, "limit": 2})
    denied_after = client.get("/api/items", headers=h)
    assert denied_after.status_code == 429
    after = denied_after.json()["error"]["details"]
    assert after["window_seconds"] == 1.0 and after["remaining"] == 0
    assert 0.0 < after["retry_after_seconds"] <= 1.0
    assert int(denied_after.headers["Retry-After"]) == 1
    # 等当前（新）窗口真正走完后，从下一请求起恢复
    time.sleep(1.05)
    ok = client.get("/api/items", headers=h)
    assert ok.status_code == 200
    assert ok.json()["rate_limit"]["window_seconds"] == 1.0
    _log("adm-window", "窗口 60->1：不清零，恢复信息跟随当前窗口", after)


def test_admin_get_reports_policy_and_per_caller_usage(client):
    h = {"X-Caller-ID": "adm-view", "X-API-Version": "v2"}
    client.get("/api/items", headers=h)
    r = client.get("/admin/versions/v2")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "v2" and body["status"] == "deprecated"
    assert body["policy"] == {"limit": 2, "window_seconds": 60.0}
    entry = next(c for c in body["callers"] if c["caller"] == "adm-view")
    assert entry["used"] == 1 and entry["remaining"] == 1
    assert 0 < entry["reset_in_seconds"] <= 60.0
    # 未知版本走 VERSION_UNKNOWN
    assert client.get("/admin/versions/v9").status_code == 400
    _log("admin-view", "管理端可见 v2 策略与 adm-view 用量", entry)


def test_invalid_limit_and_window_return_validation_error_not_500(client):
    # 0 与负数都属于调用方把参数写错：必须按参数校验失败返回，
    # 不能走到服务内部错误，也不能透出任何内部异常细节。
    cases = [
        {"limit": 0},
        {"limit": -1},
        {"window_seconds": 0},
        {"window_seconds": -0.5},
        {"limit": 0, "window_seconds": 10},
        {"limit": 1, "window_seconds": -10},
        {"limit": "abc"},
        {"window_seconds": "abc"},
        {"status": "paused"},  # 状态枚举之外的值
    ]
    for i, body in enumerate(cases):
        rid = f"adm-invalid-{i}"
        r = client.patch(
            "/admin/versions/v3", json=body, headers={"X-Request-ID": rid}
        )
        assert r.status_code == 422, body
        err = r.json()["error"]
        assert err["code"] == "VALIDATION_FAILED", body
        # 链路标识与其它错误路径一致：回传头与错误体都用调用方标识
        assert r.headers["X-Request-ID"] == rid, body
        assert err["request_id"] == rid, body
        text = r.text
        for marker in ("Traceback", "ValueError", "must be positive"):
            assert marker not in text, f"内部细节不得透出: {marker}"
    _log("adm-invalid", f"{len(cases)} 类非法调整请求全部 422", "VALIDATION_FAILED")


def test_empty_body_and_unknown_field_are_rejected(client):
    # 空请求体：没有任何调整意图，不能默默当成功
    r = client.patch("/admin/versions/v3", json={}, headers={"X-Request-ID": "adm-empty"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert r.headers["X-Request-ID"] == "adm-empty"
    # 字段名写错：必须按参数错误提示调用方，而不是静默忽略返回 200
    r = client.patch("/admin/versions/v3", json={"limt": 3})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    # 请求体为 null / 根本不是 JSON 同样是参数错误
    assert client.patch("/admin/versions/v3", json=None).status_code == 422
    r = client.patch(
        "/admin/versions/v3",
        content=b"{oops",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    _log("adm-empty", "空体/错字段名/null/坏 JSON 全部拒绝", "422")


def test_failed_update_is_atomic_status_policy_and_usage_unchanged(client):
    # 同时要求停用又把额度写成非法值：请求必须失败，且版本状态、
    # 额度、窗口与已累计用量全部跟没调过一样。
    h = {"X-Caller-ID": "adm-atomic", "X-API-Version": "v3"}
    for _ in range(2):
        assert client.get("/api/items", headers=h).status_code == 200

    before = client.get("/admin/versions/v3").json()
    r = client.patch(
        "/admin/versions/v3",
        json={"status": "disabled", "limit": -5},
        headers={"X-Request-ID": "adm-atomic-rid"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["request_id"] == "adm-atomic-rid"

    after = client.get("/admin/versions/v3").json()
    assert after["status"] == "active", "失败请求不得把版本真的停用"
    assert after["policy"] == before["policy"], "失败请求不得改动额度/窗口"
    used = next(c for c in after["callers"] if c["caller"] == "adm-atomic")["used"]
    assert used == 2, "失败请求不得影响已累计用量"
    # 版本仍在正常服务：剩余额度按原策略继续计数（5 - 2 = 3）
    assert client.get("/api/items", headers=h).status_code == 200

    # 窗口非法 + 停用组合同样原子
    r = client.patch(
        "/admin/versions/v3",
        json={"status": "disabled", "window_seconds": 0},
    )
    assert r.status_code == 422
    assert client.get("/admin/versions/v3").json()["status"] == "active"
    # 错字段名 + 合法状态同样不能让状态落地
    r = client.patch(
        "/admin/versions/v3", json={"status": "disabled", "limt": 1}
    )
    assert r.status_code == 422
    assert client.get("/admin/versions/v3").json()["status"] == "active"
    _log("adm-atomic-rid", "停用+非法值失败后状态/策略/用量全部不变", after["policy"])


def test_disable_clears_usage_and_reenable_starts_clean_even_with_policy_change(client):
    h = {"X-Caller-ID": "adm-reset", "X-API-Version": "v2"}
    for _ in range(2):
        assert client.get("/api/items", headers=h).status_code == 200
    assert client.get("/api/items", headers=h).status_code == 429

    # 停用后：立即 410，且管理端看不到任何历史调用方用量
    client.patch("/admin/versions/v2", json={"status": "disabled"})
    assert client.get("/api/items", headers=h).status_code == 410
    assert client.get("/admin/versions/v2").json()["callers"] == []

    # 停用期间调整额度，再启用：新额度生效，且从干净桶开始（4 个全新额度）
    client.patch("/admin/versions/v2", json={"limit": 4})
    client.patch("/admin/versions/v2", json={"status": "deprecated"})
    codes = [client.get("/api/items", headers=h).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 429], codes

    # 再次停用/启用：上一轮的 4 次用量不跨停用周期保留
    client.patch("/admin/versions/v2", json={"status": "disabled"})
    view = client.patch("/admin/versions/v2", json={"status": "deprecated"})
    assert view.json()["callers"] == []
    codes = [client.get("/api/items", headers=h).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 429], codes
    _log("adm-reset", "停用清空用量；停用期改额度后启用：新额度+干净桶", codes)


def test_runtime_policy_change_under_concurrent_requests(client):
    # v3 初始额度 50 便于观察并发；50 个并发打满，再并发 PATCH 收紧到 10
    client.patch("/admin/versions/v3", json={"limit": 50})
    import threading

    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(80)

    def hit(i):
        barrier.wait()
        if i == 0:
            client.patch("/admin/versions/v3", json={"limit": 10})
        r = client.get("/api/items", headers={"X-Caller-ID": "adm-race"})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(80)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = codes.count(200)
    rejected = codes.count(429)
    assert accepted + rejected == 80, "并发期每个请求要么放行要么 429"
    assert 10 <= accepted <= 50, f"策略切换前后都可能判定，放行数应在 10~50，实际 {accepted}"
    assert accepted + rejected == 80
    # 管理端最终用量与放行数一致：不重扣、不漏扣
    usage = client.get("/admin/versions/v3").json()
    used = next(c["used"] for c in usage["callers"] if c["caller"] == "adm-race")
    assert used == accepted, f"最终用量 {used} 应等于放行数 {accepted}"
    _log("adm-race", "80 并发 + 并发收紧 50->10", f"200={accepted} 429={rejected} used={used}")
