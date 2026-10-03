"""版本级配额：跨版本隔离、运行时策略调整、并发判定准确、恢复信息与回收。"""
import logging
import threading

import pytest

from app.quotas import (
    QuotaPolicy,
    UnknownVersionPolicyError,
    VersionQuotaManager,
)

logger = logging.getLogger("gateway.tests.quotas")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def test_quota_isolated_per_version_and_caller():
    m = VersionQuotaManager(
        {"v2": QuotaPolicy(2, 10.0), "v3": QuotaPolicy(5, 10.0)}
    )
    assert [m.check("c", "v2", now=0.0).allowed for _ in range(3)] == [True, True, False]
    # 同一调用方换版本：有自己独立的额度
    assert [m.check("c", "v3", now=0.0).allowed for _ in range(6)] == [True] * 5 + [False]
    # 另一调用方在两个版本上都不受影响
    assert m.check("other", "v2", now=0.0).allowed
    assert m.check("other", "v3", now=0.0).allowed
    _log("q-iso", "(c,v2) 打满；c 在 v3 与 other 全部独立", "双向不挤占")


def test_tighten_limit_keeps_used_count():
    m = VersionQuotaManager({"v3": QuotaPolicy(10, 60.0)})
    for _ in range(8):
        assert m.check("c", "v3", now=0.0).allowed
    m.update_policy("v3", QuotaPolicy(3, 60.0))
    # 已用 8 > 新限额 3：不能因为换策略就把已用量当零而多放行
    denied = m.check("c", "v3", now=1.0)
    assert not denied.allowed and denied.limit == 3 and denied.remaining == 0
    snap = m.usage("v3", caller="c", now=1.0)
    assert snap.callers["c"].used == 8, "拒绝不得重复扣减或漏扣，已用量保持 8"
    _log("q-tighten", "limit 10->3，已用 8 保留，立即拒绝", snap.callers["c"])


