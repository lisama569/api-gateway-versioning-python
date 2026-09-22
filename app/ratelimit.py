"""按调用方维度的进程内限流器。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    caller: str
    limit: int
    remaining: int
    reset_at: float


class RateLimitExceeded(Exception):
    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(decision.caller)
        self.decision = decision


@dataclass
class _Bucket:
    window_start: float
    count: int = 0
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """固定窗口限流器。

    - 按 caller 分桶，桶之间互不影响；
    - 每个桶持有独立锁，计数与窗口翻转在同一临界区内完成，
      并发下不会重复计数或计数泄漏；
    - 超限请求立即被拒绝（allowed=False），不排队；
    - 空闲超过两个窗口的桶会被清理，避免状态无限增长。
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._buckets_lock = threading.Lock()
        self._last_cleanup = 0.0

    def _get_bucket(self, caller: str, now: float) -> _Bucket:
        with self._buckets_lock:
            bucket = self._buckets.get(caller)
            if bucket is None:
                bucket = _Bucket(window_start=now, last_seen=now)
                self._buckets[caller] = bucket
            return bucket

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._window:
            return
        self._last_cleanup = now
        idle_threshold = 2 * self._window
        with self._buckets_lock:
            stale = [
                caller
                for caller, bucket in self._buckets.items()
                if now - bucket.last_seen > idle_threshold
            ]
            for caller in stale:
                del self._buckets[caller]

    def check(self, caller: str, now: float | None = None) -> RateLimitDecision:
        now = time.monotonic() if now is None else now
        bucket = self._get_bucket(caller, now)
        with bucket.lock:
            if now - bucket.window_start >= self._window:
                bucket.window_start = now
                bucket.count = 0
            bucket.last_seen = now
            allowed = bucket.count < self._limit
            if allowed:
                bucket.count += 1
            remaining = max(0, self._limit - bucket.count)
            reset_at = bucket.window_start + self._window
        self._cleanup(now)
        return RateLimitDecision(
            allowed=allowed,
            caller=caller,
            limit=self._limit,
            remaining=remaining,
            reset_at=reset_at,
        )

    def bucket_count(self) -> int:
        with self._buckets_lock:
            return len(self._buckets)

    def reset(self) -> None:
        with self._buckets_lock:
            self._buckets.clear()
