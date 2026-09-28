import threading
import time
from pathlib import Path

import pytest

from testinghq.barrage import runner as runner_module
from testinghq.barrage.runner import (
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_RATE_PER_SEC,
    RampStage,
    RateCeilingError,
    RunPlan,
    _pace_and_gate,
    _run_open_loop_stages,
    build_stages,
    check_rate_ceiling,
    ramp_stages,
    run,
)
from testinghq.core.ratelimit import TokenBucket



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


class _InFlightProbe:
    """A `send_fn` that counts how many calls are executing at once.

    The only way to observe concurrency from inside a run. `peak` is the
    high-water mark; `max_allowed` is the configured bound, asserted separately
    so a pool that ignored it fails for the right reason.

    It sleeps in REAL time. A fake clock cannot see overlap, because a fake
    `sleep` returns instantly and a serial loop and a parallel one look
    identical to virtual time. That is precisely why the old serial
    implementation was able to have a test suite that read as coverage.
    """

    def __init__(self, service_seconds: float) -> None:
        self.service_seconds = service_seconds
        self.peak = 0
        self.calls = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, index: int):
        with self._lock:
            self._in_flight += 1
            self.calls += 1
            if self._in_flight > self.peak:
                self.peak = self._in_flight
        try:
            time.sleep(self.service_seconds)
        finally:
            with self._lock:
                self._in_flight -= 1
        return (index, self.service_seconds)


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_one_worker_is_one_request_at_a_time(mode):
    """`concurrency=1` means one request in flight, in both modes.

    Proven with a real thread pool and a `send_fn` that actually blocks, because
    "nothing overlapped" is not observable with a fake clock: a fake `sleep`
    advances virtual time instantly, so a loop that ran everything at once and
    a loop that ran everything in sequence look identical to it. Counting real
    in-flight requests is the only way to see the difference.

    This is the test the old implementation could not have passed. It dispatched
    inline, so the answer was 1 by construction, and no test said so.
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    in_flight = _InFlightProbe(service_seconds=0.02)
    plan = RunPlan(
        mode=mode, rate=200.0, concurrency=1,
        warmup_seconds=0.0, hold_seconds=0.05, ramp_step_count=0,
    )

    records = run(plan, in_flight, clock=clock.now, sleep=sleeper,
                 allow_high_rate=True)

    assert records, "the run dispatched nothing"
    assert in_flight.peak == 1, (
        f"concurrency=1 had {in_flight.peak} requests in flight. One worker is "
        "the specification, not a rounding of it."
    )


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_more_workers_really_means_more_requests_in_flight(mode):
    """The load-bearing property, and the one issue #38 is about.

    Before the executor, `concurrency` sized an arithmetic list and nothing
    else: `concurrency=4` and `concurrency=400` produced byte-identical dispatch
    records, because the loop always called `send_fn` inline. A load tester that
    never has more than one request in flight measures the target's response
    time and calls it throughput.

    Asserted as `>= 2` rather than `== 4` on purpose. Exactly-four is a real
    time and thread-scheduling assertion, and a test that flakes on a busy
    runner is a test that gets disabled. The claim worth making is that the
    number went UP, which is the whole defect, and that it never exceeded the
    configured concurrency, which is the guarantee.
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    in_flight = _InFlightProbe(service_seconds=0.02)
    plan = RunPlan(
        mode=mode, rate=400.0, concurrency=4,
        warmup_seconds=0.0, hold_seconds=0.1, ramp_step_count=0,
    )

    records = run(plan, in_flight, clock=clock.now, sleep=sleeper,
                 allow_high_rate=True)

    assert records, "the run dispatched nothing"
    assert in_flight.peak >= 2, (
        f"concurrency=4 never had more than {in_flight.peak} request in "
        "flight, so --concurrency still does nothing. This is issue #38."
    )
    assert in_flight.peak <= 4, (
        f"concurrency=4 had {in_flight.peak} in flight, which is more than the "
        "configured concurrency. The pool is the bound."
    )


