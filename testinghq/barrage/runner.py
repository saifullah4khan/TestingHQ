"""Barrage's rate and concurrency engine.

Barrage is a load generator for an endpoint the operator controls, not a
flooding tool. Everything in this module exists to keep firing at a
controlled, bounded rate: a warmup ramp eases into load instead of
slamming an endpoint cold, a steady-state hold sustains the configured
target, and a hard rate, duration and concurrency ceiling refuses to run an
unreasonably large load test unless the caller explicitly says so with
`allow_high_rate=True`. That ceiling is a safety control, not a tuning
knob: a typo in `--rate` or `--duration` must not turn into a
self-inflicted denial of service against the operator's own endpoint.
Fix the caller's inputs, never widen the default ceiling.

Pacing is done exclusively with testinghq.core.ratelimit.TokenBucket
(imported, never reimplemented). The clock and sleep function are always
injectable, all the way through this module's public API, so tests can
drive a full run through simulated time with zero real sleeps.

Two firing modes, matching standard load-testing terminology:

- Closed-loop (`mode="closed"`): a fixed number of workers (`concurrency`).
  Each worker only issues its next request after its previous one
  completes, so offered load self-limits when the target slows down.
  Still capped by the same rate ceiling, shared across all workers.
- Open-loop (`mode="open"`): a fixed arrival rate. Requests are dispatched
  on schedule regardless of how long previous requests take to complete,
  which is what actually reveals a target's breaking point (closed-loop
  load quietly throttles itself against a slow target; open-loop does
  not). `concurrency` bounds how many requests may be outstanding at
  once, as a resource safety valve, not as the thing controlling rate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..core.ratelimit import TokenBucket
from .executor import PoolExecutor, SerialExecutor, make_executor

# ---------------------------------------------------------------------------
# The hard ceiling.
#
# Defaults are deliberately modest: 50 requests/second and a 5 minute
# duration cap. That is enough to see real throughput and latency behaviour
# under sustained load against a local or staging target, but not enough to
# come close to actually denial-of-servicing anything by accident. Raising
# either limit requires the caller to pass allow_high_rate=True explicitly
# (wired to a CLI flag), so a fat-fingered --rate or --duration cannot
# silently turn a load test into an outage. This ceiling must never be
# raised by default and must never be bypassed implicitly.
# ---------------------------------------------------------------------------

DEFAULT_MAX_RATE_PER_SEC = 50.0
DEFAULT_MAX_DURATION_SEC = 300.0
#: Requests in flight at once. Each is a worker thread, so this is a memory
#: ceiling as much as a load one. 64 covers the default rate ceiling against a
#: target answering in over a second (50 req/s x 1.28s), which is already a
#: target in trouble. Raised by the same --allow-high-rate opt-in as the others.
DEFAULT_MAX_CONCURRENCY = 64


class RateCeilingError(RuntimeError):
    """Raised when a run would exceed the hard rate, duration or concurrency
    ceiling and the caller did not explicitly opt in to a higher limit, or asks
    for a concurrency below 1."""


def check_rate_ceiling(
    rate: float,
    duration_seconds: float,
    allow_high_rate: bool = False,
    max_rate: float = DEFAULT_MAX_RATE_PER_SEC,
    max_duration: float = DEFAULT_MAX_DURATION_SEC,
    concurrency: Optional[int] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> None:
    """Refuse a run whose target rate, total duration or concurrency exceeds
    the ceiling, unless `allow_high_rate` is True. Checked once, up front,
    before any stage is built or any request is dispatched.

    A concurrency below 1 is refused whatever `allow_high_rate` says: it is not
    a high setting but a meaningless one, and the pool would reject it later
    with a less useful message."""
    if concurrency is not None and concurrency < 1:
        raise RateCeilingError(
            f"concurrency {concurrency!r} must be at least 1: it is the number "
            "of requests in flight at once"
        )
    if allow_high_rate:
        return
    if concurrency is not None and concurrency > max_concurrency:
        raise RateCeilingError(
            f"concurrency {concurrency!r} exceeds the safety ceiling of "
            f"{max_concurrency!r} requests in flight. Each one is a worker "
            "thread. Pass allow_high_rate=True (CLI: --allow-high-rate) to run "
            "above this ceiling explicitly."
        )
    if rate > max_rate:
        raise RateCeilingError(
            f"rate {rate!r} req/s exceeds the safety ceiling "
            f"of {max_rate!r} req/s. Pass allow_high_rate=True (CLI: "
            "--allow-high-rate) to run above this ceiling explicitly."
        )
    if duration_seconds > max_duration:
        raise RateCeilingError(
            f"duration {duration_seconds!r}s exceeds the "
            f"safety ceiling of {max_duration!r}s. Pass allow_high_rate=True "
            "(CLI: --allow-high-rate) to run above this ceiling explicitly."
        )


# ---------------------------------------------------------------------------
# Ramp schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RampStage:
    """One constant-rate segment of a run. A full run is a list of stages:
    several rising stages that approximate a linear ramp, followed by one
    steady-state hold stage at the full target rate."""

    rate: float
    duration: float


def ramp_stages(target_rate: float, warmup_seconds: float, steps: int = 10) -> List[RampStage]:
    """Approximate a linear ramp from 0 to `target_rate` over
    `warmup_seconds`, discretized into `steps` constant-rate stages of equal
    duration. Each stage is paced by its own TokenBucket at a constant rate
    (a TokenBucket's rate is fixed for its lifetime), so a true continuous
    ramp is approximated by a staircase of `steps` increasing rates. More
    steps makes the staircase a closer approximation to a straight line.

    Returns an empty list if there is no warmup (warmup_seconds <= 0) or no
    steps requested.
    """
    if warmup_seconds <= 0 or steps <= 0:
        return []
    step_duration = warmup_seconds / steps
    return [
        RampStage(rate=target_rate * (i + 1) / steps, duration=step_duration)
        for i in range(steps)
    ]


def build_stages(
    target_rate: float, warmup_seconds: float, hold_seconds: float, ramp_step_count: int = 10
) -> List[RampStage]:
    """The full stage list for a run: the warmup ramp, then one steady-state
    hold stage at `target_rate` for `hold_seconds`."""
    stages = ramp_stages(target_rate, warmup_seconds, ramp_step_count)
    if hold_seconds > 0:
        stages.append(RampStage(rate=target_rate, duration=hold_seconds))
    return stages


def total_duration(warmup_seconds: float, hold_seconds: float) -> float:
    return warmup_seconds + hold_seconds


# ---------------------------------------------------------------------------
# Dispatch
#
# `send_fn(index) -> (result, service_seconds)` is the caller's hook for
# actually issuing a request (or, in tests, faking one). `service_seconds`
# is how long the call took (or, for a fake, however long the test wants to
# simulate); open-loop dispatch ignores it entirely (that is the point of
# open-loop: arrivals do not wait on service time), closed-loop dispatch
# uses it to know when a worker slot frees up.
# ---------------------------------------------------------------------------

SendFn = Callable[[int], Tuple[object, float]]

#: What `run` needs from a dispatch strategy: `submit`, `drain` and `shutdown`,
#: as defined by the two executors in `testinghq/barrage/executor.py`. A string,
#: because it is only ever used in annotations.
DispatchExecutor = "SerialExecutor | PoolExecutor"


@dataclass(frozen=True)
class DispatchRecord:
    """One dispatched request: which stage rate was targeted at dispatch
    time, when (simulated or real, per the injected clock) it was
    dispatched, how long the bucket made it wait, and the caller's result
    object for that request."""

    index: int
    target_rate: float
    dispatch_time: float
    waited: float
    result: object
    #: Seconds the request spent waiting for a free worker after it was
    #: scheduled. `dispatch_time` is when it actually went out, so a saturated
    #: pool shows up as achieved throughput falling behind the target, and this
    #: says by how much each request was held back. Always 0 for serial dispatch.
    queued: float = 0.0


# Headroom for the gate bucket. See _pace_and_gate: the schedule is the
# pacer, and this bucket is a check on the schedule's interval arithmetic, so
# it needs enough headroom that a correctly paced run never enters its own
# internal wait loop. 2.0 does that.
#
# The old justification for this value was a float knife-edge where the bucket
# would spin. That was the TokenBucket.acquire() defect fixed in 2026-07-27
# and it no longer applies, so 1.0 would now be safe too. 2.0 stays because it
# tolerates jitter for free, not because lowering it is dangerous.
# See docs/decisions/0001-barrage-pacing.md.
_GATE_CAPACITY = 2.0


def _stage_dispatch_count(stage: RampStage) -> int:
    """How many requests a stage is budgeted: rate * duration, rounded to
    the nearest whole request.

    Deliberately computed up front rather than by looping on
    `while clock() - stage_start < stage.duration`. Every dispatch loop
    below is bounded by this plain integer, so a stage always terminates in
    exactly that many iterations no matter what the injected clock or sleep
    do at the boundary. This is also the ceiling's per-stage budget: it is
    what makes aggregate throughput bounded by the configured rate even
    when concurrency is high and the target responds instantly.
    """
    return max(0, round(stage.rate * stage.duration))


def _sleep_until(deadline: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    """Sleep, via the injected sleep, until the clock reads `deadline`.
    A no-op if the deadline has already passed."""
    now = clock()
    if deadline > now:
        sleep(deadline - now)


def _hold_until_stage_end(
    stage: RampStage,
    stage_start: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Occupy the rest of the stage's nominal duration after its budget is
    spent.

    Without this, a stage ends the moment its last request is dispatched,
    which is up to one full interval early, and collapses entirely when
    rounding gives a stage a budget of 1 (a 0.3s stage at 2 req/s fires its
    single request at stage_start and returns immediately). The ramp then
    takes almost no wall time and dumps every warmup request at once: the
    achieved rate overshoots the target, and the warmup ramp does the exact
    opposite of its job, slamming a cold endpoint instead of easing into
    it. Caught by a real run against a local sink, where the first second
    of a 10 req/s run reported 15 req/s achieved.

    Holding to the stage boundary is what makes a stage's rate mean what it
    says, and is why the ramp actually occupies warmup_seconds.
    """
    _sleep_until(stage_start + stage.duration, clock, sleep)


def _pace_and_gate(bucket: TokenBucket) -> float:
    """Take one token from the rate-gate bucket and return seconds waited.

    The schedule is the pacer. The caller sleeps to an absolute deadline
    derived from the stage's rate, and this bucket is a check on that: the
    schedule computes an interval by dividing, the bucket is handed the rate
    itself, so a mistake in one is not automatically a mistake in the other,
    and if the dispatch loop ever takes tokens faster than the rate refills,
    this blocks and the run slows to the configured rate rather than exceeding
    it. In a correctly paced run this returns exactly 0.0 for every dispatch.

    That is a narrower claim than it looks, and worth stating plainly. Both
    mechanisms read the rate from `stage.rate`, so this does NOT catch a
    disagreement about the rate; it catches an error in the interval
    arithmetic. `tests/unit/barrage/test_runner.py` pins both halves: that the
    gate never waits on a correct run, and that it does block when tokens are
    taken too fast, which is what makes it a check rather than decoration.

    Why the gate is not the pacer, which is the actual decision here: a
    blocking gate would throttle the offered load down exactly when the target
    is slow, and measuring that is the whole point of an open-loop run. See
    docs/decisions/0001-barrage-pacing.md, which records that decision and the
    larger finding that came out of it: the dispatch here used to be serial, so
    neither the gate nor the schedule was what limited throughput, and the
    reported rate was a measurement of this loop. That is fixed, in
    testinghq/barrage/executor.py.

    Note the asymmetry if this is ever revisited: a closed-loop run has no
    arrival schedule to hold, so a blocking acquire would be the right tool
    there.
    """
    return bucket.acquire()


def _submit(
    executor: "DispatchExecutor",
    send_fn: SendFn,
    index: int,
    dispatch_time: float,
    target_rate: float,
    waited: float,
    records: List[DispatchRecord],
    clock: Callable[[], float],
) -> None:
    """Hand one request to the executor and record it.

    The record is appended in submission order, before the request has
    completed. `DispatchRecord.index` is the payload identity and the artifact's
    ordering key, and both have to be a function of the plan rather than of how
    the workers happened to interleave: a run artifact that reorders itself
    between executions of the same plan cannot be compared, and `compare`
    aligns on exactly that index.

    `dispatch_time` on the finished record is when the request actually went
    out, read from `clock` by the worker as it starts, not when it was handed to
    the executor. The first version recorded the hand-off. With every worker
    busy, requests wait in the pool's queue, and a hand-off timestamp reported
    them as sent on schedule: achieved throughput equalled the target and the
    knee never showed, while the target was in fact falling behind. The gap
    between the two is kept as `queued`.

    `result` is None until the request completes. Nothing reads a record's
    result before the run is drained, and a partially filled record is what a
    crash mid-run would leave behind, which is the honest state for one.
    """
    position = len(records)
    records.append(
        DispatchRecord(
            index=index,
            target_rate=target_rate,
            dispatch_time=dispatch_time,
            waited=waited,
            result=None,
        )
    )

    def _send():
        started = clock()
        # The future carries the RESULT and the start time only, not
        # `send_fn`'s service seconds, which are an artefact of the loop.
        return send_fn(index)[0], started

    future = executor.submit(_send)
    future.add_done_callback(_bind_result(records, position))


def _bind_result(records: List[DispatchRecord], position: int):
    """A callback that writes a completed request's result back into its record.

    By position, captured at submission, rather than by searching for the
    index. The first version scanned the list on every completion, which is
    quadratic in the run: at the default ceiling that is 15,000 requests and
    over a hundred million comparisons, on the worker threads that are meant
    to be sending.

    `DispatchRecord` is frozen, so this replaces the element rather than
    mutating it. Item assignment on a list is atomic under the GIL, and each
    position is written by exactly one callback.
    """

    def _write(future) -> None:
        # An exception inside send_fn is not raised here. It is re-raised by
        # executor.drain() against the future that carries it, so a failure
        # surfaces at the point the run waits for the request rather than
        # inside a worker thread where the traceback would be lost.
        if future.cancelled() or future.exception() is not None:
            return
        result, started = future.result()
        record = records[position]
        records[position] = DispatchRecord(
            index=record.index,
            target_rate=record.target_rate,
            dispatch_time=started,
            waited=record.waited,
            result=result,
            queued=max(started - record.dispatch_time, 0.0),
        )

    return _write


def _run_open_loop_stages(
    stages: List[RampStage],
    concurrency: int,
    send_fn: SendFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    executor: "DispatchExecutor",
) -> List[DispatchRecord]:
    """Open-loop dispatch: for each stage, dispatch on an absolute schedule of
    `1 / rate` second arrivals from the stage's start, gated by a TokenBucket
    at that stage's rate. Arrivals happen on schedule regardless of how long any
    prior request took, which is what actually reveals a target's breaking
    point.

    Requests are SUBMITTED on schedule, not completed on schedule. That
    distinction is the whole tool: the previous version called `send_fn`
    inline, so a target slower than the arrival interval pushed every
    subsequent arrival into the past and the achieved rate silently became
    `1 / response_time`. With `concurrency` in flight the arrivals stay on the
    schedule and the target sees the load that was asked for, which is the only
    way a p90 over a queue is a measurement of the target rather than of this
    loop.

    The loop records the hand-off, and the worker overwrites it with the time
    the request actually went out (see `_submit`), so a pool that cannot keep
    up shows as a shortfall rather than as a schedule met. A future returned
    here is deliberately not awaited: an open-loop run's arrivals must not wait
    on service, and draining happens at the stage boundary, below.
    """
    records: List[DispatchRecord] = []
    index = 0
    for stage in stages:
        if stage.duration <= 0:
            continue
        stage_start = clock()
        # A stage whose budget rounds to 0 (an early, low-rate ramp step
        # too short to earn a whole request) still occupies its window.
        # Skipping it outright would silently shorten the ramp.
        count = _stage_dispatch_count(stage)
        if count > 0:
            bucket = TokenBucket(
                rate_per_sec=stage.rate, capacity=_GATE_CAPACITY, clock=clock, sleep=sleep
            )
            interval = 1.0 / stage.rate
            for step in range(count):
                _sleep_until(stage_start + step * interval, clock, sleep)
                waited = _pace_and_gate(bucket)
                dispatch_time = clock()
                _submit(executor, send_fn, index, dispatch_time, stage.rate, waited, records, clock)
                index += 1
        # Drain before the stage's window closes, so a request submitted in
        # this stage cannot be reported in a later one and so a stage's error
        # is attributed to the stage that caused it.
        executor.drain()
        _hold_until_stage_end(stage, stage_start, clock, sleep)
    executor.drain()
    return records


def _run_closed_loop_stages(
    stages: List[RampStage],
    concurrency: int,
    send_fn: SendFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    executor: "DispatchExecutor",
) -> List[DispatchRecord]:
    """Closed-loop dispatch across `concurrency` workers.

    Offered load self-limits against a slow target, which is the closed-loop
    property: the pool has `concurrency` workers, so when they are all busy
    further submissions queue rather than going out. That is the backpressure
    arriving by construction, rather than the `free_at` arithmetic that used to
    simulate `concurrency` workers inside a single-threaded loop.

    Bounded by `_stage_dispatch_count(stage)`, the stage's rate budget, so
    the loop always terminates; a slow target simply may not use its whole
    budget before `stage.duration` elapses, which is correct.
    """
    records: List[DispatchRecord] = []
    index = 0
    for stage in stages:
        if stage.duration <= 0:
            continue
        stage_start = clock()
        # As in the open-loop path: a zero-budget ramp step still occupies
        # its window rather than being skipped, so the ramp keeps its shape.
        budget = _stage_dispatch_count(stage)
        bucket = TokenBucket(
            rate_per_sec=stage.rate, capacity=_GATE_CAPACITY, clock=clock, sleep=sleep
        )
        interval = 1.0 / stage.rate
        dispatched = 0
        while dispatched < budget:
            deadline = stage_start + dispatched * interval
            _sleep_until(deadline, clock, sleep)
            if clock() - stage_start >= stage.duration:
                break
            waited = _pace_and_gate(bucket)
            dispatch_time = clock()
            _submit(executor, send_fn, index, dispatch_time, stage.rate, waited, records, clock)
            index += 1
            dispatched += 1
        executor.drain()
        _hold_until_stage_end(stage, stage_start, clock, sleep)
    executor.drain()
    return records


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

MODE_OPEN = "open"
MODE_CLOSED = "closed"
VALID_MODES = (MODE_OPEN, MODE_CLOSED)


@dataclass(frozen=True)
class RunPlan:
    """The full description of one Barrage run. `mode` is "open" or
    "closed" (see module docstring). `rate` is the target requests/second
    (open-loop: the exact arrival rate; closed-loop: the aggregate ceiling
    workers are paced against). `concurrency` is the worker count
    (closed-loop) or the max outstanding requests (open-loop, enforced by
    the caller's executor, not by this module). `warmup_seconds` ramps from
    0 to `rate`; `hold_seconds` is the steady-state duration at `rate`.
    """

    mode: str
    rate: float
    concurrency: int
    warmup_seconds: float
    hold_seconds: float
    ramp_step_count: int = 10

    def __post_init__(self):
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if self.rate <= 0:
            raise ValueError(f"rate must be > 0, got {self.rate!r}")
        if self.concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {self.concurrency!r}")
        if self.warmup_seconds < 0:
            raise ValueError(f"warmup_seconds must be >= 0, got {self.warmup_seconds!r}")
        if self.hold_seconds <= 0:
            raise ValueError(f"hold_seconds must be > 0, got {self.hold_seconds!r}")
        if self.ramp_step_count < 0:
            raise ValueError(f"ramp_step_count must be >= 0, got {self.ramp_step_count!r}")

    @property
    def duration_seconds(self) -> float:
        return total_duration(self.warmup_seconds, self.hold_seconds)


def run(
    plan: RunPlan,
    send_fn: SendFn,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    allow_high_rate: bool = False,
    max_rate: float = DEFAULT_MAX_RATE_PER_SEC,
    max_duration: float = DEFAULT_MAX_DURATION_SEC,
    executor: Optional["DispatchExecutor"] = None,
) -> List[DispatchRecord]:
    """Run `plan`, dispatching through `send_fn`, and return the ordered
    list of DispatchRecord.

    Enforces the hard rate, duration and concurrency ceiling before building any stage
    or dispatching anything: a run that would exceed it raises
    RateCeilingError unless `allow_high_rate=True`.

    `clock` and `sleep` are threaded through to every TokenBucket this run
    creates; passing fakes makes the whole run hermetic; no call in this
    module ever touches the real wall clock or a real sleep except via
    these defaults.

    `executor` defaults to one sized by `plan.concurrency`: a thread pool of
    that many workers for anything above 1, and inline dispatch for exactly 1.
    The pool is what makes `concurrency` mean something, and injecting it is
    what lets a test prove requests overlapped without a socket.

    Records come back ordered by `index` and fully populated. A caller never
    sees a half-filled record from a completed run, because the executor is
    drained before `run` returns even when the caller supplied it.
    """
    check_rate_ceiling(
        plan.rate,
        plan.duration_seconds,
        allow_high_rate=allow_high_rate,
        max_rate=max_rate,
        max_duration=max_duration,
        concurrency=plan.concurrency,
    )
    stages = build_stages(plan.rate, plan.warmup_seconds, plan.hold_seconds, plan.ramp_step_count)
    owned = executor is None
    if executor is None:
        executor = make_executor(plan.concurrency)
    try:
        if plan.mode == MODE_OPEN:
            records = _run_open_loop_stages(
                stages, plan.concurrency, send_fn, clock, sleep, executor
            )
        else:
            records = _run_closed_loop_stages(
                stages, plan.concurrency, send_fn, clock, sleep, executor
            )
        executor.drain()
    except BaseException:
        # An error or a Ctrl-C ends the run. Cancel what is still queued rather
        # than draining it: a drain would keep sending to the target until the
        # queue was empty, and would re-raise a worker's exception over the one
        # that actually stopped the run.
        if owned:
            executor.shutdown(wait=False, cancel_pending=True)
        raise
    if owned:
        executor.shutdown(wait=True)
    return records
