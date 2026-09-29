"""配额场景：版本级隔离、运行时改策略、停用即拒、恢复信息、并发精确、回收。"""
import logging
import threading

from app.ratelimit import VersionedRateLimiter
from app.versioning import (
    QuotaPolicy,
    VersionInfo,
    VersionRegistry,
    VersionStatus,
)

logger = logging.getLogger("gateway.tests.ratelimit")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def _registry() -> VersionRegistry:
    return VersionRegistry(
        versions={
            "v2": VersionInfo("v2", VersionStatus.DEPRECATED, sunset="2026-12-31"),
            "v3": VersionInfo("v3", VersionStatus.ACTIVE),
        },
        default="v3",
        quotas={
            "v2": QuotaPolicy(limit=2, window_seconds=10.0),
            "v3": QuotaPolicy(limit=5, window_seconds=60.0),
        },
    )


# ---------- 跨版本额度互不影响 ----------

def test_quota_isolated_between_versions():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2, s3 = reg.negotiate("v2"), reg.negotiate("v3")

    # v2 额度只有 2，用满
    assert rl.check(s2, "c", now=0.0).allowed
    assert rl.check(s2, "c", now=0.0).allowed
    denied_v2 = rl.check(s2, "c", now=0.0)
    assert not denied_v2.allowed

    # 同一调用方换到 v3，额度完整，不受 v2 用满影响
    v3_decisions = [rl.check(s3, "c", now=0.0).allowed for _ in range(5)]
    assert v3_decisions == [True] * 5
    assert not rl.check(s3, "c", now=0.0).allowed

    # 反向也不挤占：v3 用满后，v2 的判定结果不变（仍拒绝，且不会因 v3 而变化）
    assert not rl.check(s2, "c", now=0.0).allowed
    _log("iso-1", "v2=2 用满 / v3=5 完整 / 反向不挤占", "双向隔离")


def test_quota_isolated_via_http(client):
    # v2 额度 3，v3 额度 10
    for _ in range(3):
        assert client.get(
            "/api/items",
            headers={"X-Caller-ID": "iso", "X-API-Version": "v2"},
        ).status_code == 200
    assert client.get(
        "/api/items",
        headers={"X-Caller-ID": "iso", "X-API-Version": "v2"},
    ).status_code == 429

    # v2 被限，同一调用方在 v3 仍能正常调用
    for _ in range(10):
        assert client.get(
            "/api/items",
            headers={"X-Caller-ID": "iso", "X-API-Version": "v3"},
        ).status_code == 200
    assert client.get(
        "/api/items",
        headers={"X-Caller-ID": "iso", "X-API-Version": "v3"},
    ).status_code == 429

    # 未声明版本走默认 v3 的策略：v3 已用满，未声明也应被拒；与 v2 无关
    assert client.get(
        "/api/items", headers={"X-Caller-ID": "iso"}
    ).status_code == 429
    _log("iso-http", "同 caller：v2 被限，v3 独立 10 次，默认版本走 v3", 429)


def test_default_version_uses_default_quota():
    reg = VersionRegistry(
        versions={"v3": VersionInfo("v3", VersionStatus.ACTIVE)},
        default="v3",
        default_quota=QuotaPolicy(limit=3, window_seconds=10.0),
    )
    rl = VersionedRateLimiter()
    snap = reg.negotiate(None)
    assert snap.used_default and snap.quota.limit == 3
    assert all(rl.check(snap, "anon", now=0.0).allowed for _ in range(3))
    assert not rl.check(snap, "anon", now=0.0).allowed
    _log("default-quota", "未声明版本按默认版本配额计算", "limit=3")


# ---------- 运行时调整策略 ----------

