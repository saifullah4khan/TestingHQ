"""Barrage's rate and concurrency engine.

Barrage is a load generator for an endpoint the operator controls, not a
flooding tool. A warmup ramp eases into load, a steady-state hold sustains the
configured rate, and a hard rate, duration and concurrency ceiling refuses an
unreasonably large run unless the caller passes `allow_high_rate=True`. The
ceiling is a safety control, not a tuning knob.

Two firing modes, with standard load-testing meanings:

- Open loop (`mode="open"`): arrivals follow a fixed schedule computed from the
  plan before the run starts, across the ramp and the hold, whatever the target
  does. `concurrency` workers carry them; a request that cannot get a worker
  waits in a queue, and if it waits longer than `max_queue_seconds` it is
  recorded as `dropped_backlog` and never sent.
- Closed loop (`mode="closed"`): `concurrency` workers, each of which sends,
  waits for the response, then takes the next token from one shared
  TokenBucket paced at the stage rate. Offered load is therefore
  min(rate, concurrency / latency): a slow target slows the test down.

Both modes end at a hard deadline of `duration + grace_seconds` after the
start. A request still in flight then is recorded as `abandoned`, one still
queued as `not_dispatched`. Ctrl-C ends the run early with the same
accounting and `RunResult.aborted` set.

The clock and sleep are injectable throughout, so tests drive a run through
simulated time. Waiting for in-flight work at the deadline uses real time,
bounded by `deadline - clock()`.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_EXCEPTION, wait
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..core.ratelimit import TokenBucket
from .executor import make_executor

# ---------------------------------------------------------------------------
# The hard ceiling. Raising any of these requires allow_high_rate=True.
# ---------------------------------------------------------------------------

DEFAULT_MAX_RATE_PER_SEC = 50.0
DEFAULT_MAX_DURATION_SEC = 300.0
#: Requests in flight at once. Each is a worker thread, so this is a memory
#: ceiling as much as a load one.
DEFAULT_MAX_CONCURRENCY = 64

#: Seconds after `duration` that in-flight requests may still finish.
DEFAULT_GRACE_SECONDS = 10.0
#: Open loop only: a request that waited this long for a worker is not sent.
DEFAULT_MAX_QUEUE_SECONDS = 5.0

#: Outcomes a DispatchRecord can carry.
OUTCOME_COMPLETED = "completed"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_DROPPED_BACKLOG = "dropped_backlog"
OUTCOME_NOT_DISPATCHED = "not_dispatched"
OUTCOMES = (
    OUTCOME_COMPLETED,
    OUTCOME_ABANDONED,
    OUTCOME_DROPPED_BACKLOG,
    OUTCOME_NOT_DISPATCHED,
)


class RateCeilingError(RuntimeError):
    """A run would exceed the rate, duration or concurrency ceiling without the
    explicit opt-in, or asks for a concurrency below 1."""


def check_rate_ceiling(
    rate: float,
    duration_seconds: float,
    allow_high_rate: bool = False,
    max_rate: float = DEFAULT_MAX_RATE_PER_SEC,
    max_duration: float = DEFAULT_MAX_DURATION_SEC,
    concurrency: Optional[int] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> None:
    """Refuse a run over the ceiling unless `allow_high_rate` is True. A
    concurrency below 1 is refused regardless."""
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
    """One constant-rate segment of a run."""

    rate: float
    duration: float


def ramp_stages(target_rate: float, warmup_seconds: float, steps: int = 10) -> List[RampStage]:
    """A staircase from 0 to `target_rate` over `warmup_seconds` in `steps`
    equal stages. Empty when there is no warmup or no steps."""
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
    """The warmup ramp followed by one hold stage at `target_rate`."""
    stages = ramp_stages(target_rate, warmup_seconds, ramp_step_count)
    if hold_seconds > 0:
        stages.append(RampStage(rate=target_rate, duration=hold_seconds))
    return stages


def total_duration(warmup_seconds: float, hold_seconds: float) -> float:
    return warmup_seconds + hold_seconds


@dataclass(frozen=True)
class Arrival:
    """One scheduled open-loop request: its index, its offset from the start
    of the run, and the stage rate it belongs to."""

    index: int
    offset: float
    rate: float


def arrival_schedule(stages: List[RampStage]) -> List[Arrival]:
    """The open-loop schedule for `stages`, fixed before the run starts.

    Each stage contributes round(rate * duration) arrivals spaced 1 / rate
    apart from the stage's nominal start, and each stage starts where the
    previous one nominally ended. Nothing here depends on how the target
    responds, which is what makes it open loop.
    """
    arrivals: List[Arrival] = []
    stage_start = 0.0
    for stage in stages:
        if stage.duration <= 0:
            continue
        count = max(0, round(stage.rate * stage.duration))
        interval = 1.0 / stage.rate
        for step in range(count):
            arrivals.append(Arrival(len(arrivals), stage_start + step * interval, stage.rate))
        stage_start += stage.duration
    return arrivals


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

#: The caller's hook: send request `index`, return its result.
SendFn = Callable[[int], object]


@dataclass(frozen=True)
class DispatchRecord:
    """One request the run scheduled.

    Times are readings of the injected clock. `scheduled_at` is when the
    request was due (open loop: its slot on the schedule; closed loop: when a
    worker took its token). `dispatched_at` is when it actually went out and is
    None for a request that was never sent. `completed_at` is None unless the
    request finished before the deadline. `waited` is the rate gate's wait.
    """

    index: int
    target_rate: float
    scheduled_at: float
    outcome: str
    dispatched_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: object = None
    waited: float = 0.0

    @property
    def queued(self) -> float:
        """Seconds between due and dispatched, 0.0 if never dispatched."""
        if self.dispatched_at is None:
            return 0.0
        return max(self.dispatched_at - self.scheduled_at, 0.0)


@dataclass(frozen=True)
class RunResult:
    """What `run` returns: records in index order plus how the run ended."""

    records: List[DispatchRecord]
    started_at: float
    deadline: float
    ended_at: float
    aborted: bool = False

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.records if r.outcome == outcome)


class _Slot:
    """Mutable state for one request while the run is live. Guarded by the
    run's lock; turned into a frozen DispatchRecord at finalisation."""

    __slots__ = ("index", "target_rate", "scheduled_at", "waited", "dispatched_at",
                 "completed_at", "result", "outcome")

    def __init__(self, index: int, target_rate: float, scheduled_at: float, waited: float = 0.0):
        self.index = index
        self.target_rate = target_rate
        self.scheduled_at = scheduled_at
        self.waited = waited
        self.dispatched_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.result: object = None
        self.outcome: Optional[str] = None

    def freeze(self) -> DispatchRecord:
        outcome = self.outcome
        if outcome is None:
            outcome = OUTCOME_ABANDONED if self.dispatched_at is not None else OUTCOME_NOT_DISPATCHED
        return DispatchRecord(
            index=self.index,
            target_rate=self.target_rate,
            scheduled_at=self.scheduled_at,
            outcome=outcome,
            dispatched_at=self.dispatched_at,
            completed_at=self.completed_at if outcome == OUTCOME_COMPLETED else None,
            result=self.result if outcome == OUTCOME_COMPLETED else None,
            waited=self.waited,
        )


