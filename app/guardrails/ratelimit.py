"""Tiny in-memory per-IP rate limiter (token bucket). Zero dependencies —
enough for a single-process internal tool. Swap for Redis if you scale out."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, per_minute: int):
        self._capacity = float(per_minute)
        self._refill_per_sec = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True
