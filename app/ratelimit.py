"""按 (调用方, 版本) 双维度的进程内配额限流器。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.versioning import QuotaPolicy, VersionSnapshot


@dataclass(frozen=True)
class CallerUsage:
    """单个调用方在某个版本当前窗口的用量快照。"""

    caller: str
    version: str
    limit: int
    remaining: int
    used: int
    window_start: float
    reset_at: float


@dataclass(frozen=True)
class VersionUsage:
    """某个版本当前生效的策略与各调用方用量。"""

    version: str
    limit: int
    window_seconds: float
    callers: tuple[CallerUsage, ...]
    active_callers: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    caller: str
    version: str
    limit: int
    remaining: int
    used: int
    window_start: float
    window_seconds: float
    reset_at: float
    retry_after: float
    window_rolled: bool

    @property
    def retry_after_header(self) -> str:
        """Retry-After 头：向上取整到整秒，给调用方保守的等待秒数。"""
        return str(max(1, int(self.retry_after + 0.999999)))


class RateLimitExceeded(Exception):
    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(decision.caller)
        self.decision = decision


@dataclass
class _Bucket:
    window_start: float
    window: float
    limit: int
    count: int = 0
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class VersionedRateLimiter:
    """按 (caller, version) 分桶的固定窗口限流器。

    - 同一调用方在不同版本各有独立桶，额度两个方向都不互相挤占；
    - 每次判定使用请求自带的版本策略快照，运行时改策略不影响在途请求；
    - 窗口长度/额度可以在运行中调整，已用计数不重置、不重复扣；
    - 版本停用后由 drop_version 立即清掉该版本所有桶；
    - 空闲超过自身两个窗口的桶会被回收。
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._buckets_lock = threading.Lock()
        self._last_cleanup = 0.0

    def _get_bucket(
        self, key: tuple[str, str], now: float, window: float, limit: int
    ) -> _Bucket:
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    window_start=now, window=window, limit=limit, last_seen=now
                )
                self._buckets[key] = bucket
            return bucket

    def _cleanup(self, now: float) -> None:
        # 清理节流：至少跨过一个（最小）窗口才扫描一次。
        with self._buckets_lock:
            if self._buckets:
                min_window = min(b.window for b in self._buckets.values())
            else:
                min_window = None
            if min_window is not None and now - self._last_cleanup < min_window:
                return
            self._last_cleanup = now
            stale = [
                key
                for key, bucket in self._buckets.items()
                if now - bucket.last_seen > 2 * bucket.window
            ]
            for key in stale:
                del self._buckets[key]

    def check(
        self, snapshot: VersionSnapshot, caller: str, now: float | None = None
    ) -> RateLimitDecision:
        now = time.monotonic() if now is None else now
        policy: QuotaPolicy = snapshot.quota
        version = snapshot.info.name
        key = (caller, version)
        bucket = self._get_bucket(key, now, policy.window_seconds, policy.limit)
        with bucket.lock:
            rolled = False
            # 始终按"当前策略"的窗口长度计算已跨过的窗口数：
            # 改长窗口时窗口内已用量保留（不会被当成零而多放行）；
            # 改短窗口时到了新边界立即翻转，不会被旧的长窗口拖住不翻。
            strides = int((now - bucket.window_start) // policy.window_seconds)
            if strides >= 1:
                bucket.window_start += strides * policy.window_seconds
                bucket.count = 0
                rolled = True
            bucket.window = policy.window_seconds
            bucket.limit = policy.limit
            bucket.last_seen = now
            allowed = bucket.count < policy.limit
            if allowed:
                bucket.count += 1
            used = bucket.count
            remaining = max(0, policy.limit - used)
            window_start = bucket.window_start
            reset_at = window_start + policy.window_seconds
        self._cleanup(now)
        return RateLimitDecision(
            allowed=allowed,
            caller=caller,
            version=version,
            limit=policy.limit,
            remaining=remaining,
            used=used,
            window_start=window_start,
            window_seconds=policy.window_seconds,
            reset_at=reset_at,
            retry_after=max(0.0, reset_at - now),
            window_rolled=rolled,
        )

    def drop_version(self, version: str) -> None:
        """版本停用时立即清掉它的全部调用方桶，停用后不再占用任何额度。"""
        with self._buckets_lock:
            stale = [key for key in self._buckets if key[1] == version]
            for key in stale:
                del self._buckets[key]

    def usage(self, snapshot: VersionSnapshot, now: float | None = None) -> VersionUsage:
        """查询版本当前生效策略与各调用方当前窗口用量（过期桶不计入）。"""
        now = time.monotonic() if now is None else now
        policy = snapshot.quota
        version = snapshot.info.name
        callers: list[CallerUsage] = []
        with self._buckets_lock:
            items = [
                (key, bucket)
                for key, bucket in self._buckets.items()
                if key[1] == version
            ]
        for (caller, _), bucket in items:
            with bucket.lock:
                if now - bucket.window_start >= policy.window_seconds:
                    # 已跨过当前策略窗口：对查询而言用量已归零，不展示。
                    continue
                used = bucket.count
                callers.append(
                    CallerUsage(
                        caller=caller,
                        version=version,
                        limit=policy.limit,
                        remaining=max(0, policy.limit - used),
                        used=used,
                        window_start=bucket.window_start,
                        reset_at=bucket.window_start + policy.window_seconds,
                    )
                )
        callers.sort(key=lambda item: item.caller)
        return VersionUsage(
            version=version,
            limit=policy.limit,
            window_seconds=policy.window_seconds,
            callers=tuple(callers),
            active_callers=len(callers),
        )

    def bucket_count(self, version: str | None = None) -> int:
        with self._buckets_lock:
            if version is None:
                return len(self._buckets)
            return sum(1 for key in self._buckets if key[1] == version)

    def reset(self) -> None:
        with self._buckets_lock:
            self._buckets.clear()
