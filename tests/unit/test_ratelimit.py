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
