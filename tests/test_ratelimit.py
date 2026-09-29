"""限流场景：超限即拒、并发计数精确、窗口过期、跨调用方/跨版本隔离、状态清理。"""
import logging
import threading

from app.ratelimit import RateLimiter

logger = logging.getLogger("gateway.tests.ratelimit")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def test_over_limit_rejected_immediately(client):
    codes = [
        client.get("/api/items", headers={"X-Caller-ID": "rl-basic"}).status_code
        for _ in range(7)
    ]
    assert codes == [200] * 5 + [429] * 2
    r = client.get("/api/items", headers={"X-Caller-ID": "rl-basic"})
    err = r.json()["error"]
    assert err["code"] == "RATE_LIMITED"
    _log(err["request_id"], "caller=rl-basic 第 6 次请求超出 limit=5", err["code"])


def test_concurrent_counting_is_exact():
    limiter = RateLimiter(limit=50, window_seconds=60.0)
    results: list[bool] = []
    barrier = threading.Barrier(100)

    def hit():
        barrier.wait()
        results.append(limiter.check("rl-race", now=0.0).allowed)

    threads = [threading.Thread(target=hit) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    allowed = sum(results)
    assert allowed == 50, f"并发下恰好 50 个请求被放行，实际 {allowed}"
    _log("rl-race", "100 线程同时竞争 limit=50", f"allowed={allowed} denied={100 - allowed}")


def test_concurrent_per_caller_isolation():
    limiter = RateLimiter(limit=10, window_seconds=60.0)
    counts: dict[str, int] = {"c1": 0, "c2": 0}
    lock = threading.Lock()

    def hit(caller: str):
        if limiter.check(caller, now=0.0).allowed:
            with lock:
                counts[caller] += 1

    threads = [
        threading.Thread(target=hit, args=(caller,))
        for caller in ("c1", "c2")
        for _ in range(30)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts == {"c1": 10, "c2": 10}, "跨调用方不得互相污染"
    _log("rl-isolation", "c1/c2 各 30 并发，limit=10", counts)


def test_window_expiry_recounts():
    limiter = RateLimiter(limit=2, window_seconds=10.0)
    assert limiter.check("w", now=0.0).allowed
    assert limiter.check("w", now=5.0).allowed
    assert not limiter.check("w", now=9.9).allowed, "窗口内超限必须拒绝"
    assert limiter.check("w", now=10.0).allowed, "窗口过期后重新计数"
    _log("rl-window", "window=10s，t=9.9 拒绝 / t=10.0 放行", "窗口翻转语义稳定")


def test_stale_buckets_cleaned_up():
    limiter = RateLimiter(limit=1, window_seconds=10.0)
    limiter.check("old", now=0.0)
    limiter.check("trigger", now=100.0)  # 触发清理
    assert limiter.bucket_count() == 1, "空闲超过两个窗口的桶必须被清理"
    _log("rl-cleanup", "old 空闲 100s > 2*window", f"buckets={limiter.bucket_count()}")


def test_quota_isolated_across_versions_and_callers(client):
    # v2 额度 2：同一调用方在 v2 上打满后立即被拒
    headers_v2 = {"X-Caller-ID": "rl-ver", "X-API-Version": "v2"}
    codes_v2 = [client.get("/api/items", headers=headers_v2).status_code for _ in range(4)]
    assert codes_v2 == [200, 200, 429, 429], "v2 窗口额度为 2"
    # 换到 v3（额度 5）仍可正常调用，额度互不挤占
    codes_v3 = [
        client.get(
            "/api/items",
            headers={"X-Caller-ID": "rl-ver", "X-API-Version": "v3"},
        ).status_code
        for _ in range(6)
    ]
    assert codes_v3 == [200] * 5 + [429], "v3 有独立额度，不被 v2 的用量挤占"
    # 另一调用方在 v2 上也不受 rl-ver 影响
    r = client.get(
        "/api/items",
        headers={"X-Caller-ID": "rl-ver-other", "X-API-Version": "v2"},
    )
    assert r.status_code == 200
    _log(
        "rl-ver",
        "rl-ver 打满 v2 后 v3 仍有 5 个额度；其它调用方 v2 独立",
        {"v2": codes_v2, "v3": codes_v3, "other": r.status_code},
    )


def test_undeclared_version_uses_default_version_quota(client):
    # 未声明版本按默认版本 v3 的额度（5）计算，与显式 v3 请求共享同一桶
    codes = [
        client.get("/api/items", headers={"X-Caller-ID": "rl-default"}).status_code
        for _ in range(3)
    ]
    assert codes == [200] * 3
    for _ in range(2):
        r = client.get(
            "/api/items",
            headers={"X-Caller-ID": "rl-default", "X-API-Version": "v3"},
        )
        assert r.status_code == 200
    r = client.get("/api/items", headers={"X-Caller-ID": "rl-default"})
    assert r.status_code == 429, "未声明版本走默认版本策略，额度与显式 v3 合并计算"
    err = r.json()["error"]
    assert err["details"]["version"] == "v3"
    _log(err["request_id"], "未声明版本按默认 v3 配额判定", err["details"])