def test_change_limit_keeps_used_count():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    assert rl.check(s2, "c", now=0.0).allowed
    assert rl.check(s2, "c", now=0.0).allowed
    assert not rl.check(s2, "c", now=0.0).allowed

    # 窗口内把额度从 2 放宽到 4：已用 2 保留，再放且只再放 2 个
    s2b = reg.update_policy("v2", limit=4)
    assert rl.check(s2b, "c", now=5.0).allowed  # 第 3 个
    assert rl.check(s2b, "c", now=5.0).allowed  # 第 4 个
    d = rl.check(s2b, "c", now=5.0)
    assert not d.allowed and d.used == 4
    _log("dyn-limit-up", "limit 2->4，已用计数不清零", "再放 2 个后拒绝")


def test_tighten_limit_does_not_reset_window():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    assert rl.check(s2, "c", now=0.0).allowed
    # 收紧到 1：已用 1 已达新额度，不能因为改策略而多放行
    s2b = reg.update_policy("v2", limit=1)
    d = rl.check(s2b, "c", now=5.0)
    assert not d.allowed and d.used == 1 and d.remaining == 0
    # 窗口结束（t=10）才重新按新额度计数
    assert rl.check(s2b, "c", now=10.0).allowed
    _log("dyn-limit-down", "limit 2->1，窗口不重置，到期才恢复", "t=10 恢复")


def test_change_window_does_not_forget_usage():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    rl.check(s2, "c", now=0.0)
    # 窗口从 10 拉长到 20：t=5 仍处于原窗口语义下，已用计数不能清零
    s2b = reg.update_policy("v2", window_seconds=20.0)
    rl.check(s2b, "c", now=5.0)
    d = rl.check(s2b, "c", now=5.0)
    assert not d.allowed and d.used == 2, "拉长窗口不得把已用量归零而多放行"
    _log("dyn-window-up", "window 10->20，t=5 已用量保留", "used=2 拒绝")


def test_shorten_window_flips_at_new_boundary():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    rl.check(s2, "c", now=0.0)  # 窗口 [0,10)，已用 1
    # 窗口从 10 缩短到 5：t=5 就是新边界，必须翻转并重新计数，
    # 不能被旧的长窗口拖到 t=10 才翻。
    s2b = reg.update_policy("v2", window_seconds=5.0)
    d = rl.check(s2b, "c", now=5.0)
    assert d.allowed and d.used == 1 and d.window_rolled
    assert d.window_start == 5.0 and d.reset_at == 10.0
    _log("dyn-window-down", "window 10->5，t=5 在新边界翻转", "used 重置为 1")


