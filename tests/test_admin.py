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


def test_runtime_disable_rejects_immediately_without_consuming_quota(client):
    h = {"X-Caller-ID": "adm-disable", "X-API-Version": "v2"}
    assert client.get("/api/items", headers=h).status_code == 200
    r = client.patch("/admin/versions/v2", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    # 停用后立刻 410
    blocked = client.get("/api/items", headers=h)
    assert blocked.status_code == 410
    assert blocked.json()["error"]["code"] == "VERSION_DISABLED"
    # 重新启用：停用期间没有任何请求占用 v2 额度，仍剩 1 个（之前已用 1）
    client.patch("/admin/versions/v2", json={"status": "deprecated"})
    ok = client.get("/api/items", headers=h)
    assert ok.status_code == 200
    again = client.get("/api/items", headers=h)
    assert again.status_code == 429, "v2 额度 2：恢复前 1 + 恢复后 1，第 3 次拒绝"
    _log(blocked.json()["error"]["request_id"], "运行时停用 v2 即时生效", "410->200->429")


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
