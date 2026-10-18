"""Client-side token bucket rate limiter.

Paces Blast's firing and Barrage's closed-loop workers. Both the clock and the
sleep function are injectable so tests can drive the bucket through simulated
time without any test ever actually sleeping.

Structural contract (frozen; match this shape exactly, do not rename these
methods):

    def acquire(self, tokens: int = 1) -> float: ...     # blocks until tokens available; returns seconds waited
    def try_acquire(self, tokens: int = 1) -> bool: ...   # non-blocking; True if consumed

Constructor: TokenBucket(rate_per_sec, capacity, clock=..., sleep=...)

Thread safety: every method is safe to call from several threads at once. The
token count is guarded by a lock, and `acquire` releases that lock while it
sleeps, so one waiting thread never blocks another's `try_acquire`. Two threads
waiting on the same bucket each recompute their deficit after waking, so a
token is never handed out twice.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

# Shortest wait this module will ever ask a clock to honour, in seconds.
#
# An injected clock is usually purely additive: it advances by exactly the
# amount it was asked to sleep. A wait small enough to be a no-op against the
# current clock reading is a wait that will never elapse under such a clock, so
# a deficit this small is rounded off instead of slept on.
#
# One nanosecond is far below anything this rate limiter acts on: at the
# 50 req/s Barrage ceiling it is five hundred millionths of one token.
_MIN_WAIT = 1e-9


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
        self._lock = threading.Lock()
        self._tokens = float(capacity)
        self._last_refill = clock()

    @property
    def rate(self) -> float:
        return self._rate

    def _refill(self) -> None:
        # Caller holds self._lock.
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def set_rate(self, rate_per_sec: float) -> None:
        """Change the refill rate from now on. Tokens earned so far are kept,
        at the old rate, so a rate change never mints or forfeits credit."""
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be > 0, got {rate_per_sec}")
        with self._lock:
            self._refill()
            self._rate = float(rate_per_sec)

    def wait_time(self, tokens: int = 1) -> float:
        """Seconds until `tokens` would be available, without consuming any.
        0.0 when they are available now."""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
            self._refill()
            deficit = tokens - self._tokens
            return 0.0 if deficit <= 0 else deficit / self._rate

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking. Consume `tokens` if available right now and return
        True; otherwise leave the bucket untouched and return False."""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
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
        satisfied.

        Each pass sleeps for the whole current deficit in one call, and a
        deficit below _MIN_WAIT is rounded off rather than slept on. That is
        what makes this terminate under a purely additive injected clock.
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        if tokens > self._capacity:
            raise ValueError(
                f"cannot acquire {tokens} tokens: bucket capacity is {self._capacity}"
            )
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                remaining = (tokens - self._tokens) / self._rate
                if remaining <= _MIN_WAIT:
                    # Covered to within a rounding error the clock cannot
                    # represent. Hand over the sliver rather than spin on it.
                    self._tokens = 0.0
                    return waited
            self._sleep(remaining)
            waited += remaining
