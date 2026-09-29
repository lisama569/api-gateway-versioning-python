"""按 (版本, 调用方) 维度的进程内配额管理，支持运行时策略调整。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class QuotaPolicy:
    """单个版本的配额策略：窗口内最多放行 limit 个请求。"""

    limit: int
    window_seconds: float


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    caller: str
    version: str
    limit: int
    remaining: int
    window_seconds: float
    window_start: float
    reset_at: float
    retry_after: float


@dataclass(frozen=True)
class CallerUsage:
    caller: str
    limit: int
    used: int
    remaining: int
    window_seconds: float
    window_start: float
    reset_at: float


@dataclass(frozen=True)
class VersionQuotaSnapshot:
    version: str
    limit: int
    window_seconds: float
    callers: dict[str, CallerUsage]


class UnknownVersionPolicyError(Exception):
    """查询/操作了未配置配额策略的版本。"""

    def __init__(self, version: str) -> None:
        super().__init__(version)
        self.version = version


class QuotaExceeded(Exception):
    """(版本, 调用方) 维度配额耗尽，携带判定依据供错误映射使用。"""

    def __init__(self, decision: QuotaDecision) -> None:
        super().__init__(decision.caller, decision.version)
        self.decision = decision


@dataclass
class _Bucket:
    window_start: float
    count: int = 0
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _validate(policy: QuotaPolicy) -> None:
    if policy.limit <= 0:
        raise ValueError("limit must be positive")
    if policy.window_seconds <= 0:
        raise ValueError("window_seconds must be positive")


class VersionQuotaManager:
    """按版本独立配置、按调用方分桶的固定窗口配额管理器。

    - 桶键为 ``(version, caller)``：同一调用方在不同版本上的额度互不挤占，
      不同调用方之间也互不影响；
    - 策略表与桶表由同一把表锁保护，每个桶另有独立锁。``check`` 先在表锁内
      抓取该版本的策略快照，再在桶锁内完成窗口翻转与计数，因此运行时调整
      策略时，每个请求只按一套策略判定，计数不会重复扣减或漏扣；
    - 更新策略不会清零任何桶：窗口翻转只取决于“当前时间距窗口起点是否已经
      过去一个完整窗口”。额度收紧时已用量被保留（可能立即超限），额度放宽
      时仅补足差额；
    - 删除某版本策略时同步清理其全部桶；空闲超过两个窗口的桶也会被回收，
      内存占用不随历史调用方数量无限增长。
    """

    def __init__(self, policies: dict[str, QuotaPolicy]) -> None:
        for policy in policies.values():
            _validate(policy)
        self._policies: dict[str, QuotaPolicy] = dict(policies)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.RLock()
        self._last_cleanup = float("-inf")

    def policy_snapshot(self) -> dict[str, QuotaPolicy]:
        with self._lock:
            return dict(self._policies)

    def _roll_locked(self, bucket: _Bucket, policy: QuotaPolicy, now: float) -> None:
        # 仅当一个完整窗口确实已经过去时才翻窗计数；换策略本身不清零。
        if now - bucket.window_start >= policy.window_seconds:
            bucket.window_start = now
            bucket.count = 0

    def check(
        self, caller: str, version: str, now: float | None = None
    ) -> QuotaDecision:
        now = time.monotonic() if now is None else now
        key = (version, caller)
        with self._lock:
            policy = self._policies.get(version)
            if policy is None:
                raise UnknownVersionPolicyError(version)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(window_start=now, last_seen=now)
                self._buckets[key] = bucket

        with bucket.lock:
            self._roll_locked(bucket, policy, now)
            bucket.last_seen = now
            allowed = bucket.count < policy.limit
            if allowed:
                bucket.count += 1
            remaining = max(0, policy.limit - bucket.count)
            reset_at = bucket.window_start + policy.window_seconds
            retry_after = max(0.0, reset_at - now)
            used = bucket.count

        self._cleanup(now)
        return QuotaDecision(
            allowed=allowed,
            caller=caller,
            version=version,
            limit=policy.limit,
            remaining=remaining,
            window_seconds=policy.window_seconds,
            window_start=bucket.window_start,
            reset_at=reset_at,
            retry_after=retry_after if not allowed else 0.0,
        )

    def update_policy(self, version: str, policy: QuotaPolicy) -> None:
        """运行时替换某版本的额度/窗口，下个请求起生效；已用计数保留。"""
        _validate(policy)
        with self._lock:
            self._policies[version] = policy

    def remove_version(self, version: str) -> None:
        """删除版本策略并立即回收该版本的全部调用方桶。"""
        with self._lock:
            self._policies.pop(version, None)
            stale_keys = [key for key in self._buckets if key[0] == version]
            for key in stale_keys:
                del self._buckets[key]

    def usage(
        self,
        version: str,
        caller: str | None = None,
        now: float | None = None,
    ) -> VersionQuotaSnapshot:
        """查看某版本当前生效策略与用量；只读，不创建桶，不影响计数。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            policy = self._policies.get(version)
            if policy is None:
                raise UnknownVersionPolicyError(version)
            keys = (
                [(version, caller)]
                if caller is not None
                else [key for key in self._buckets if key[0] == version]
            )
            buckets = [(key[1], self._buckets[key]) for key in keys if key in self._buckets]

        usages: dict[str, CallerUsage] = {}
        for caller_name, bucket in buckets:
            with bucket.lock:
                self._roll_locked(bucket, policy, now)
                used = bucket.count
                reset_at = bucket.window_start + policy.window_seconds
                usages[caller_name] = CallerUsage(
                    caller=caller_name,
                    limit=policy.limit,
                    used=used,
                    remaining=max(0, policy.limit - used),
                    window_seconds=policy.window_seconds,
                    window_start=bucket.window_start,
                    reset_at=reset_at,
                )
        return VersionQuotaSnapshot(
            version=version,
            limit=policy.limit,
            window_seconds=policy.window_seconds,
            callers=usages,
        )

    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _cleanup(self, now: float) -> None:
        with self._lock:
            if not self._policies:
                self._buckets.clear()
                self._last_cleanup = now
                return
            smallest_window = min(p.window_seconds for p in self._policies.values())
            if now - self._last_cleanup < smallest_window:
                return
            self._last_cleanup = now
            stale_keys = []
            for (version, _caller), bucket in self._buckets.items():
                policy = self._policies.get(version)
                if policy is None:
                    stale_keys.append((version, _caller))
                    continue
                if now - bucket.last_seen > 2 * policy.window_seconds:
                    stale_keys.append((version, _caller))
            for key in stale_keys:
                self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._last_cleanup = float("-inf")