def test_disabled_version_rejected_immediately_and_frees_quota(client):
    # 停用前 v2 正常并产生用量
    assert client.get(
        "/api/items", headers={"X-API-Version": "v2", "X-Caller-ID": "d"}
    ).status_code == 200

    r = client.patch("/admin/versions/v2/policy", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"

    # 停用后请求立刻 410，且不占任何额度（桶被清掉）
    assert client.get(
        "/api/items", headers={"X-API-Version": "v2", "X-Caller-ID": "d"}
    ).status_code == 410

    # 重新启用后，历史用量已随停用清空，从 0 开始
    r = client.patch("/admin/versions/v2/policy", json={"status": "deprecated"})
    assert r.status_code == 200
    assert client.get(
        "/api/items", headers={"X-API-Version": "v2", "X-Caller-ID": "d"}
    ).status_code == 200
    _log("disable", "v2 运行时停用：立即 410、清桶、重启用从 0 开始", "ok")


def test_concurrent_policy_change_decisions_stable():
    """并发请求打进来时改额度，放行总数与计数必须精确，不多放不漏计。"""
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    # 先用掉 1 个（limit=2）
    rl.check(s2, "race", now=0.0)

    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker(i: int):
        barrier.wait()
        # 每个线程取一次最新快照（模拟运行中策略已切换），额度放宽到 20
        snap = reg.describe("v2")
        d = rl.check(snap, "race", now=1.0)
        with lock:
            results.append(d.allowed)

    # 与工作线程并发地把额度从 2 改到 20
    def changer():
        barrier.wait()
        reg.update_policy("v2", limit=20)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(39)]
    threads.append(threading.Thread(target=changer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    # 无论改变量在什么时序生效：放行数最多为 20-1=19，且计数精确不超发
    assert allowed <= 19, f"并发改策略不得多放行，实际 {allowed}"
    assert allowed >= 1, "放宽额度后必须有额外放行"
    final = reg.describe("v2")
    d = rl.check(final, "race", now=1.0)
    assert not d.allowed, "最终窗口累计用量必须已达新额度 20"
    _log(
        "race-policy",
        "39 并发 + 并发改 limit 2->20（已用 1）",
        f"allowed={allowed}, final_used={d.used}",
    )


def test_concurrent_counting_is_exact():
    reg = _registry()
    rl = VersionedRateLimiter()
    s3 = reg.negotiate("v3")
    results: list[bool] = []
    barrier = threading.Barrier(100)

    def hit():
        barrier.wait()
        results.append(rl.check(s3, "rl-race", now=0.0).allowed)

    threads = [threading.Thread(target=hit) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 5, "并发下恰好 limit 个请求被放行"
    _log("race-exact", "100 线程竞争 limit=5", f"allowed={sum(results)}")


# ---------- 拒绝时的恢复信息与当前窗口一致 ----------

def test_rejection_recovery_matches_window():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    rl.check(s2, "w", now=0.0)
    rl.check(s2, "w", now=0.0)
    d = rl.check(s2, "w", now=3.0)
    assert not d.allowed
    # 恢复时间必须指向当前窗口结束 t=10，而不是下一次窗口起点 t=20
    assert d.window_start == 0.0
    assert d.reset_at == 10.0
    assert d.retry_after == 7.0
    assert d.retry_after_header == "7"
    assert d.remaining == 0 and d.used == 2 and d.limit == 2
    _log("recover", "t=3 被拒，恢复信息指向当前窗口结束 t=10", "retry_after=7")


def test_rejection_recovery_http(client):
    # v3 limit=10 / window=60，用满后检查 429 响应的依据与恢复信息
    for _ in range(10):
        client.get("/api/items", headers={"X-Caller-ID": "rh"})
    r = client.get("/api/items", headers={"X-Caller-ID": "rh"})
    assert r.status_code == 429
    details = r.json()["error"]["details"]
    assert details["version"] == "v3"
    assert details["caller"] == "rh"
    assert details["used"] == 10 and details["limit"] == 10 and details["remaining"] == 0
    assert 0 < details["reset_after_seconds"] <= 60
    assert r.headers["Retry-After"] == str(int(round(details["reset_after_seconds"]))) or (
        abs(int(r.headers["Retry-After"]) - details["reset_after_seconds"]) <= 1
    )
    _log("recover-http", "429 携带版本依据、剩余额度与窗口一致的恢复时间", details)


# ---------- 窗口过期与过期桶回收 ----------

def test_window_expiry_recounts():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2 = reg.negotiate("v2")
    assert rl.check(s2, "w", now=0.0).allowed
    assert rl.check(s2, "w", now=5.0).allowed
    assert not rl.check(s2, "w", now=9.9).allowed
    assert rl.check(s2, "w", now=10.0).allowed, "窗口过期后重新计数"
    _log("window", "window=10s，t=9.9 拒 / t=10.0 放", "翻转语义稳定")


def test_stale_buckets_cleaned_up():
    reg = _registry()
    rl = VersionedRateLimiter()
    s2, s3 = reg.negotiate("v2"), reg.negotiate("v3")
    rl.check(s2, "old", now=0.0)
    rl.check(s3, "new", now=0.0)
    # v2 窗口 10s，空闲超过 2*10 触发清理；v3 窗口 60s 仍保留
    rl.check(s2, "trigger", now=25.0)
    rl._cleanup(25.0)
    assert rl.bucket_count("v2") == 1, "old 桶应被回收，仅剩 trigger"
    assert rl.bucket_count("v3") == 1, "v3 未超过自身两窗口，保留"
    _log("cleanup", "按各桶自身窗口回收过期调用方统计", f"total={rl.bucket_count()}")