class _Run:
    """Shared state of one live run: the slots, the lock, and the flag that
    tells late workers the run is over."""

    def __init__(self, plan: "RunPlan", send_fn: SendFn, clock, started_at: float):
        self.plan = plan
        self.send_fn = send_fn
        self.clock = clock
        self.started_at = started_at
        self.end = started_at + plan.duration_seconds
        self.deadline = self.end + plan.grace_seconds
        self.lock = threading.Lock()
        self.closed = False
        self.slots: List[_Slot] = []

    def send(self, slot: _Slot, *, check_backlog: bool) -> None:
        """Send one request if the run still wants it, recording the outcome."""
        with self.lock:
            if self.closed:
                return
            now = self.clock()
            if now >= self.deadline:
                slot.outcome = OUTCOME_NOT_DISPATCHED
                return
            if check_backlog and now - slot.scheduled_at > self.plan.max_queue_seconds:
                slot.outcome = OUTCOME_DROPPED_BACKLOG
                return
            slot.dispatched_at = now
        result = self.send_fn(slot.index)
        finished = self.clock()
        with self.lock:
            if not self.closed:
                slot.result = result
                slot.completed_at = finished
                slot.outcome = OUTCOME_COMPLETED

    def finalise(self) -> List[DispatchRecord]:
        with self.lock:
            self.closed = True
            records = [slot.freeze() for slot in self.slots]
        records.sort(key=lambda r: r.index)
        return records


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

