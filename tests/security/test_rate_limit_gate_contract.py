"""Tests for the RateLimitGate contract defined in guardrails.py.

Per the interface contract, the engine lane implements the real token
bucket in testinghq.core.ratelimit, built against this Protocol.

The rest of this suite keeps its own hermetic fake bucket and never imports
the engine package. That separation was written when core/ratelimit.py did
not exist on this branch and could not be imported at all. It still holds,
and it is still the right default: a contract test that exercises the real
implementation is no longer testing the contract, it is testing the
implementation, and the two fail for different reasons.

The section at the bottom is the deliberate exception, added 2026-07-27.
A Protocol that nothing in the tree actually satisfies is a guard that
cannot fail, and this repo's own PR #1 established the rule this follows:
prove the check can go red before you trust it going green. The real bucket
is now on main, so the contract is directly exercisable, and the sweeps
below run it at rates whose reciprocal is not representable in binary,
which is where the engine lane's implementation used to spin forever. A
conformance test that only ever used rates 1 and 2 would have stayed green
through that entire defect.
"""
import pytest

from testinghq.core import guardrails
from testinghq.core.ratelimit import TokenBucket

# Rates whose 1/rate is not exactly representable in binary. Measured against
# the pre-fix engine implementation with an injected additive clock, one
# blocking process per rate: all of these hung. Only 1, 2, 4, 8 and 16, the
# powers of two, plus sub-unit rates with an integer reciprocal, terminated.
NON_BINARY_EXACT_RATES = [3.0, 5.0, 6.0, 7.0, 9.0, 10.0, 11.0]


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


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_acquire_never_calls_real_time_sleep(monkeypatch, rate):
    import time

    def _real_sleep_forbidden(*args, **kwargs):
        raise AssertionError("acquire() must pace via the injected clock, not time.sleep")

    monkeypatch.setattr(time, "sleep", _real_sleep_forbidden)
    clock = FakeClock()
    gate = TokenBucketGate(rate=rate, capacity=1, clock=clock)
    gate.acquire()
    gate.acquire()  # would need to wait; must use clock.sleep, not time.sleep


# ---------------------------------------------------------------------------
# Conformance: the real implementation, driven through the contract.
#
# Everything above this line tests the contract against a fake written to
# match it. That is necessary and it is not sufficient. A fake conforms by
# construction, so no amount of sweeping its rate proves anything about the
# bucket that actually gates firing.
#
# The gap was not hypothetical. TokenBucket.acquire() spun forever under an
# injected clock at any rate whose reciprocal is not binary-exact, and the
# engine lane's own tests missed it because they drove rates 1 and 2 only,
# both exact. The fix moved the wait to an absolute deadline with a
# nanosecond floor; the tests below pin that this lane's own rates, swept
# rather than assumed, terminate and pace correctly.
# ---------------------------------------------------------------------------


class ConformanceClock:
    """Injected clock that also refuses to let a wait go unrecorded.

    `sleep` is the only way time can move here, exactly as the real clock is
    the only way time moves in production. A wait below `floor` cannot
    elapse against a clock reading of this size, so it is refused loudly
    rather than silently no-op'd.
    """

    def __init__(self, floor=1e-9):
        self.now = 0.0
        self.sleeps = []
        self.floor = floor

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert not (0 < seconds < self.floor), (
            f"gate asked for an unpayable wait of {seconds!r}s; it is a "
            "no-op against a clock at this resolution, so a loop retrying "
            "on it never terminates"
        )
        self.sleeps.append(seconds)
        self.now += seconds


def test_real_token_bucket_satisfies_the_frozen_protocol():
    """The contract in guardrails.py is not aspirational. Something in the
    tree implements it, and isinstance says so."""
    assert isinstance(TokenBucket(rate_per_sec=5, capacity=1), guardrails.RateLimitGate)


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_real_token_bucket_terminates_at_non_binary_exact_rates(rate):
    """The defect itself, through the security lane's own rates. This is the
    test that would have gone red on 2026-07-16 instead of the suite
    sitting at 455 green against a rate limiter that could hang forever."""
    clock = ConformanceClock()
    gate = TokenBucket(rate_per_sec=rate, capacity=1, clock=clock.time, sleep=clock.sleep)

    for _ in range(30):
        gate.acquire()

    assert clock.now == pytest.approx(29.0 / rate, rel=1e-9)


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_real_token_bucket_paces_without_real_sleep(monkeypatch, rate):
    """Pacing a load generator is a safety property, not a nicety. If the
    gate ever reached for the real wall clock it would be both untestable
    and ungovernable, so forbid time.sleep outright and prove the gate still
    paces on its injected clock alone."""
    import time

    def _real_sleep_forbidden(*args, **kwargs):
        raise AssertionError("the gate must pace via the injected clock, not time.sleep")

    monkeypatch.setattr(time, "sleep", _real_sleep_forbidden)
    clock = ConformanceClock()
    gate = TokenBucket(rate_per_sec=rate, capacity=1, clock=clock.time, sleep=clock.sleep)

    assert gate.acquire() == pytest.approx(0.0)  # the initial token is free
    waited = gate.acquire()

    assert waited == pytest.approx(1.0 / rate, abs=1e-12)
    assert clock.sleeps == [pytest.approx(1.0 / rate)]


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_real_token_bucket_try_acquire_never_sleeps(monkeypatch, rate):
    """The non-blocking half of the contract. A gate whose try_acquire
    blocks is a gate that can stall a caller who asked not to be blocked,
    so this holds at every swept rate and with the real sleep forbidden."""
    import time

    def _real_sleep_forbidden(*args, **kwargs):
        raise AssertionError("try_acquire() must never sleep")

    monkeypatch.setattr(time, "sleep", _real_sleep_forbidden)
    clock = ConformanceClock()
    gate = TokenBucket(rate_per_sec=rate, capacity=2, clock=clock.time, sleep=clock.sleep)

    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False
    assert clock.sleeps == []
    assert clock.now == 0.0
