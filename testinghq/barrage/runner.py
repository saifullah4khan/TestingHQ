"""Barrage's rate and concurrency engine.

Barrage is a load generator for an endpoint the operator controls, not a
flooding tool. Everything in this module exists to keep firing at a
controlled, bounded rate: a warmup ramp eases into load instead of
slamming an endpoint cold, a steady-state hold sustains the configured
target, and a hard rate-and-duration ceiling refuses to run an
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
from typing import Callable, List, Tuple

from ..core.ratelimit import TokenBucket

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


class RateCeilingError(RuntimeError):
    """Raised when a run would exceed the hard rate or duration ceiling and
    the caller did not explicitly opt in to a higher limit."""


def check_rate_ceiling(
    rate: float,
    duration_seconds: float,
    allow_high_rate: bool = False,
    max_rate: float = DEFAULT_MAX_RATE_PER_SEC,
    max_duration: float = DEFAULT_MAX_DURATION_SEC,
) -> None:
    """Refuse a run whose target rate or total duration exceeds the ceiling,
    unless `allow_high_rate` is True. Checked once, up front, before any
    stage is built or any request is dispatched."""
    if allow_high_rate:
        return
    if rate > max_rate:
        raise RateCeilingError(
            f"refusing to run: rate {rate!r} req/s exceeds the safety ceiling "
            f"of {max_rate!r} req/s. Pass allow_high_rate=True (CLI: "
            "--allow-high-rate) to run above this ceiling explicitly."
        )
    if duration_seconds > max_duration:
        raise RateCeilingError(
            f"refusing to run: duration {duration_seconds!r}s exceeds the "
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


def _stage_dispatch_count(stage: RampStage) -> int:
    """How many requests a stage should dispatch: rate * duration, rounded
    to the nearest whole request.

    Deliberately NOT computed by looping `while clock() - stage_start <
    stage.duration`: driving that comparison off an injected fake clock
    that advances by whatever fractional amount `TokenBucket.acquire()`
    computes can, under floating-point rounding, get stuck asymptotically
    approaching the boundary in ever-smaller increments that never quite
    close the gap (a real wall clock always ticks forward regardless; a
    clock advanced only by adding computed floats does not have that
    guarantee). That failure mode was caught directly by this module's own
    hermetic tests: a fake clock plus a tight time-boundary loop produced
    runs of vanishingly small sleeps that never terminated. Computing the
    stage's dispatch count up front makes every loop below bounded by a
    plain integer comparison, so it always terminates in exactly that many
    iterations no matter what the injected clock or sleep do.
    """
    return max(0, round(stage.rate * stage.duration))


def _run_open_loop_stages(
    stages: List[RampStage],
    send_fn: SendFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> List[DispatchRecord]:
    """Serial open-loop dispatch: for each stage, pace strictly by a
    TokenBucket at that stage's rate and dispatch on schedule. Never waits
    on `service_seconds`; arrivals happen on the bucket's schedule
    regardless of how long a prior call took. Bucket capacity is 1: no
    burst allowance, so the arrival rate is exactly the stage's rate, not a
    rate-plus-an-initial-burst.
    """
    records: List[DispatchRecord] = []
    index = 0
    for stage in stages:
        count = _stage_dispatch_count(stage)
        if count <= 0:
            continue
        bucket = TokenBucket(rate_per_sec=stage.rate, capacity=1.0, clock=clock, sleep=sleep)
        for _ in range(count):
            waited = bucket.acquire()
            dispatch_time = clock()
            result, _service_seconds = send_fn(index)
            records.append(
                DispatchRecord(
                    index=index,
                    target_rate=stage.rate,
                    dispatch_time=dispatch_time,
                    waited=waited,
                    result=result,
                )
            )
            index += 1
    return records


def _run_closed_loop_stages(
    stages: List[RampStage],
    concurrency: int,
    send_fn: SendFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> List[DispatchRecord]:
    """Serial simulation of `concurrency` fixed workers, single-threaded so
    it stays deterministic and hermetically testable. Each of `concurrency`
    worker slots tracks the time it next becomes free
    (dispatch_time + service_seconds); a new request is only dispatched
    once a slot is free, AND a token is available in that stage's shared
    TokenBucket (capacity 1, no burst). The bucket is the safety ceiling:
    it caps aggregate throughput across all workers at the stage's rate no
    matter how much concurrency is configured or how fast the target
    responds.

    Bounded by `_stage_dispatch_count(stage)` (the ceiling's own budget for
    the stage), same as the open-loop path and for the same reason: a plain
    integer bound that always terminates, regardless of what the injected
    clock or sleep do at a stage boundary. A slow target simply may not use
    its whole budget before `stage.duration` elapses, which is correct
    closed-loop behaviour (offered load self-limits); the elapsed-time
    check below stops a stage as soon as its nominal duration is used, even
    if dispatch budget remains.
    """
    records: List[DispatchRecord] = []
    index = 0
    for stage in stages:
        budget = _stage_dispatch_count(stage)
        if budget <= 0 or stage.duration <= 0:
            continue
        bucket = TokenBucket(rate_per_sec=stage.rate, capacity=1.0, clock=clock, sleep=sleep)
        stage_start = clock()
        free_at = [stage_start] * concurrency
        dispatched = 0
        while dispatched < budget:
            slot = free_at.index(min(free_at))
            now = clock()
            if free_at[slot] > now:
                sleep(free_at[slot] - now)
            waited = bucket.acquire()
            dispatch_time = clock()
            if dispatch_time - stage_start >= stage.duration:
                break
            result, service_seconds = send_fn(index)
            free_at[slot] = dispatch_time + max(service_seconds, 0.0)
            records.append(
                DispatchRecord(
                    index=index,
                    target_rate=stage.rate,
                    dispatch_time=dispatch_time,
                    waited=waited,
                    result=result,
                )
            )
            index += 1
            dispatched += 1
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
) -> List[DispatchRecord]:
    """Run `plan`, dispatching through `send_fn`, and return the ordered
    list of DispatchRecord.

    Enforces the hard rate-and-duration ceiling before building any stage
    or dispatching anything: a run that would exceed it raises
    RateCeilingError unless `allow_high_rate=True`.

    `clock` and `sleep` are threaded through to every TokenBucket this run
    creates; passing fakes makes the whole run hermetic; no call in this
    module ever touches the real wall clock or a real sleep except via
    these defaults.
    """
    check_rate_ceiling(
        plan.rate,
        plan.duration_seconds,
        allow_high_rate=allow_high_rate,
        max_rate=max_rate,
        max_duration=max_duration,
    )
    stages = build_stages(plan.rate, plan.warmup_seconds, plan.hold_seconds, plan.ramp_step_count)
    if plan.mode == MODE_OPEN:
        return _run_open_loop_stages(stages, send_fn, clock, sleep)
    return _run_closed_loop_stages(stages, plan.concurrency, send_fn, clock, sleep)