#: Headroom for the open-loop rate gate. The schedule is the pacer; the gate is
#: an independent check on the schedule's interval arithmetic, and in a
#: correctly paced run it never waits. See docs/decisions/0001-barrage-pacing.md.
_GATE_CAPACITY = 2.0

#: Float residue tolerated when comparing a clock reading with a boundary. An
#: additive injected clock lands a hair short of a boundary it was told to
#: reach, and treating that as "before" would ask for a sub-nanosecond sleep.
_EPSILON = 1e-9


def _sleep_until(deadline: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    now = clock()
    if deadline > now:
        sleep(deadline - now)


def _pace_and_gate(bucket: TokenBucket) -> float:
    """Take one token from the open-loop gate and return seconds waited. Zero
    on a correctly paced run; it blocks only if the schedule hands out arrivals
    faster than the stage rate."""
    return bucket.acquire()


class _ClosedLoopPacer:
    """The shared token source for closed-loop workers.

    One TokenBucket of capacity 1, so workers cannot burst, whose rate follows
    the stage the run is in. `take` blocks the calling worker until its token
    is earned, and returns None once the next token would land at or after the
    end of the run. The lock serialises token takers, so token order is index
    order.
    """

    def __init__(self, stages: List[RampStage], run: _Run, clock, sleep):
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._done = False
        self._next_index = 0
        self._bounds: List[Tuple[float, float]] = []  # (absolute stage end, rate)
        at = run.started_at
        for stage in stages:
            if stage.duration > 0:
                at += stage.duration
                self._bounds.append((at, stage.rate))
        self._bucket = TokenBucket(
            rate_per_sec=self._bounds[0][1], capacity=1.0, clock=clock, sleep=sleep
        )

    def stop(self) -> None:
        self._done = True

    def _stage_at(self, now: float) -> Tuple[float, float]:
        for stage_end, rate in self._bounds:
            if now < stage_end:
                return stage_end, rate
        return self._bounds[-1]

    def take(self) -> Optional[_Slot]:
        end = self._run.end - _EPSILON
        with self._lock:
            while True:
                now = self._clock()
                if self._done or self._run.closed or now >= end:
                    self._done = True
                    return None
                stage_end, rate = self._stage_at(now + _EPSILON)
                if self._bucket.rate != rate:
                    self._bucket.set_rate(rate)
                wait_for = self._bucket.wait_time()
                boundary = min(stage_end, self._run.end)
                if now + wait_for >= boundary - _EPSILON:
                    if boundary >= end:
                        self._done = True
                        return None
                    # The token falls in the next stage: move there and
                    # re-price it at that stage's rate.
                    self._sleep(boundary - now)
                    continue
                waited = self._bucket.acquire()
                taken_at = self._clock()
                if taken_at >= end:
                    self._done = True
                    return None
                slot = _Slot(self._next_index, rate, taken_at, waited)
                self._next_index += 1
                return slot


# ---------------------------------------------------------------------------
# The two modes
# ---------------------------------------------------------------------------


def _run_open_loop(run: _Run, stages: List[RampStage], executor, clock, sleep) -> List:
    futures = []
    gate: Optional[TokenBucket] = None
    schedule = arrival_schedule(stages)
    for position, arrival in enumerate(schedule):
        due = run.started_at + arrival.offset
        _sleep_until(due, clock, sleep)
        if clock() >= run.deadline:
            # Only reachable when an inline send overran the whole run. Every
            # remaining arrival was due and is accounted for as never sent.
            with run.lock:
                for late in schedule[position:]:
                    slot = _Slot(late.index, late.rate, run.started_at + late.offset)
                    slot.outcome = OUTCOME_NOT_DISPATCHED
                    run.slots.append(slot)
            break
        if gate is None or gate.rate != arrival.rate:
            gate = TokenBucket(
                rate_per_sec=arrival.rate, capacity=_GATE_CAPACITY, clock=clock, sleep=sleep
            )
        waited = _pace_and_gate(gate)
        slot = _Slot(arrival.index, arrival.rate, due, waited)
        with run.lock:
            run.slots.append(slot)
        futures.append(executor.submit(lambda s=slot: run.send(s, check_backlog=True)))
    _sleep_until(run.end, clock, sleep)
    return futures


def _run_closed_loop(run: _Run, executor, pacer: _ClosedLoopPacer) -> List:
    def worker() -> None:
        while True:
            slot = pacer.take()
            if slot is None:
                return
            with run.lock:
                if run.closed:
                    return
                run.slots.append(slot)
            run.send(slot, check_backlog=False)

    return [executor.submit(worker) for _ in range(run.plan.concurrency)]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _failed(future) -> bool:
    return future.done() and not future.cancelled() and future.exception() is not None


MODE_OPEN = "open"
MODE_CLOSED = "closed"
VALID_MODES = (MODE_OPEN, MODE_CLOSED)


@dataclass(frozen=True)
class RunPlan:
    """One Barrage run. `rate` is requests per second (open: the arrival rate;
    closed: the shared token rate). `concurrency` is the number of workers in
    both modes. `warmup_seconds` ramps from 0 to `rate`, then `hold_seconds`
    holds it. The run ends `grace_seconds` after `warmup + hold` at the latest.
    `max_queue_seconds` bounds how long an open-loop request may wait for a
    worker before it is dropped."""

    mode: str
    rate: float
    concurrency: int
    warmup_seconds: float
    hold_seconds: float
    ramp_step_count: int = 10
    grace_seconds: float = DEFAULT_GRACE_SECONDS
    max_queue_seconds: float = DEFAULT_MAX_QUEUE_SECONDS

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
        if self.grace_seconds < 0:
            raise ValueError(f"grace_seconds must be >= 0, got {self.grace_seconds!r}")
        if self.max_queue_seconds <= 0:
            raise ValueError(
                f"max_queue_seconds must be > 0, got {self.max_queue_seconds!r}"
            )

    @property
    def duration_seconds(self) -> float:
        return total_duration(self.warmup_seconds, self.hold_seconds)

    @property
    def stages(self) -> List[RampStage]:
        return build_stages(self.rate, self.warmup_seconds, self.hold_seconds, self.ramp_step_count)


def run(
    plan: RunPlan,
    send_fn: SendFn,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    allow_high_rate: bool = False,
    max_rate: float = DEFAULT_MAX_RATE_PER_SEC,
    max_duration: float = DEFAULT_MAX_DURATION_SEC,
    executor=None,
) -> RunResult:
    """Run `plan` through `send_fn` and return a RunResult.

    The ceiling is checked before anything is dispatched. `executor` defaults
    to one sized by `plan.concurrency`; an injected one is not shut down.

    A KeyboardInterrupt ends the run: it is not re-raised, the records gathered
    so far come back, and `RunResult.aborted` is True. An exception raised by
    `send_fn` is a bug in the hook and is re-raised after the run is closed.
    """
    check_rate_ceiling(
        plan.rate,
        plan.duration_seconds,
        allow_high_rate=allow_high_rate,
        max_rate=max_rate,
        max_duration=max_duration,
        concurrency=plan.concurrency,
    )
    stages = plan.stages
    owned = executor is None
    if owned:
        executor = make_executor(plan.concurrency)
    live = _Run(plan, send_fn, clock, clock())
    pacer = None
    futures: List = []
    aborted = False
    failure: Optional[BaseException] = None
    try:
        if plan.mode == MODE_OPEN:
            futures = _run_open_loop(live, stages, executor, clock, sleep)
        else:
            pacer = _ClosedLoopPacer(stages, live, clock, sleep)
            futures = _run_closed_loop(live, executor, pacer)
        remaining = live.deadline - clock()
        if futures and remaining > 0:
            wait(futures, timeout=remaining, return_when=FIRST_EXCEPTION)
        if plan.mode == MODE_CLOSED and not any(_failed(f) for f in futures):
            _sleep_until(live.end, clock, sleep)
    except KeyboardInterrupt:
        aborted = True
    finally:
        if pacer is not None:
            pacer.stop()
        records = live.finalise()
        if owned:
            executor.shutdown(cancel_pending=True)
    for future in futures:
        if not future.done() or future.cancelled():
            continue
        exc = future.exception()
        if isinstance(exc, KeyboardInterrupt):
            # Raised on a worker thread (an injected hook can do this); it
            # still means "stop and keep what you have".
            aborted = True
        elif exc is not None and failure is None:
            failure = exc
    if failure is not None:
        raise failure
    return RunResult(
        records=records,
        started_at=live.started_at,
        deadline=live.deadline,
        ended_at=clock(),
        aborted=aborted,
    )
