"""Token-bucket rate limiter (in-process; Redis is unnecessary for a single process — Decision D13)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate = rate_per_s
        self.capacity = burst
        self._tokens = float(burst)
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


class RateLimiter:
    """One bucket per tool, shared by all executions in this process (protects backends)."""

    def __init__(self, rate_per_s: float = 5.0, burst: int = 10, clock: Callable[[], float] = time.monotonic):
        self._rate, self._burst, self._clock = rate_per_s, burst, clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, tool: str) -> bool:
        with self._lock:
            bucket = self._buckets.setdefault(tool, TokenBucket(self._rate, self._burst, self._clock))
        return bucket.try_acquire()
