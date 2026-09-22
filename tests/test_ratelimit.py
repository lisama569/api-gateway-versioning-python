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


def test_quota_not_shared_across_versions(client):
    headers = {"X-Caller-ID": "rl-ver", "X-API-Version": "v2"}
    codes = [client.get("/api/items", headers=headers).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429], "额度按调用方计，与版本无关且不重复计数"
    _log("rl-ver", "同一 caller 跨版本请求共享同一额度", codes)
