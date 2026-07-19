"""In-memory rate limiting for the decoy chat — stdlib only, no Redis/DB/shared cache.

Two independent layers, both required to pass before a visitor's turn ever reaches Ollama:

  1. **Per-IP token bucket** (`TokenBucket`) — bursty-but-bounded per visitor.
  2. **Global concurrent + per-minute caps** (`GlobalLimiter`) — shared across every visitor, so no
     single IP (or a small botnet of them) can monopolize the one local Ollama instance.

Both are process-local, in-memory state — fine here: the decoy is a single-process, local-only
service (cockpit-spec.md ruling 12), so state resetting on restart is a non-issue. Guarded with a
plain `threading.Lock` rather than `asyncio.Lock` so this works correctly regardless of whether the
caller is a sync or async path function, or which event loop (if any) is live — FastAPI may run sync
route handlers in a worker thread, and `threading.Lock` is safe either way.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucket:
    """Per-key (IP) token bucket: `capacity` tokens max, refilling at `rate` tokens/second. One
    `allow()` call costs one token; returns False (deny) when the bucket is empty."""

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = max(0.0, capacity)
        self.rate = max(0.0, rate)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def reset(self) -> None:
        """Test-only: clear all per-key state so tests don't bleed into each other."""
        with self._lock:
            self._buckets.clear()


class GlobalLimiter:
    """Two caps shared across every visitor: a hard concurrent-in-flight ceiling, and a rolling
    60-second request-count ceiling. `try_acquire()` fails closed (denies) the instant either cap is
    hit; a successful acquire MUST be paired with exactly one `release()` (use try/finally)."""

    def __init__(self, max_concurrent: int, max_per_minute: int) -> None:
        self.max_concurrent = max(0, max_concurrent)
        self.max_per_minute = max(0, max_per_minute)
        self._concurrent = 0
        self._window_start = time.monotonic()
        self._window_count = 0
        self._lock = threading.Lock()

    def try_acquire(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._window_count = 0
            if self._concurrent >= self.max_concurrent:
                return False
            if self._window_count >= self.max_per_minute:
                return False
            self._concurrent += 1
            self._window_count += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._concurrent = max(0, self._concurrent - 1)

    def reset(self) -> None:
        """Test-only: return to a clean-slate state."""
        with self._lock:
            self._concurrent = 0
            self._window_start = time.monotonic()
            self._window_count = 0


class Counter:
    """The one thing this process remembers about visitors across requests: an anonymized in-memory
    tally, no content, no per-visitor identity, never written to disk. Satisfies cockpit-spec.md's "no
    persistence of visitor chats beyond an anonymized counter" — there is deliberately nothing else."""

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value