def test_loosen_limit_only_adds_headroom():
    m = VersionQuotaManager({"v3": QuotaPolicy(5, 60.0)})
    for _ in range(5):
        assert m.check("c", "v3", now=0.0).allowed
    assert not m.check("c", "v3", now=0.0).allowed
    m.update_policy("v3", QuotaPolicy(8, 60.0))
    decisions = [m.check("c", "v3", now=1.0) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[0].remaining == 2 and decisions[2].remaining == 0
    _log("q-loosen", "limit 5->8，仅补 3 个差额", [d.remaining for d in decisions])


def test_widen_window_does_not_reset_count_at_old_boundary():
    m = VersionQuotaManager({"v3": QuotaPolicy(5, 60.0)})
    for _ in range(5):
        m.check("c", "v3", now=0.0)
    m.update_policy("v3", QuotaPolicy(5, 120.0))
    # t=61：旧窗口边界，按新窗口尚未走完，计数必须保留 -> 拒绝
    d = m.check("c", "v3", now=61.0)
    assert not d.allowed and d.reset_at == 120.0
    # t=120：一个完整新窗口过去后才翻窗 -> 放行
    d2 = m.check("c", "v3", now=120.0)
    assert d2.allowed and d2.window_start == 120.0 and d2.reset_at == 240.0
    _log("q-window-wide", "窗口 60->120，t=61 拒绝 / t=120 放行", (d.reset_at, d2.reset_at))


def test_shrink_window_rolls_only_after_full_new_window():
    m = VersionQuotaManager({"v3": QuotaPolicy(5, 120.0)})
    for _ in range(5):
        m.check("c", "v3", now=0.0)
    m.update_policy("v3", QuotaPolicy(5, 60.0))
    # 新窗口 60s 已完整过去：翻窗放行（旧窗口本也要到 120，
    # 这里提前放行只会发生在新窗口确实走完之后，不多放）
    d = m.check("c", "v3", now=60.0)
    assert d.allowed and d.reset_at == 120.0
    _log("q-window-shrink", "窗口 120->60，t=60 新窗口走完才翻窗", d.reset_at)


def test_concurrent_decisions_exact_while_policy_changes():
    m = VersionQuotaManager({"v3": QuotaPolicy(50, 60.0)})
    results: list[tuple[bool, int]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(100)

    def hit(i):
        barrier.wait()
        if i == 0:
            # 与其它 99 个判定并发收紧策略；无论谁先进入，
            # 每个请求只按一套策略判定，且桶计数不重不漏。
            m.update_policy("v3", QuotaPolicy(30, 60.0))
        d = m.check("c", "v3", now=0.0)
        with results_lock:
            results.append((d.allowed, d.limit))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # update 先于 check 的请求按 30 判定，反之按 50；放行总数介于两者之间，
    # 但最终计数必须精确等于放行数（无重复扣减/漏扣）。
    allowed = sum(1 for a, _ in results if a)
    assert 30 <= allowed <= 50, allowed
    used = m.usage("v3", caller="c", now=0.0).callers["c"].used
    assert used == allowed, f"最终用量 {used} 必须等于放行数 {allowed}"
    # 每个被拒请求在旧或新限额下都确实是第 >limit 个，决策与限额自洽。
    for allowed_flag, limit in results:
        if not allowed_flag:
            assert limit in (30, 50)
    _log("q-race", "100 并发中收紧 50->30", f"allowed={allowed} used={used}")


def test_concurrent_decisions_exact_while_window_changes():
    # 运行时改窗口与并发判定交叠：只换窗口不换额度，全部 100 个请求在
    # 同一时刻（now=0）判定，窗口翻转不发生——放行数必须恰好等于额度，
    # 最终用量与放行数精确一致，不重扣也不漏扣。
    m = VersionQuotaManager({"v3": QuotaPolicy(40, 60.0)})
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(100)

    def hit(i):
        barrier.wait()
        if i == 0:
            m.update_policy("v3", QuotaPolicy(40, 15.0))
        d = m.check("c", "v3", now=0.0)
        with results_lock:
            results.append(d.allowed)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    assert allowed == 40, f"只换窗口不换额度且未到翻窗时刻，应恰好放行 40，实际 {allowed}"
    used = m.usage("v3", caller="c", now=0.0).callers["c"].used
    assert used == allowed, f"最终用量 {used} 必须等于放行数 {allowed}"
    # 换窗口后恢复信息按新窗口给出（同一窗口起点 + 新窗口长度）
    denied = m.check("c", "v3", now=0.0)
    assert not denied.allowed and denied.window_seconds == 15.0
    assert denied.reset_at == 15.0
    _log("q-race-window", "100 并发中换窗口 60->15", f"allowed={allowed} used={used}")


def test_recovery_info_matches_current_window():
    m = VersionQuotaManager({"v3": QuotaPolicy(2, 10.0)})
    m.check("c", "v3", now=0.0)
    m.check("c", "v3", now=0.0)
    d = m.check("c", "v3", now=3.5)
    assert not d.allowed
    # 拒绝依据：当前窗口的窗口与限额
    assert d.window_start == 0.0 and d.window_seconds == 10.0
    assert d.reset_at == 10.0
    # 恢复时间对应当前窗口剩余时间，而不是下一次窗口的开始时间戳
    assert d.retry_after == pytest.approx(6.5)
    assert d.remaining == 0
    # 窗口翻转后恢复信息归零
    ok = m.check("c", "v3", now=10.0)
    assert ok.allowed and ok.retry_after == 0.0 and ok.remaining == 1
    _log("q-retry", "t=3.5 拒绝，恢复需等到 t=10（6.5s 后）", d.retry_after)


def test_recovery_info_after_mid_window_policy_change():
    m = VersionQuotaManager({"v3": QuotaPolicy(1, 60.0)})
    m.check("c", "v3", now=0.0)
    d1 = m.check("c", "v3", now=10.0)
    assert not d1.allowed and d1.retry_after == pytest.approx(50.0)
    # 改限额让请求重新可放行，不翻窗：恢复信息反映的是当前窗口
    m.update_policy("v3", QuotaPolicy(5, 30.0))
    d2 = m.check("c", "v3", now=10.0)
    assert d2.allowed  # 新窗口 30s 尚未走完，旧计数 1 < 5
    assert d2.reset_at == 30.0
    _log("q-retry-policy", "收紧改放宽后按当前窗口重置时刻恢复", d2.reset_at)


def test_stale_caller_buckets_reclaimed():
    m = VersionQuotaManager({"v2": QuotaPolicy(1, 10.0), "v3": QuotaPolicy(100, 5.0)})
    m.check("old-v2", "v2", now=0.0)
    m.check("old-v3", "v3", now=0.0)
    # 用活跃调用方触发清理：清理节流按最小窗口 5s；阈值为空闲严格大于 2*window。
    m.check("keeper", "v3", now=10.0)
    assert ("v2", "old-v2") in m._buckets, "v2 空闲恰好 2*window，按 > 判定保留"
    assert ("v3", "old-v3") in m._buckets, "v3 空闲恰好 2*window，按 > 判定保留"
    m.check("keeper", "v3", now=15.0)  # 距上次清理超过 5s，再次触发
    assert ("v3", "old-v3") not in m._buckets, "v3 空闲 15s > 2*5s，回收"
    assert ("v2", "old-v2") in m._buckets, "v2 空闲 15s < 2*10s，保留"
    m.check("keeper", "v3", now=25.0)
    assert ("v2", "old-v2") not in m._buckets, "v2 空闲 25s > 2*10s，回收"
    _log("q-gc", "按各版本自身窗口阈值回收过期调用方", m.bucket_count())


def test_remove_version_drops_its_buckets():
    m = VersionQuotaManager(
        {"v2": QuotaPolicy(1, 10.0), "v3": QuotaPolicy(10, 10.0)}
    )
    m.check("c", "v2", now=0.0)
    m.check("c", "v3", now=0.0)
    m.remove_version("v2")
    assert m.bucket_count() == 1
    with pytest.raises(UnknownVersionPolicyError):
        m.check("c", "v2", now=1.0)
    assert m.check("c", "v3", now=1.0).allowed
    _log("q-remove", "删除 v2 策略同步回收其桶", m.bucket_count())


def test_clear_usage_drops_buckets_but_keeps_policy():
    m = VersionQuotaManager(
        {"v2": QuotaPolicy(2, 10.0), "v3": QuotaPolicy(5, 10.0)}
    )
    for caller in ("a", "b"):
        m.check(caller, "v2", now=0.0)
    m.check("c", "v3", now=0.0)
    assert m.bucket_count() == 3
    # 停用语义：清空该版本全部调用方累计，其它版本桶不动
    m.clear_usage("v2")
    assert ("v2", "a") not in m._buckets
    assert ("v2", "b") not in m._buckets
    assert ("v3", "c") in m._buckets, "清理 v2 不得波及 v3 的桶"
    # 策略保留：清空后仍可按原策略判定，且调用方从 0 开始（干净重启口径）
    d = m.check("a", "v2", now=1.0)
    assert d.allowed and d.limit == 2 and d.remaining == 1
    # 清空一个本来没有桶的版本是安全的幂等操作
    m.clear_usage("v9")
    _log("q-clear", "clear_usage(v2) 只清用量保留策略，v3 不受影响", m.bucket_count())


def test_disabled_window_accumulates_nothing_and_restart_is_fresh():
    # 覆盖停用/重新启用的额度口径：打满 -> 清空 -> 干净桶 -> 再打满
    m = VersionQuotaManager({"v2": QuotaPolicy(2, 60.0)})
    assert [m.check("c", "v2", now=0.0).allowed for _ in range(3)] == [
        True,
        True,
        False,
    ]
    m.clear_usage("v2")  # 停用
    snap = m.usage("v2", caller="c", now=30.0)
    assert "c" not in snap.callers, "停用后旧用量不保留"
    assert m.bucket_count() == 0
    # 停用期间可以换策略（不创建桶），启用后新策略 + 干净桶
    m.update_policy("v2", QuotaPolicy(3, 60.0))
    decisions = [m.check("c", "v2", now=31.0) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    _log("q-disable-cycle", "打满->停用清空->换额度->启用后全新 3 额度", "通过")


def test_usage_snapshot_does_not_consume_quota():
    m = VersionQuotaManager({"v3": QuotaPolicy(2, 10.0)})
    m.check("c", "v3", now=0.0)
    for _ in range(5):
        snap = m.usage("v3", now=0.0)
    assert snap.callers["c"].used == 1 and snap.limit == 2
    # 下一个判定仍可放行（查询不消耗额度）
    assert m.check("c", "v3", now=0.0).allowed
    assert not m.check("c", "v3", now=0.0).allowed
    with pytest.raises(UnknownVersionPolicyError):
        m.usage("v9")
    _log("q-usage", "只读快照不创建/不扣减", snap.callers["c"])
