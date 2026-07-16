"""Tests for the RateLimitGate contract defined in guardrails.py.

Per the interface contract, the engine lane implements the real token
bucket in testinghq.core.ratelimit, built against this Protocol. This
security branch must not import that package (it may not exist here), so
this suite builds its own hermetic fake token bucket and uses it to prove
two things: the contract is structurally checkable (runtime_checkable
Protocol), and a real implementation of it actually paces calls using an
injected clock, never a real sleep.
"""
import pytest

from testinghq.core import guardrails


class FakeClock:
    """A controllable clock for hermetic rate-limit tests. `sleep` never
    blocks; it records the request and advances the fake clock instantly.
    """

    def __init__(self, start=0.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class TokenBucketGate:
    """Minimal token-bucket rate limiter implementing RateLimitGate.

    A reference fake for testing the contract in isolation. Not the
    engine lane's implementation; that lives in core/ratelimit.py and is
    not imported here.
    """

    def __init__(self, rate, capacity, clock):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.clock = clock
        self._last = clock.time()

    def _refill(self):
        now = self.clock.time()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def try_acquire(self, tokens=1):
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def acquire(self, tokens=1):
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return 0.0
        deficit = tokens - self.tokens
        wait = deficit / self.rate
        self.clock.sleep(wait)
        self.tokens = 0.0
        self._last = self.clock.time()
        return wait


def test_conforming_gate_satisfies_the_protocol():
    clock = FakeClock()
    gate = TokenBucketGate(rate=1, capacity=1, clock=clock)
    assert isinstance(gate, guardrails.RateLimitGate)


def test_object_missing_try_acquire_fails_the_protocol():
    class OnlyAcquire:
        def acquire(self, tokens=1):
            return 0.0

    assert not isinstance(OnlyAcquire(), guardrails.RateLimitGate)


def test_object_missing_acquire_fails_the_protocol():
    class OnlyTryAcquire:
        def try_acquire(self, tokens=1):
            return True

    assert not isinstance(OnlyTryAcquire(), guardrails.RateLimitGate)


def test_try_acquire_is_non_blocking_and_never_sleeps():
    clock = FakeClock()
    gate = TokenBucketGate(rate=1, capacity=2, clock=clock)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False  # bucket is empty, no time passed
    assert clock.sleeps == []
    assert clock.now == 0.0


def test_acquire_paces_using_the_injected_clock_not_real_time():
    clock = FakeClock()
    gate = TokenBucketGate(rate=2, capacity=1, clock=clock)  # 2 tokens/sec
    assert gate.acquire() == pytest.approx(0.0)  # first token is free
    waited = gate.acquire()  # bucket empty: must wait for 1 token at 2/s
    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]
    assert clock.now == pytest.approx(0.5)


def test_bucket_refills_only_after_the_clock_advances():
    clock = FakeClock()
    gate = TokenBucketGate(rate=1, capacity=1, clock=clock)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False
    clock.advance(1.0)
    assert gate.try_acquire() is True


def test_acquire_never_calls_real_time_sleep(monkeypatch):
    import time

    def _real_sleep_forbidden(*args, **kwargs):
        raise AssertionError("acquire() must pace via the injected clock, not time.sleep")

    monkeypatch.setattr(time, "sleep", _real_sleep_forbidden)
    clock = FakeClock()
    gate = TokenBucketGate(rate=10, capacity=1, clock=clock)
    gate.acquire()
    gate.acquire()  # would need to wait; must use clock.sleep, not time.sleep
