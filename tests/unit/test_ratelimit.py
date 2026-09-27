import pytest

from testinghq.core.ratelimit import TokenBucket


class FakeClock:
    """A controllable clock. `advance` moves time forward; `now` reads it.
    Never touches the real wall clock."""

    def __init__(self, start: float = 0.0):
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class FakeSleeper:
    """A sleep() stand-in that records requested durations and advances a
    FakeClock by exactly that much instead of actually sleeping."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def test_try_acquire_succeeds_while_tokens_available():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=1, capacity=3, clock=clock.now, sleep=lambda s: None)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True


def test_try_acquire_fails_when_bucket_is_empty():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=1, capacity=1, clock=clock.now, sleep=lambda s: None)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_try_acquire_refills_over_simulated_time():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=2, capacity=2, clock=clock.now, sleep=lambda s: None)
    assert bucket.try_acquire(tokens=2) is True
    assert bucket.try_acquire() is False
    clock.advance(0.5)  # 2 tokens/sec * 0.5s = 1 token
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_try_acquire_never_exceeds_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=10, capacity=2, clock=clock.now, sleep=lambda s: None)
    clock.advance(100)  # would overflow capacity without clamping
    assert bucket.try_acquire(tokens=2) is True
    assert bucket.try_acquire() is False


def test_try_acquire_rejects_non_positive_tokens():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=1, capacity=1, clock=clock.now, sleep=lambda s: None)
    with pytest.raises(ValueError):
        bucket.try_acquire(tokens=0)


def test_acquire_returns_zero_when_tokens_already_available():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=1, capacity=5, clock=clock.now, sleep=lambda s: None)
    waited = bucket.acquire()
    assert waited == 0.0


def test_acquire_paces_via_injected_sleep_without_real_time_passing():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(rate_per_sec=1, capacity=1, clock=clock.now, sleep=sleeper)

    bucket.acquire()  # drains the single token
    waited = bucket.acquire()  # must wait ~1 second for the next token

    assert waited == pytest.approx(1.0)
    assert sleeper.calls  # sleep was actually invoked to pace the caller
    assert sum(sleeper.calls) == pytest.approx(1.0)


def test_acquire_paces_correctly_for_multiple_tokens():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(rate_per_sec=2, capacity=4, clock=clock.now, sleep=sleeper)

    bucket.acquire(tokens=4)  # drains the bucket
    waited = bucket.acquire(tokens=2)  # 2 tokens at 2/sec = 1 second

    assert waited == pytest.approx(1.0)


def test_acquire_rejects_request_larger_than_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=1, capacity=3, clock=clock.now, sleep=lambda s: None)
    with pytest.raises(ValueError):
        bucket.acquire(tokens=4)


def test_default_clock_and_sleep_are_time_module():
    import time

    bucket = TokenBucket(rate_per_sec=1, capacity=1)
    assert bucket._clock is time.monotonic
    assert bucket._sleep is time.sleep


def test_rejects_non_positive_rate_or_capacity():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=1, capacity=0)


# ---------------------------------------------------------------------------
# Regression: the float-residue spin in acquire().
#
# acquire() used to recompute the wait on every pass and re-accumulate it. For
# a rate whose reciprocal is not exactly representable in binary (1/3, 1/6,
# 1/7, 1/10) the refill rounded to just under the deficit, so the next computed
# wait was about 1e-17, and adding 1e-17 to a clock reading around 0.67 is a
# no-op at float precision. Elapsed became zero, no refill happened, and the
# loop never exited. Rates 1, 2 and 4 are exact and never trip it, which is why
# the blocking-acquire tests above pass.
#
# Every test guarding this module used to pass for a reason unrelated to
# whether it worked: the blocking tests drive rates 1 and 2 only, and the one
# test using a non-representable rate (10) drives it through the non-blocking
# try_acquire(), which cannot spin. Nobody chose 1 and 2 for any reason at all.
# The sweep below is the correction: the rate is a parameter of the test, not
# a coincidence of it.
# ---------------------------------------------------------------------------

# 3, 5, 6, 7, 9, 10 and 11. Measured against the pre-fix module, one
# blocking process per rate: every one of these hangs, and the only rates
# that did not are 1, 2, 4, 8, 16 (the powers of two) plus 0.5 and 0.1. So the
# dividing line is exactly representability of the reciprocal, and the
# existing tests sat entirely on the safe side of it.
NON_BINARY_EXACT_RATES = [3.0, 5.0, 6.0, 7.0, 9.0, 10.0, 11.0]


class BoundedSleeper(FakeSleeper):
    """A FakeSleeper that fails loudly instead of spinning.

    A degenerate sub-nanosecond sleep, or an implausible number of sleep
    calls, means acquire() is making no forward progress rather than waiting.
    Without this guard a regression surfaces as a hung test run instead of a
    failed test, which is the worst available signal: the suite simply stops.
    """

    def __init__(self, clock: FakeClock, max_calls: int = 1000):
        super().__init__(clock)
        self.max_calls = max_calls

    def __call__(self, seconds: float) -> None:
        if len(self.calls) >= self.max_calls:
            raise AssertionError(
                f"acquire() made {self.max_calls} sleep calls without "
                "finishing; the wait loop is spinning, not waiting"
            )
        if 0 < seconds < 1e-9:
            raise AssertionError(
                f"acquire() requested a degenerate sleep of {seconds!r}s; the "
                "wait loop is making no forward progress"
            )
        super().__call__(seconds)


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_acquire_terminates_when_reciprocal_is_not_binary_exact(rate):
    """The spin itself. 30 blocking calls on a bucket holding one token, at
    a rate whose 1/rate is not representable in binary."""
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    bucket = TokenBucket(rate_per_sec=rate, capacity=1, clock=clock.now, sleep=sleeper)

    for _ in range(30):
        bucket.acquire()

    # One sleep per earned token, not a loop's worth of them.
    assert len(sleeper.calls) <= 30


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_acquire_waits_the_full_earned_time_at_non_exact_rates(rate):
    """Terminating is necessary and not sufficient. The bucket must still
    actually wait for the token, not round the wait away to make the loop
    exit.

    BoundedSleeper rather than FakeSleeper throughout this section: a test
    that hangs on a regression reports nothing at all, and a suite that
    stops mid-run is a worse signal than a red one."""
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    bucket = TokenBucket(rate_per_sec=rate, capacity=1, clock=clock.now, sleep=sleeper)

    bucket.acquire()  # the initial token is free
    waited = bucket.acquire()  # this one has to be paid for

    assert waited == pytest.approx(1.0 / rate, abs=1e-12)
    assert clock.time == pytest.approx(1.0 / rate, abs=1e-12)


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_acquire_does_not_round_away_real_time_over_many_calls(rate):
    """The fix absorbs an unpayable sliver, not actual waiting. If the
    tolerance ever grew large enough to swallow real time, the bucket would
    silently pace faster than configured and the requested rate would stop
    meaning anything. 99 paid waits after the free initial token."""
    clock = FakeClock()
    bucket = TokenBucket(
        rate_per_sec=rate, capacity=1, clock=clock.now, sleep=BoundedSleeper(clock)
    )

    for _ in range(100):
        bucket.acquire()

    assert clock.time == pytest.approx(99.0 / rate, rel=1e-9)


@pytest.mark.parametrize("rate", NON_BINARY_EXACT_RATES)
def test_acquire_terminates_for_a_multi_token_request_at_non_exact_rates(rate):
    """Same defect, reached through a different door. A multi-token request
    is not just `tokens` times the single-token case: the deficit divides
    into a different float, and a bucket of capacity 7 at rate 3 is a shape
    the old loop had never been asked to close."""
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    bucket = TokenBucket(rate_per_sec=rate, capacity=7, clock=clock.now, sleep=sleeper)

    for _ in range(20):
        bucket.acquire(tokens=5)

    assert clock.time == pytest.approx((20 * 5 - 7) / rate, rel=1e-9)


def test_acquire_never_asks_the_clock_for_less_than_a_nanosecond():
    """The termination path is guarded by an explicit floor. Pin it directly
    so that a future change to that floor has to be a deliberate change to
    this test rather than a quiet loosening of the pacing."""
    from testinghq.core.ratelimit import _MIN_WAIT

    assert _MIN_WAIT == 1e-9
