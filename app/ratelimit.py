"""In-process per-caller, per-version fixed-window rate limiter.

Concurrency model: a single reentrant-free lock guards the whole window map,
so check-and-increment is atomic. Keys are ``(caller_id, version)`` tuples,
which guarantees no cross-caller or cross-version pollution. Expired windows
are rolled over lazily and other expired entries are pruned on every check,
so memory is bounded by callers active within the current window.

Rejected requests are *not* counted: once the quota is full further calls do
not keep extending occupancy, and callers are rejected immediately (never
queued).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Window:
    started_at: float
    count: int = 0


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock=time.monotonic,
    ):
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], Window] = {}

    def check(self, caller: str, version: str) -> tuple[bool, int, int, float]:
        """Return ``(allowed, remaining, limit, retry_after_seconds)``.

        Atomically evaluates the quota and consumes one token when allowed.
        """
        now = self._clock()
        key = (caller, version)
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started_at >= self.window_seconds:
                # New (or rolled-over) window: first request consumes one token.
                window = Window(started_at=now, count=1)
                self._windows[key] = window
                allowed = True
            elif window.count >= self.limit:
                allowed = False
            else:
                window.count += 1
                allowed = True

            # Opportunistic pruning of expired entries belonging to other keys.
            stale = [
                k
                for k, w in self._windows.items()
                if k != key and now - w.started_at >= self.window_seconds
            ]
            for k in stale:
                del self._windows[k]

            remaining = max(0, self.limit - window.count)
            retry_after = max(0.0, self.window_seconds - (now - window.started_at))
            return allowed, remaining, self.limit, retry_after

    def active_keys(self) -> int:
        """Number of currently tracked (caller, version) windows."""
        with self._lock:
            return len(self._windows)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
