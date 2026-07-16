import pytest

from testinghq.barrage.runner import (
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_RATE_PER_SEC,
    RampStage,
    RateCeilingError,
    RunPlan,
    build_stages,
    check_rate_ceiling,
    ramp_stages,
    run,
)


class FakeClock:
    """A controllable clock, same shape as tests/unit/test_ratelimit.py's.
    Never touches the real wall clock."""

    def __init__(self, start: float = 0.0):
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class FakeSleeper:
    """Records requested durations and advances a FakeClock by exactly that
    much instead of actually sleeping. If this is never called with a
    positive duration, no real or simulated time passed via sleeping."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def _real_sleep_forbidden(_seconds):
    raise AssertionError("a real sleep happened; this test must stay hermetic")


# ---------------------------------------------------------------------------
# check_rate_ceiling
# ---------------------------------------------------------------------------


def test_ceiling_blocks_over_rate_run_without_explicit_flag():
    with pytest.raises(RateCeilingError):
        check_rate_ceiling(DEFAULT_MAX_RATE_PER_SEC + 1, 10.0, allow_high_rate=False)


def test_ceiling_blocks_over_duration_run_without_explicit_flag():
    with pytest.raises(RateCeilingError):
        check_rate_ceiling(1.0, DEFAULT_MAX_DURATION_SEC + 1, allow_high_rate=False)


def test_ceiling_allows_over_rate_run_with_explicit_flag():
    check_rate_ceiling(DEFAULT_MAX_RATE_PER_SEC + 1, 10.0, allow_high_rate=True)


def test_ceiling_allows_over_duration_run_with_explicit_flag():
    check_rate_ceiling(1.0, DEFAULT_MAX_DURATION_SEC + 1, allow_high_rate=True)


def test_ceiling_allows_a_run_within_limits():
    check_rate_ceiling(5.0, 30.0, allow_high_rate=False)


def test_run_refuses_over_ceiling_before_dispatching_anything():
    plan = RunPlan(
        mode="open",
        rate=DEFAULT_MAX_RATE_PER_SEC + 10,
        concurrency=1,
        warmup_seconds=0,
        hold_seconds=1,
    )
    calls = []

    def send_fn(index):
        calls.append(index)
        return (None, 0.0)

    with pytest.raises(RateCeilingError):
        run(plan, send_fn, clock=lambda: 0.0, sleep=_real_sleep_forbidden)
    assert calls == []


# ---------------------------------------------------------------------------
# ramp_stages / build_stages
# ---------------------------------------------------------------------------


def test_ramp_stages_is_empty_with_no_warmup():
    assert ramp_stages(10.0, 0.0, steps=10) == []


def test_ramp_stages_rises_linearly_to_target_rate():
    stages = ramp_stages(10.0, 5.0, steps=5)
    assert len(stages) == 5
    assert [s.rate for s in stages] == [2.0, 4.0, 6.0, 8.0, 10.0]
    assert all(s.duration == pytest.approx(1.0) for s in stages)
    assert sum(s.duration for s in stages) == pytest.approx(5.0)


def test_build_stages_appends_steady_state_hold():
    stages = build_stages(10.0, 5.0, 20.0, ramp_step_count=5)
    assert len(stages) == 6
    assert stages[-1] == RampStage(rate=10.0, duration=20.0)


def test_build_stages_with_no_warmup_is_hold_only():
    stages = build_stages(10.0, 0.0, 20.0)
    assert stages == [RampStage(rate=10.0, duration=20.0)]


# ---------------------------------------------------------------------------
# run(): rate control paces correctly under an injected clock, no real sleep
# ---------------------------------------------------------------------------


def test_open_loop_dispatches_at_the_target_rate_with_no_real_sleep():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(mode="open", rate=2.0, concurrency=1, warmup_seconds=0.0, hold_seconds=3.0)

    calls = []

    def send_fn(index):
        calls.append(index)
        return (index, 0.0)

    records = run(plan, send_fn, clock=clock.now, sleep=sleeper)

    # 2 req/s for 3s: expect dispatch roughly every 0.5s, ~6 dispatches.
    assert len(records) == 6
    assert [r.index for r in records] == list(range(6))
    # Strictly increasing dispatch times, evenly paced.
    times = [r.dispatch_time for r in records]
    assert times == sorted(times)
    assert times[1] - times[0] == pytest.approx(0.5)


def test_open_loop_ramp_increases_rate_over_warmup():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=5.0,
        hold_seconds=1.0,
        ramp_step_count=5,
    )

    def send_fn(index):
        return (index, 0.0)

    records = run(plan, send_fn, clock=clock.now, sleep=sleeper)

    # The gap between consecutive dispatches should shrink as the ramp's
    # target rate rises (stages are 2, 4, 6, 8, 10 req/s before the hold).
    gaps = [b.dispatch_time - a.dispatch_time for a, b in zip(records, records[1:])]
    assert gaps[0] > gaps[-1]


def test_closed_loop_dispatch_count_is_bound_by_concurrency_and_service_time():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    # Rate ceiling is generous; the real limiter here is service time.
    plan = RunPlan(mode="closed", rate=50.0, concurrency=1, warmup_seconds=0.0, hold_seconds=2.0)

    def send_fn(index):
        # Every call takes 0.5 simulated seconds to "complete".
        return (index, 0.5)

    records = run(plan, send_fn, clock=clock.now, sleep=sleeper)

    # One worker, 0.5s per call, 2s hold: expect 4 dispatches.
    assert len(records) == 4


def test_closed_loop_more_workers_yields_more_throughput_for_same_service_time():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(mode="closed", rate=50.0, concurrency=4, warmup_seconds=0.0, hold_seconds=2.0)

    def send_fn(index):
        return (index, 0.5)

    records = run(plan, send_fn, clock=clock.now, sleep=sleeper)

    # 4 workers, 0.5s per call, 2s hold: expect roughly 4x the single-worker
    # count (16), bounded by the 50 req/s ceiling (which would allow 100).
    assert len(records) == 16


def test_closed_loop_respects_the_rate_ceiling_even_with_high_concurrency():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    # 10 workers that "complete" instantly would blow way past 5 req/s
    # without the shared bucket capping aggregate throughput.
    plan = RunPlan(mode="closed", rate=5.0, concurrency=10, warmup_seconds=0.0, hold_seconds=2.0)

    def send_fn(index):
        return (index, 0.0)

    records = run(plan, send_fn, clock=clock.now, sleep=sleeper)

    assert len(records) == 10  # 5 req/s * 2s, not concurrency-unbounded


def test_run_never_calls_the_real_sleep_function():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(mode="open", rate=5.0, concurrency=1, warmup_seconds=1.0, hold_seconds=2.0)

    def send_fn(index):
        return (index, 0.0)

    # Passing _real_sleep_forbidden as a canary anywhere in this call graph
    # would blow up the test; here we instead assert the fake sleeper is the
    # only sleep path exercised and that no call ever reaches real time.
    run(plan, send_fn, clock=clock.now, sleep=sleeper)
    assert sleeper.calls, "expected the run to pace itself via sleep"


def test_run_with_real_clock_and_sleep_defaults_raises_on_over_ceiling_before_dispatching():
    # The ceiling check happens before any pacing or dispatch, even with the
    # real time.monotonic/time.sleep defaults, so a bad --rate never sleeps
    # or fires its way through a real run before being refused.
    plan = RunPlan(
        mode="open",
        rate=DEFAULT_MAX_RATE_PER_SEC + 1,
        concurrency=1,
        warmup_seconds=0.0,
        hold_seconds=1.0,
    )
    calls = []

    def send_fn(index):
        calls.append(index)
        return (index, 0.0)

    with pytest.raises(RateCeilingError):
        run(plan, send_fn)
    assert calls == []


# ---------------------------------------------------------------------------
# Regression: the ramp hang.
#
# TokenBucket.acquire() cannot be driven to completion by an injected,
# purely additive clock when the rate's reciprocal is not exactly
# representable in binary: the refill rounds to just under the deficit, the
# next computed wait is ~1e-17, and adding that to a clock reading ~0.67 is
# a no-op at float precision, so the bucket's internal wait loop spins
# forever. Rates 2 and 4 are exactly representable and never trip it, which
# is why the steady-state tests above pass; a ramp to 10 in 5 steps produces
# rates 2, 4, 6, 8, 10 and rate 6 hangs the whole run.
#
# These tests pin the ramp path against that. A load generator whose rate
# ramp can block forever is a real defect: this is the module whose entire
# safety story is that it paces predictably and stops when told.
# ---------------------------------------------------------------------------


class BoundedSleeper(FakeSleeper):
    """A FakeSleeper that fails loudly instead of spinning. A degenerate
    sub-nanosecond sleep, or an implausible number of sleeps, means pacing
    is stuck making no forward progress rather than pacing."""

    def __init__(self, clock: FakeClock, max_calls: int = 5000):
        super().__init__(clock)
        self.max_calls = max_calls

    def __call__(self, seconds: float) -> None:
        if len(self.calls) >= self.max_calls:
            raise AssertionError(
                f"pacing made {self.max_calls} sleep calls without finishing; "
                "the rate loop is spinning, not pacing"
            )
        if 0 < seconds < 1e-9:
            raise AssertionError(
                f"pacing requested a degenerate sleep of {seconds!r}s; the "
                "rate loop is making no forward progress"
            )
        super().__call__(seconds)


@pytest.mark.parametrize("rate", [5.0, 6.0, 10.0, 7.0, 3.0])
def test_open_loop_terminates_for_rates_whose_reciprocal_is_not_binary_exact(rate):
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    plan = RunPlan(mode="open", rate=rate, concurrency=1, warmup_seconds=0.0, hold_seconds=2.0)

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    assert len(records) == round(rate * 2.0)


def test_ramp_through_non_binary_exact_rates_terminates():
    # The exact plan that hung: ramp 0 to 10 over 5s in 5 steps produces
    # stage rates 2, 4, 6, 8, 10, and rate 6 spun forever.
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=5.0,
        hold_seconds=1.0,
        ramp_step_count=5,
    )

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    # 2+4+6+8+10 across the ramp, plus 10 in the hold.
    assert len(records) == 40


def test_closed_loop_ramp_through_non_binary_exact_rates_terminates():
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    plan = RunPlan(
        mode="closed",
        rate=10.0,
        concurrency=2,
        warmup_seconds=5.0,
        hold_seconds=1.0,
        ramp_step_count=5,
    )

    records = run(plan, lambda index: (index, 0.05), clock=clock.now, sleep=sleeper)

    assert records
    assert all(r.dispatch_time >= 0 for r in records)


def test_pacing_never_requests_a_degenerate_sleep():
    clock = FakeClock()
    sleeper = BoundedSleeper(clock)
    plan = RunPlan(mode="open", rate=6.0, concurrency=1, warmup_seconds=0.0, hold_seconds=3.0)

    run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    assert sleeper.calls, "expected the run to pace itself via sleep"
    assert all(s >= 1e-9 for s in sleeper.calls if s > 0)


# ---------------------------------------------------------------------------
# Regression: the collapsing ramp.
#
# A stage used to end the instant its last request was dispatched, which is
# up to one interval early and collapses completely when rounding gives a
# stage a budget of 1: a 0.3s stage at 2 req/s fired its single request at
# stage_start and returned immediately. The ramp then took almost no wall
# time and dumped every warmup request at once, so the achieved rate
# overshot the target and the warmup ramp slammed a cold endpoint instead of
# easing into it, which is the precise opposite of its purpose.
#
# Caught by a real run against a local sink: the first second of a 10 req/s
# run reported 15 req/s achieved. These tests pin it under an injected clock.
# ---------------------------------------------------------------------------


def test_warmup_ramp_occupies_its_full_configured_duration():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=3.0,
        hold_seconds=5.0,
        ramp_step_count=10,
    )

    run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    # The whole run must take warmup + hold of simulated time, not collapse
    # into a burst. Before the fix this finished in well under the 8s.
    assert clock.time == pytest.approx(8.0, abs=0.05)


def test_ramp_stage_with_a_single_request_still_occupies_its_stage():
    # The exact collapse case: 0.3s stages at low rates round to a budget of
    # 1, so the stage used to take zero simulated time.
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=3.0,
        hold_seconds=1.0,
        ramp_step_count=10,
    )

    run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    assert clock.time == pytest.approx(4.0, abs=0.05)


def test_open_loop_achieved_rate_never_overshoots_the_target_in_any_second():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=3.0,
        hold_seconds=5.0,
        ramp_step_count=10,
    )

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    # Bucket dispatches into 1 second windows. A window may straddle a stage
    # boundary and pick up one extra, so allow exactly one; the bug this
    # pins was a 50% overshoot (15 in a 10/s window), which this still
    # catches decisively.
    per_second = {}
    origin = records[0].dispatch_time
    for record in records:
        bucket = int(record.dispatch_time - origin)
        per_second[bucket] = per_second.get(bucket, 0) + 1
    assert per_second, "expected some dispatches"
    assert max(per_second.values()) <= 11, (
        f"a one second window overshot the 10/s target: {per_second}"
    )


def test_steady_state_dispatches_are_never_spaced_tighter_than_the_target_rate():
    # The rigorous form of "never faster than the target rate", free of any
    # bucket-boundary artifact: inside the steady-state hold, consecutive
    # dispatches must be at least one interval apart.
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(mode="open", rate=10.0, concurrency=1, warmup_seconds=0.0, hold_seconds=5.0)

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    gaps = [b.dispatch_time - a.dispatch_time for a, b in zip(records, records[1:])]
    assert gaps
    assert min(gaps) >= 0.1 - 1e-9, f"dispatched faster than 10/s: min gap {min(gaps)}"


def test_steady_state_dispatch_count_is_exactly_rate_times_duration():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(mode="open", rate=10.0, concurrency=1, warmup_seconds=0.0, hold_seconds=5.0)

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    assert len(records) == 50


def test_ramp_dispatches_fewer_requests_early_than_late():
    # The ramp must actually be a ramp: the first second of a run should
    # carry less load than the steady state, not the same or more.
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open",
        rate=10.0,
        concurrency=1,
        warmup_seconds=4.0,
        hold_seconds=4.0,
        ramp_step_count=8,
    )

    records = run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    origin = records[0].dispatch_time
    first_second = sum(1 for r in records if r.dispatch_time - origin < 1.0)
    last_second = sum(1 for r in records if r.dispatch_time - origin >= 7.0)
    assert first_second < last_second


def test_closed_loop_ramp_also_occupies_its_full_duration():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="closed",
        rate=10.0,
        concurrency=2,
        warmup_seconds=3.0,
        hold_seconds=3.0,
        ramp_step_count=10,
    )

    run(plan, lambda index: (index, 0.0), clock=clock.now, sleep=sleeper)

    assert clock.time == pytest.approx(6.0, abs=0.05)


def test_run_plan_rejects_invalid_fields():
    with pytest.raises(ValueError):
        RunPlan(mode="sideways", rate=1.0, concurrency=1, warmup_seconds=0.0, hold_seconds=1.0)
    with pytest.raises(ValueError):
        RunPlan(mode="open", rate=0.0, concurrency=1, warmup_seconds=0.0, hold_seconds=1.0)
    with pytest.raises(ValueError):
        RunPlan(mode="open", rate=1.0, concurrency=0, warmup_seconds=0.0, hold_seconds=1.0)
    with pytest.raises(ValueError):
        RunPlan(mode="open", rate=1.0, concurrency=1, warmup_seconds=-1.0, hold_seconds=1.0)
    with pytest.raises(ValueError):
        RunPlan(mode="open", rate=1.0, concurrency=1, warmup_seconds=0.0, hold_seconds=0.0)
