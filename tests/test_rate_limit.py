"""Per-(caller, version) rate limiting: boundaries, isolation, concurrency."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import Settings, RateLimitConfig
from app.main import create_app
from app.ratelimit import RateLimiter


def app_client(limit: int = 3, window: float = 60.0) -> TestClient:
    return TestClient(
        create_app(Settings(rate_limit=RateLimitConfig(limit=limit, window_seconds=window)))
    )


# --------------------------------------------------------------- limiter units
def test_quota_boundary_rejects_immediately_without_queue():
    rl = RateLimiter(limit=3, window_seconds=10.0)
    results = [rl.check("c", "2") for _ in range(5)]
    allowed = [r[0] for r in results]
    assert allowed == [True, True, True, False, False]
    assert results[-1][1] == 0  # remaining zero
    assert results[-1][3] > 0  # retry_after positive


def test_rejected_requests_are_not_counted():
    t = {"now": 0.0}
    rl = RateLimiter(limit=1, window_seconds=10.0, clock=lambda: t["now"])
    assert rl.check("c", "2")[0] is True
    for _ in range(10):
        assert rl.check("c", "2")[0] is False
    # Roll over: only the single accepted request may have consumed the old
    # window; the ten rejected calls must not have inflated the counter.
    t["now"] = 10.0
    assert rl.check("c", "2")[0] is True
    assert rl.check("c", "2")[0] is False


def test_window_rollover_at_boundary():
    t = {"now": 0.0}
    rl = RateLimiter(limit=2, window_seconds=10.0, clock=lambda: t["now"])
    assert rl.check("c", "2")[0] is True
    assert rl.check("c", "2")[0] is True
    assert rl.check("c", "2")[0] is False
    t["now"] = 9.999
    assert rl.check("c", "2")[0] is False  # still inside window
    t["now"] = 10.0
    rollover = rl.check("c", "2")
    assert rollover[0] is True  # exactly at boundary -> fresh window
    assert rollover[1] == 1


def test_no_cross_caller_or_version_pollution():
    rl = RateLimiter(limit=2, window_seconds=10.0)
    rl.check("alice", "2")
    rl.check("alice", "2")
    assert rl.check("alice", "2")[0] is False
    assert rl.check("bob", "2")[0] is True       # caller isolated
    assert rl.check("alice", "3")[0] is True     # version isolated
    assert rl.check("alice", "3")[0] is True
    assert rl.check("alice", "3")[0] is False


def test_expired_windows_are_pruned_no_count_leak():
    t = {"now": 0.0}
    rl = RateLimiter(limit=1, window_seconds=5.0, clock=lambda: t["now"])
    for caller in ("a", "b", "c"):
        assert rl.check(caller, "2")[0] is True
    assert rl.active_keys() == 3
    t["now"] = 6.0
    # Traffic on one key rolls it over and must prune the other stale keys.
    assert rl.check("a", "2")[0] is True
    assert rl.active_keys() == 1
    # Expired callers start a clean window: no leaked quota consumption.
    assert rl.check("b", "2") == (True, 0, 1, 5.0)


def test_concurrent_burst_exact_quota_under_race():
    # 500 threads contend for a quota of 100 on ONE key. Atomic check-and-
    # increment must yield exactly 100 wins, no double counting.
    limit = 100
    rl = RateLimiter(limit=limit, window_seconds=10.0)
    start = threading.Event()

    def one_check():
        start.wait()
        return rl.check("burst", "2")[0]

    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = [pool.submit(one_check) for _ in range(500)]
        start.set()
        results = [f.result() for f in futures]

    allowed = sum(results)
    print(f"并发竞态 caller=burst version=2 limit={limit} 总请求=500 -> 放行={allowed} 拒绝={500 - allowed}")
    assert allowed == limit


def test_concurrent_burst_isolation_per_version_key():
    # Same caller, two versions hit concurrently: each gets its own full quota.
    rl = RateLimiter(limit=50, window_seconds=10.0)
    start = threading.Event()

    def one_check(version):
        start.wait()
        return rl.check("multi", version)[0]

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(one_check, "2") for _ in range(200)]
        futures += [pool.submit(one_check, "3") for _ in range(200)]
        start.set()
        results = [f.result() for f in futures]
    assert sum(results) == 100  # 50 per version, no cross-pollution


# ------------------------------------------------------------- http-level tests
def test_http_quota_exceeded_is_429_not_queued():
    client = app_client(limit=3)
    statuses = [client.get("/api/ping", headers={"X-Caller-ID": "alice"}).status_code for _ in range(5)]
    print("判定序列 caller=alice version=2 ->", statuses)
    assert statuses == [200, 200, 200, 429, 429]
    limited = client.get("/api/ping", headers={"X-Caller-ID": "alice"})
    body = limited.json()["error"]
    assert body["code"] == "RATE_LIMITED"
    assert body["details"]["caller"] == "alice"
    assert body["details"]["version"] == "2"
    assert limited.headers["Retry-After"].isdigit()


def test_http_anonymous_caller_uses_shared_quota():
    client = app_client(limit=2)
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/ping").status_code == 429


def test_http_isolation_between_callers_and_versions():
    client = app_client(limit=2)
    h = lambda c, v=None: {"X-Caller-ID": c, **({"X-API-Version": v} if v else {})}
    assert client.get("/api/ping", headers=h("a")).status_code == 200
    assert client.get("/api/ping", headers=h("a")).status_code == 200
    assert client.get("/api/ping", headers=h("a")).status_code == 429
    assert client.get("/api/ping", headers=h("b")).status_code == 200
    assert client.get("/api/ping", headers=h("a", "v3")).status_code == 200
    assert client.get("/api/ping", headers=h("a", "v3")).status_code == 200
    assert client.get("/api/ping", headers=h("a", "v3")).status_code == 429


def test_http_concurrent_burst_respects_quota():
    # TestClient serializes portal calls per client instance; a shared client
    # across threads still exercises the real limiter lock end-to-end.
    client = app_client(limit=20)
    start = threading.Barrier(16)

    def one_request():
        start.wait()
        return client.get("/api/ping", headers={"X-Caller-ID": "burst-http"}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(lambda _: one_request(), range(80)))
    ok = statuses.count(200)
    print(f"HTTP并发竞态 caller=burst-http limit=20 总请求=80 -> 200x{ok}, 429x{statuses.count(429)}")
    assert ok == 20