def test_a_slow_target_no_longer_caps_the_achieved_rate_at_its_response_time():
    """The specific lie the old implementation told, stated as a number.

    A target that takes 200ms per request, offered 10 requests/second: the old
    loop could not exceed 5/second, because it was blocked on each request
    before scheduling the next. Five would have looked like a measurement of
    the target. It was a measurement of this loop.

    The same shape now goes out at the requested rate, because arrival is
    decoupled from completion. Asserted on dispatch times, which are recorded
    at submit and are what the throughput report buckets.
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    plan = RunPlan(
        mode="open", rate=50.0, concurrency=8,
        warmup_seconds=0.0, hold_seconds=0.4, ramp_step_count=0,
    )

    records = run(plan, _InFlightProbe(service_seconds=0.001),
                  clock=clock.now, sleep=sleeper, allow_high_rate=True)

    # budget = rate * duration = 50 * 0.4 = 20 requests
    assert len(records) == 20
    gaps = [b.dispatch_time - a.dispatch_time for a, b in zip(records, records[1:])]
    # Every arrival is 1/50s = 0.02s apart on the schedule.
    assert all(g == pytest.approx(0.02, abs=1e-9) for g in gaps), (
        f"arrivals drifted: {gaps[:5]}. A 0.001s service time must not move the "
        "schedule, and a 0.2s one must not move it either."
    )


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
# TokenBucket.acquire() could not be driven to completion by an injected,
# purely additive clock when the rate's reciprocal is not exactly
# representable in binary: the refill rounded to just under the deficit, the
# next computed wait was ~1e-17, and adding that to a clock reading ~0.67 is
# a no-op at float precision, so the bucket's internal wait loop spun
# forever. Rates 2 and 4 are exactly representable and never trip it, which
# is why the steady-state tests above pass; a ramp to 10 in 5 steps produces
# rates 2, 4, 6, 8, 10 and rate 6 hung the whole run.
#
# These tests pin the ramp path. They are kept, and kept strict, after the
# 2026-07-27 fix in core/ratelimit.py, for two reasons that survive that
# fix. First, a load generator whose rate ramp can block forever is a real
# defect regardless of whose arithmetic caused it: this is the module whose
# entire safety story is that it paces predictably and stops when told.
# Second, Barrage does not rely on acquire() blocking, so these tests no
# longer prove the core fix; they prove this lane's own schedule arithmetic
# terminates on its own. That is a separate property, and a fix upstream
# could have regressed this path without turning any of it red.
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


# ---------------------------------------------------------------------------
# The rate gate.
#
# docs/decisions/0001-barrage-pacing.md keeps the TokenBucket in
# barrage/runner.py even though the schedule is the pacer, on the grounds that
# the gate is a check which fires when the schedule is wrong.
#
# That argument is only worth something if the gate can fail, and it is worth
# exactly as much as the precision of "when the schedule is wrong". Both halves
# are pinned here. An assertion that the gate never fires, on its own, is
# compatible with a gate that does nothing at all, so the second test is the one
# that carries the argument.
# ---------------------------------------------------------------------------


def test_the_gate_never_waits_when_the_schedule_holds():
    """The invariant the decision rests on: every dispatch is on schedule, the
    bucket is in credit before every acquire, and `waited` is exactly zero for
    all of them.

    Asserted as equality to 0.0 rather than "small", because a gate that waited
    a little on every call would still look fine in a throughput report while
    quietly having become a second pacer."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)

    # Through `run`, not through `_run_open_loop_stages`. The private helper's
    # signature changed when the executor arrived, and a test that reaches into
    # a private function is a test that breaks on a refactor while proving
    # nothing about the contract anybody actually uses. `concurrency=1` takes
    # the serial path, so this is still a single-threaded, hermetic run.
    plan = RunPlan(
        mode="open", rate=5.0, concurrency=1,
        warmup_seconds=0.0, hold_seconds=3.0, ramp_step_count=0,
    )
    records = run(
        plan, lambda index: ("ok", 0.0), clock=clock.now, sleep=sleeper
    )

    assert records, "the run dispatched nothing"
    assert [r.waited for r in records] == [0.0] * len(records), (
        "the gate blocked during a correctly paced run, which would mean the "
        "schedule and the bucket disagree about the rate"
    )
    # And the schedule really was what paced this, not the gate: dispatches
    # landed on 1/rate intervals.
    expected = [i * (1.0 / 5.0) for i in range(len(records))]
    assert [r.dispatch_time for r in records] == pytest.approx(expected, abs=1e-9)


def test_the_gate_blocks_when_tokens_are_taken_faster_than_they_are_earned():
    """The other half, and the reason the gate is worth keeping.

    Driven through `_pace_and_gate` rather than by calling `TokenBucket`
    directly. An earlier version of this test constructed the bucket itself,
    which proved the bucket can block and said nothing about whether the gate
    is wired to it: replacing the body of `_pace_and_gate` with `return 0.0`
    left that version green. This one goes through the gate the dispatch loops
    actually call, so neutering the gate turns it red.

    The state is twelve tokens taken at 5/s with no time passing, which is
    exactly what a bug in the schedule's interval arithmetic would produce."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(
        rate_per_sec=5.0,
        capacity=runner_module._GATE_CAPACITY,
        clock=clock.now,
        sleep=sleeper,
    )
    headroom = int(runner_module._GATE_CAPACITY)

    waits = [_pace_and_gate(bucket) for _ in range(12)]

    assert waits[:headroom] == [0.0] * headroom, (
        "the bucket's headroom should be free tokens, since that is what it is for"
    )
    assert any(w > 0 for w in waits), (
        "the gate let a 5/s bucket hand out 12 tokens with no time passing, so "
        "it cannot catch a pacing bug and is not the independent check the "
        "decision note claims"
    )
    # It throttles to the configured rate, not to some other value: 12 tokens
    # at 5/s less the free headroom takes that many seconds to earn.
    assert sum(waits) == pytest.approx((12 - headroom) / 5.0, abs=1e-6)




def test_the_gate_and_the_schedule_read_the_rate_from_different_expressions():
    """What the gate can and cannot catch, pinned so nobody overclaims it.

    The two mechanisms derive the rate from `stage.rate`, but by different
    routes: the schedule divides to get an interval, the bucket is handed the
    rate directly. So a mistake in one is not automatically a mistake in the
    other, and the gate does catch an interval bug.

    What it does NOT catch is a disagreement about the rate, because both read
    the same field. This asserts that shared origin, so the day someone routes
    the bucket's rate through a different source, whoever reads this learns
    that the gate's scope grew.
    """
    source = (Path(__file__).resolve().parents[3] / "testinghq" / "barrage" / "runner.py")
    text = source.read_text(encoding="utf-8-sig")

    # the schedule derives an interval by division
    assert "interval = 1.0 / stage.rate" in text, (
        "the schedule's interval is no longer derived by division; re-check "
        "what the gate is now able to catch"
    )
    # the bucket is handed the rate itself
    assert text.count("rate_per_sec=stage.rate") >= 2, (
        "both dispatch loops should hand the bucket stage.rate directly; if one "
        "has changed source, the gate no longer reads the same field as the "
        "schedule and its scope has changed"
    )
