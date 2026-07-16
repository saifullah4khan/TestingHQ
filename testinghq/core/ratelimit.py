"""Client-side token bucket rate limiter.

Used to pace Blast's firing so it never hammers a target endpoint, even by
accident. Both the clock and the sleep function are injectable so tests can
drive the bucket through simulated time without any test ever actually
sleeping.

Structural contract shared with the security lane (frozen; match this shape
exactly, do not rename these methods):

    def acquire(self, tokens: int = 1) -> float: ...     # blocks until tokens available; returns seconds waited
    def try_acquire(self, tokens: int = 1) -> bool: ...   # non-blocking; True if consumed

Constructor: TokenBucket(rate_per_sec, capacity, clock=..., sleep=...)
"""
from __future__ import annotations

import time
from typing import Callable


class TokenBucket:
    """A standard token bucket: holds at most `capacity` tokens, refilling
    continuously at `rate_per_sec` tokens per second. Starts full."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be > 0, got {rate_per_sec}")
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._last_refill = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking. Consume `tokens` if available right now and return
        True; otherwise leave the bucket untouched and return False."""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def acquire(self, tokens: int = 1) -> float:
        """Blocking. Wait, via the injected sleep, until `tokens` are
        available, then consume them and return the number of seconds
        waited (0.0 if they were already available). Raises ValueError if
        `tokens` exceeds the bucket's capacity, since it could never be
        satisfied."""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        if tokens > self._capacity:
            raise ValueError(
                f"cannot acquire {tokens} tokens: bucket capacity is {self._capacity}"
            )
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return waited
            deficit = tokens - self._tokens
            wait_for = deficit / self._rate
            self._sleep(wait_for)
            waited += wait_for
