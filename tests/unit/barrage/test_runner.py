import threading
import time

import pytest

from testinghq.barrage import runner as runner_module
from testinghq.barrage.executor import PoolExecutor, SerialExecutor
from testinghq.barrage.runner import (
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_RATE_PER_SEC,
    RampStage,
    RateCeilingError,
    RunPlan,
    _pace_and_gate,
    arrival_schedule,
    build_stages,
    check_rate_ceiling,
    ramp_stages,
    run,
)
from testinghq.core.ratelimit import TokenBucket


class FakeClock:
    """A controllable clock. Never touches the real wall clock."""

    def __init__(self, start: float = 0.0):
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class FakeSleeper:
    """Advances a FakeClock by exactly the requested amount."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def _real_sleep_forbidden(_seconds):
    raise AssertionError("a real sleep happened; this test must stay hermetic")


def _instant(index):
    return index


def _plan(**overrides):
    fields = dict(mode="open", rate=10.0, concurrency=1, warmup_seconds=0.0,
                  hold_seconds=1.0, ramp_step_count=0)
    fields.update(overrides)
    return RunPlan(**fields)


# ---------------------------------------------------------------------------
# check_rate_ceiling
# ---------------------------------------------------------------------------


def test_ceiling_blocks_over_rate_run_without_explicit_flag():
    with pytest.raises(RateCeilingError):
        check_rate_ceiling(DEFAULT_MAX_RATE_PER_SEC + 1, 10.0, allow_high_rate=False)


def test_ceiling_blocks_over_duration_run_without_explicit_flag():
    with pytest.raises(RateCeilingError):
        check_rate_ceiling(1.0, DEFAULT_MAX_DURATION_SEC + 1, allow_high_rate=False)


def test_ceiling_allows_over_limits_with_explicit_flag():
    check_rate_ceiling(DEFAULT_MAX_RATE_PER_SEC + 1, 10.0, allow_high_rate=True)
    check_rate_ceiling(1.0, DEFAULT_MAX_DURATION_SEC + 1, allow_high_rate=True)


def test_ceiling_allows_a_run_within_limits():
    check_rate_ceiling(5.0, 30.0, allow_high_rate=False)


def test_run_refuses_over_ceiling_before_dispatching_anything():
    calls = []

    def send_fn(index):
        calls.append(index)
        return None

    with pytest.raises(RateCeilingError):
        run(_plan(rate=DEFAULT_MAX_RATE_PER_SEC + 10), send_fn,
            clock=lambda: 0.0, sleep=_real_sleep_forbidden)
    assert calls == []


# ---------------------------------------------------------------------------
# Stages and the open-loop schedule
# ---------------------------------------------------------------------------


def test_ramp_stages_is_empty_with_no_warmup():
    assert ramp_stages(10.0, 0.0, steps=10) == []


def test_ramp_stages_rises_linearly_to_target_rate():
    stages = ramp_stages(10.0, 5.0, steps=5)
    assert [s.rate for s in stages] == [2.0, 4.0, 6.0, 8.0, 10.0]
    assert sum(s.duration for s in stages) == pytest.approx(5.0)


def test_build_stages_appends_steady_state_hold():
    stages = build_stages(10.0, 5.0, 20.0, ramp_step_count=5)
    assert len(stages) == 6
    assert stages[-1] == RampStage(rate=10.0, duration=20.0)


def test_arrival_schedule_is_continuous_across_stages():
    """Each stage starts where the previous one nominally ended, so the ramp's
    arrivals are a pure function of the plan."""
    schedule = arrival_schedule([RampStage(2.0, 1.0), RampStage(4.0, 1.0)])
    assert [a.offset for a in schedule] == pytest.approx([0.0, 0.5, 1.0, 1.25, 1.5, 1.75])
    assert [a.rate for a in schedule] == [2.0, 2.0, 4.0, 4.0, 4.0, 4.0]
    assert [a.index for a in schedule] == list(range(6))


# ---------------------------------------------------------------------------
# Open loop under an injected clock
# ---------------------------------------------------------------------------


def test_open_loop_dispatches_on_schedule_with_no_real_sleep():
    clock = FakeClock()
    result = run(_plan(rate=2.0, hold_seconds=3.0), _instant, clock=clock.now,
                 sleep=FakeSleeper(clock))
    records = result.records
    assert [r.index for r in records] == list(range(6))
    assert [r.dispatched_at for r in records] == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
    assert [r.result for r in records] == list(range(6))
    assert all(r.outcome == "completed" for r in records)
    assert result.aborted is False


def test_send_fn_returns_one_value_and_it_is_the_record_result():
    clock = FakeClock()
    result = run(_plan(hold_seconds=0.3), lambda i: {"n": i}, clock=clock.now,
                 sleep=FakeSleeper(clock))
    assert [r.result for r in result.records] == [{"n": 0}, {"n": 1}, {"n": 2}]


def test_open_loop_ramp_increases_rate_over_warmup():
    clock = FakeClock()
    records = run(
        _plan(warmup_seconds=5.0, hold_seconds=1.0, ramp_step_count=5),
        _instant, clock=clock.now, sleep=FakeSleeper(clock),
    ).records
    gaps = [b.dispatched_at - a.dispatched_at for a, b in zip(records, records[1:])]
    assert gaps[0] > gaps[-1]


def test_warmup_ramp_occupies_its_full_configured_duration():
    clock = FakeClock()
    run(_plan(warmup_seconds=3.0, hold_seconds=5.0, ramp_step_count=10),
        _instant, clock=clock.now, sleep=FakeSleeper(clock))
    assert clock.time == pytest.approx(8.0, abs=0.05)


def test_steady_state_dispatch_count_is_exactly_rate_times_duration():
    clock = FakeClock()
    records = run(_plan(hold_seconds=5.0), _instant, clock=clock.now,
                  sleep=FakeSleeper(clock)).records
    assert len(records) == 50
    gaps = [b.dispatched_at - a.dispatched_at for a, b in zip(records, records[1:])]
    assert min(gaps) >= 0.1 - 1e-9


def test_open_loop_achieved_rate_never_overshoots_the_target_in_any_second():
    clock = FakeClock()
    records = run(_plan(warmup_seconds=3.0, hold_seconds=5.0, ramp_step_count=10),
                  _instant, clock=clock.now, sleep=FakeSleeper(clock)).records
    per_second = {}
    for record in records:
        key = int(record.dispatched_at)
        per_second[key] = per_second.get(key, 0) + 1
    assert max(per_second.values()) <= 11, per_second


def test_ramp_dispatches_fewer_requests_early_than_late():
    clock = FakeClock()
    records = run(_plan(warmup_seconds=4.0, hold_seconds=4.0, ramp_step_count=8),
                  _instant, clock=clock.now, sleep=FakeSleeper(clock)).records
    first = sum(1 for r in records if r.dispatched_at < 1.0)
    last = sum(1 for r in records if r.dispatched_at >= 7.0)
    assert first < last


def test_run_plan_rejects_invalid_fields():
    for bad in (
        dict(mode="sideways"), dict(rate=0.0), dict(concurrency=0),
        dict(warmup_seconds=-1.0), dict(hold_seconds=0.0),
        dict(grace_seconds=-1.0), dict(max_queue_seconds=0.0),
    ):
        with pytest.raises(ValueError):
            _plan(**bad)


# ---------------------------------------------------------------------------
# Termination at rates whose reciprocal is not binary-exact
# ---------------------------------------------------------------------------


class BoundedSleeper(FakeSleeper):
    """Fails loudly instead of spinning."""

    def __init__(self, clock: FakeClock, max_calls: int = 5000):
        super().__init__(clock)
        self.max_calls = max_calls

    def __call__(self, seconds: float) -> None:
        if len(self.calls) >= self.max_calls:
            raise AssertionError("pacing is spinning, not pacing")
        if 0 < seconds < 1e-9:
            raise AssertionError(f"degenerate sleep of {seconds!r}s")
        super().__call__(seconds)


@pytest.mark.parametrize("rate", [5.0, 6.0, 10.0, 7.0, 3.0])
def test_open_loop_terminates_for_rates_whose_reciprocal_is_not_binary_exact(rate):
    clock = FakeClock()
    records = run(_plan(rate=rate, hold_seconds=2.0), _instant, clock=clock.now,
                  sleep=BoundedSleeper(clock)).records
    assert len(records) == round(rate * 2.0)


def test_ramp_through_non_binary_exact_rates_terminates():
    clock = FakeClock()
    records = run(_plan(warmup_seconds=5.0, hold_seconds=1.0, ramp_step_count=5),
                  _instant, clock=clock.now, sleep=BoundedSleeper(clock)).records
    assert len(records) == 40  # 2+4+6+8+10 across the ramp, plus 10 in the hold


def test_closed_loop_ramp_through_non_binary_exact_rates_terminates():
    clock = FakeClock()
    result = run(_plan(mode="closed", concurrency=2, warmup_seconds=5.0, hold_seconds=1.0,
                       ramp_step_count=5),
                 _instant, clock=clock.now, sleep=BoundedSleeper(clock))
    assert result.records
    assert clock.time == pytest.approx(6.0, abs=0.05)


# ---------------------------------------------------------------------------
# The rate gate (decision 0001): the schedule paces, the gate checks it.
# ---------------------------------------------------------------------------


def test_the_gate_never_waits_when_the_schedule_holds():
    clock = FakeClock()
    records = run(_plan(rate=5.0, hold_seconds=3.0), _instant, clock=clock.now,
                  sleep=FakeSleeper(clock)).records
    assert [r.waited for r in records] == [0.0] * len(records)
    assert [r.dispatched_at for r in records] == pytest.approx(
        [i * 0.2 for i in range(len(records))], abs=1e-9
    )


def test_the_gate_blocks_when_tokens_are_taken_faster_than_they_are_earned():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=5.0, capacity=runner_module._GATE_CAPACITY,
                         clock=clock.now, sleep=FakeSleeper(clock))
    headroom = int(runner_module._GATE_CAPACITY)
    waits = [_pace_and_gate(bucket) for _ in range(12)]
    assert waits[:headroom] == [0.0] * headroom
    assert sum(waits) == pytest.approx((12 - headroom) / 5.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Concurrency, observed in real time (a fake sleep cannot see overlap).
# ---------------------------------------------------------------------------


class _InFlightProbe:
    """Counts how many calls are executing at once. Sleeps in real time."""

    def __init__(self, service_seconds: float) -> None:
        self.service_seconds = service_seconds
        self.peak = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, index: int):
        with self._lock:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
        try:
            time.sleep(self.service_seconds)
        finally:
            with self._lock:
                self._in_flight -= 1
        return index


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_one_worker_is_one_request_at_a_time(mode):
    clock = FakeClock()
    probe = _InFlightProbe(0.02)
    result = run(_plan(mode=mode, rate=200.0, hold_seconds=0.05), probe,
                 clock=clock.now, sleep=FakeSleeper(clock), allow_high_rate=True)
    assert result.records
    assert probe.peak == 1


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_more_workers_really_means_more_requests_in_flight(mode):
    clock = FakeClock()
    probe = _InFlightProbe(0.02)
    result = run(_plan(mode=mode, rate=400.0, concurrency=4, hold_seconds=0.1), probe,
                 clock=clock.now, sleep=FakeSleeper(clock), allow_high_rate=True)
    assert result.records
    assert 2 <= probe.peak <= 4


def test_every_record_of_a_pooled_run_is_filled_and_in_index_order():
    clock = FakeClock()
    executor = PoolExecutor(8)
    try:
        for _ in range(30):
            result = run(_plan(rate=50.0, concurrency=8), _instant, clock=clock.now,
                         sleep=FakeSleeper(clock), executor=executor)
            assert all(r.outcome == "completed" and r.result == r.index for r in result.records)
            assert [r.index for r in result.records] == list(range(len(result.records)))
    finally:
        executor.shutdown()


# ---------------------------------------------------------------------------
# Open loop has no per-stage drain: arrivals follow the schedule across the
# ramp even when the target is slow.
# ---------------------------------------------------------------------------


def test_open_loop_arrivals_follow_the_schedule_across_a_ramp_against_a_slow_target():
    plan = _plan(rate=50.0, concurrency=8, warmup_seconds=0.2, hold_seconds=0.2,
                 ramp_step_count=4)
    started = time.monotonic()
    result = run(plan, _InFlightProbe(0.1), allow_high_rate=True)
    wall = time.monotonic() - started

    expected = [a.offset for a in arrival_schedule(plan.stages)]
    scheduled = [r.scheduled_at - result.started_at for r in result.records]
    assert scheduled == pytest.approx(expected, abs=1e-9)
    lags = [r.queued for r in result.records]
    assert max(lags) < 0.05, f"arrivals slipped behind the schedule: {max(lags):.3f}s"
    # 0.4s of schedule plus one 0.1s service time; a drain per ramp step
    # would add a service time per step.
    assert wall < 0.4 + 0.1 + 0.2, f"the run took {wall:.2f}s"


# ---------------------------------------------------------------------------
# Open and closed are different models. A 200ms send function at 20/s.
# ---------------------------------------------------------------------------


def _slow(seconds):
    def send(index):
        time.sleep(seconds)
        return index

    return send


def _both_modes(concurrency, hold=0.4):
    """Run open and closed at once, in two threads, against the same 200ms
    send function, so the comparison costs one run of wall time."""
    out = {}

    def go(mode):
        plan = _plan(mode=mode, rate=20.0, concurrency=concurrency, hold_seconds=hold)
        out[mode] = run(plan, _slow(0.2))

    threads = [threading.Thread(target=go, args=(m,)) for m in ("open", "closed")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out["open"], out["closed"]


def _rel(result, attr):
    return [getattr(r, attr) - result.started_at for r in result.records
            if getattr(r, attr) is not None]


def test_open_and_closed_loop_produce_different_dispatch_timelines():
    """Two workers, 200ms per request: the most the target can be offered
    without queueing is 2 / 0.2 = 10/s, against 20/s asked.

    Open loop keeps its arrivals on the 50ms schedule and the excess queues, so
    offered latency grows request by request. Closed loop dispatches only when
    a worker is free, so it sends about half as many and nothing queues."""
    opened, closed = _both_modes(concurrency=2)

    # Open: every arrival on the schedule, all of them sent, queue growing.
    assert _rel(opened, "scheduled_at") == pytest.approx([i * 0.05 for i in range(8)], abs=1e-6)
    assert all(r.outcome == "completed" for r in opened.records)
    open_offered = [(r.completed_at - r.scheduled_at) for r in opened.records]
    assert open_offered[-1] > open_offered[0] + 0.15, open_offered

    # Closed: never more dispatches than N / latency allows, and the k-th
    # dispatch waits for the (k - N)-th completion.
    assert len(closed.records) <= 2 / 0.2 * 0.4 + 2
    dispatched = sorted(_rel(closed, "dispatched_at"))
    completed = sorted(_rel(closed, "completed_at"))
    for k in range(2, len(dispatched)):
        assert dispatched[k] >= completed[k - 2] - 0.005, (k, dispatched, completed)
    assert max(r.queued for r in closed.records) < 0.01
    closed_offered = [(r.completed_at - r.scheduled_at) for r in closed.records]
    assert max(closed_offered) < 0.3

    assert len(opened.records) > len(closed.records)
    assert max(open_offered) > max(closed_offered) + 0.1


def test_with_enough_workers_both_modes_hold_the_rate():
    """Four workers at 200ms can carry 20/s, so min(rate, N / latency) is the
    rate itself and the two models agree, which is the other half of the
    formula."""
    opened, closed = _both_modes(concurrency=4)
    assert len(opened.records) == 8
    assert max(r.queued for r in opened.records) < 0.03
    assert len(closed.records) >= 6


# ---------------------------------------------------------------------------
# The run deadline and the bounded backlog, under an injected clock.
# ---------------------------------------------------------------------------


def test_a_slow_inline_target_cannot_stretch_the_run_past_the_deadline():
    """Serial dispatch, every request takes 1s, 10/s asked for 2s with 1s of
    grace: the run stops dispatching at 3s and every remaining arrival is
    accounted for as never sent."""
    clock = FakeClock()

    def slow(index):
        clock.advance(1.0)
        return index

    result = run(_plan(hold_seconds=2.0, grace_seconds=1.0, max_queue_seconds=100.0),
                 slow, clock=clock.now, sleep=FakeSleeper(clock))
    assert result.deadline == pytest.approx(3.0)
    assert len(result.records) == 20
    assert result.count("completed") == 3
    assert result.count("not_dispatched") == 17
    assert all(r.dispatched_at < 3.0 for r in result.records if r.dispatched_at is not None)


def test_a_request_that_waited_too_long_is_dropped_not_sent():
    clock = FakeClock()
    sent = []

    def slow(index):
        sent.append(index)
        clock.advance(1.0)
        return index

    result = run(_plan(hold_seconds=2.0, max_queue_seconds=0.5), slow,
                 clock=clock.now, sleep=FakeSleeper(clock))
    dropped = [r for r in result.records if r.outcome == "dropped_backlog"]
    assert dropped, "a 1s target at 10/s with one worker must build a backlog"
    assert all(r.dispatched_at is None for r in dropped)
    assert sorted(set(sent)) == sorted(r.index for r in result.records if r.outcome == "completed")
    assert len(result.records) == 20


def test_requests_still_in_flight_at_the_deadline_are_abandoned():
    gate = threading.Event()

    def stuck(index):
        gate.wait(5)
        return index

    clock = FakeClock()
    executor = PoolExecutor(2)
    try:
        started = time.monotonic()
        result = run(_plan(rate=10.0, concurrency=2, hold_seconds=0.5, grace_seconds=0.05),
                     stuck, clock=clock.now, sleep=FakeSleeper(clock), executor=executor)
        wall = time.monotonic() - started
    finally:
        gate.set()
        executor.shutdown()
    assert wall < 1.0
    assert result.count("abandoned") == 2
    assert result.count("not_dispatched") == 3
    assert result.count("completed") == 0
    abandoned = [r for r in result.records if r.outcome == "abandoned"]
    assert all(r.dispatched_at is not None and r.result is None for r in abandoned)


def test_closed_loop_requests_in_flight_at_the_deadline_are_abandoned():
    gate = threading.Event()

    def stuck(index):
        gate.wait(5)
        return index

    started = time.monotonic()
    try:
        result = run(_plan(mode="closed", rate=20.0, concurrency=3, hold_seconds=0.2,
                           grace_seconds=0.1), stuck)
    finally:
        gate.set()
    assert time.monotonic() - started < 0.2 + 0.1 + 0.3
    assert result.count("abandoned") == 3
    assert len(result.records) == 3, "three workers, each stuck on its first request"


# ---------------------------------------------------------------------------
# Closed loop paces on the shared bucket.
# ---------------------------------------------------------------------------


def test_closed_loop_respects_the_rate_even_with_high_concurrency():
    clock = FakeClock()
    result = run(_plan(mode="closed", rate=5.0, concurrency=10, hold_seconds=2.0),
                 _instant, clock=clock.now, sleep=FakeSleeper(clock))
    assert len(result.records) == 10  # 5/s for 2s, not concurrency-unbounded
    times = [r.dispatched_at for r in result.records]
    assert times == pytest.approx([i * 0.2 for i in range(10)], abs=1e-9)


def test_closed_loop_ramp_occupies_its_full_duration():
    clock = FakeClock()
    run(_plan(mode="closed", concurrency=2, warmup_seconds=3.0, hold_seconds=3.0,
              ramp_step_count=10),
        _instant, clock=clock.now, sleep=FakeSleeper(clock))
    assert clock.time == pytest.approx(6.0, abs=0.05)


# ---------------------------------------------------------------------------
# Abort and failure
# ---------------------------------------------------------------------------


class _InterruptingExecutor(SerialExecutor):
    """Inline execution that raises KeyboardInterrupt on submission `after`,
    the way Ctrl-C lands in the dispatch loop."""

    def __init__(self, after: int):
        self.after = after
        self.submitted = 0

    def submit(self, fn):
        if self.submitted == self.after:
            raise KeyboardInterrupt
        self.submitted += 1
        return super().submit(fn)


def test_ctrl_c_returns_the_records_collected_so_far():
    clock = FakeClock()
    result = run(_plan(hold_seconds=3.0), _instant, clock=clock.now,
                 sleep=FakeSleeper(clock), executor=_InterruptingExecutor(after=4))
    assert result.aborted is True
    assert [r.outcome for r in result.records] == ["completed"] * 4 + ["not_dispatched"]
    assert [r.result for r in result.records[:4]] == [0, 1, 2, 3]


def test_ctrl_c_in_closed_loop_returns_what_it_has():
    clock = FakeClock()
    calls = []

    def send(index):
        calls.append(index)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return index

    result = run(_plan(mode="closed", hold_seconds=3.0), send, clock=clock.now,
                 sleep=FakeSleeper(clock))
    assert result.aborted is True
    assert result.count("completed") == 2
    assert result.count("abandoned") == 1


def test_ctrl_c_raised_on_a_worker_thread_is_an_abort_not_a_crash():
    calls = []
    lock = threading.Lock()

    def send(index):
        with lock:
            calls.append(index)
            if len(calls) == 3:
                raise KeyboardInterrupt
        return index

    result = run(_plan(mode="closed", concurrency=2, rate=50.0, hold_seconds=0.5), send)
    assert result.aborted is True
    assert result.count("completed") >= 2


def test_an_error_in_send_fn_is_raised_after_the_run_is_closed():
    def broken(index):
        if index == 2:
            raise ValueError("hook fell over")
        return index

    clock = FakeClock()
    with pytest.raises(ValueError, match="hook fell over"):
        run(_plan(hold_seconds=1.0), broken, clock=clock.now, sleep=FakeSleeper(clock))
