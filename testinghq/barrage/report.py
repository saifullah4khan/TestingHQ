"""Load reporting for Barrage.

Barrage asks "did the pipeline hold up": throughput achieved versus targeted,
two latency distributions, the status mix, and the knee where the endpoint
starts shedding, falling behind, or slowing.

Two latencies, because one of them hides queueing:

- service: the request on the wire, from send to response. What the target
  spent on it.
- offered: from the request's scheduled arrival to its response. What a
  client arriving on that schedule would have experienced, including any
  time spent waiting for a free worker. In closed loop a request is scheduled
  when its worker takes the token, so the two are close by construction.

An error is a 5xx, a 429 (named "shed": the endpoint refusing load the
standard way), no response at all, or a request abandoned at the run deadline.
A 4xx other than 429 is the endpoint rejecting a request, not failing to hold.

Pure functions only: no I/O, no clock reads. Times enter as data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_BUCKET_SECONDS = 1.0

#: A bucket is shedding when this fraction of its sent requests errored.
KNEE_ERROR_RATE = 0.05
#: A bucket is slowing when its median service latency exceeds the hold
#: baseline by this factor.
KNEE_LATENCY_FACTOR = 2.0
#: A bucket is falling behind when it sent less than (1 - this) of what the
#: plan targeted for it.
KNEE_DIVERGENCE = 0.2
#: Buckets the plan expected fewer requests from than this are not judged on
#: divergence: one late request in a two-request bucket is not a knee.
KNEE_MIN_EXPECTED = 2.0
#: Consecutive bad buckets required before a knee is reported.
KNEE_PERSISTENCE = 2

STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", "no_response")

#: Outcomes, matching testinghq.barrage.runner.
COMPLETED = "completed"
ABANDONED = "abandoned"
DROPPED_BACKLOG = "dropped_backlog"
NOT_DISPATCHED = "not_dispatched"


@dataclass(frozen=True)
class Sample:
    """One scheduled request's measurement.

    Times are seconds from the start of the run. `dispatched_at` is None for a
    request that was never sent. `service_ms` and `offered_ms` are None unless
    the request completed. `status` is the HTTP status, None for no response.
    `in_hold` says whether it was scheduled in the steady-state hold.
    """

    scheduled_at: float
    dispatched_at: Optional[float]
    status: Optional[int]
    service_ms: Optional[float]
    offered_ms: Optional[float]
    target_rate: float
    outcome: str = COMPLETED
    index: int = 0
    in_hold: bool = True

    @property
    def sent(self) -> bool:
        return self.dispatched_at is not None and self.outcome in (COMPLETED, ABANDONED)

    @property
    def responded(self) -> bool:
        return self.outcome == COMPLETED and self.status is not None

    @property
    def is_shed(self) -> bool:
        return self.responded and self.status == 429

    @property
    def is_error(self) -> bool:
        if not self.sent:
            return False
        if not self.responded:
            return True
        return self.status >= 500 or self.status == 429

    @property
    def status_class(self) -> str:
        if not self.responded:
            return "no_response"
        return f"{min(max(self.status // 100, 1), 5)}xx"


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile, so the answer is always an observed value.
    None for no data, never a misleading 0.0."""
    if not values:
        return None
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction!r}")
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * fraction // 1))
    return ordered[int(rank) - 1]


def latency_percentiles(samples: Sequence[Sample], kind: str = "service") -> Dict[str, Optional[float]]:
    """p50, p90 and p99 of `kind` ("service" or "offered") latency in ms, over
    requests that got a response. A timeout's latency is the timeout setting,
    so it is counted as an error instead of skewing the distribution."""
    if kind not in ("service", "offered"):
        raise ValueError(f"kind must be 'service' or 'offered', got {kind!r}")
    values = [
        getattr(s, f"{kind}_ms") for s in samples
        if s.responded and getattr(s, f"{kind}_ms") is not None
    ]
    return {
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
    }


def latency_summary(samples: Sequence[Sample]) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    """Both latencies, over the whole run and over the hold stage only. The
    hold numbers are the ones to compare between runs: the ramp mixes rates."""
    hold = [s for s in samples if s.in_hold]
    return {
        kind: {
            "overall": latency_percentiles(samples, kind),
            "hold": latency_percentiles(hold, kind),
        }
        for kind in ("service", "offered")
    }


def status_classes(samples: Sequence[Sample]) -> Dict[str, int]:
    """Sent requests by status class. Abandoned requests are `no_response`."""
    counts = {name: 0 for name in STATUS_CLASSES}
    for s in samples:
        if s.sent:
            counts[s.status_class] += 1
    return counts


def request_counts(samples: Sequence[Sample]) -> Dict[str, int]:
    """Every scheduled request, by what happened to it."""
    return {
        "scheduled": len(samples),
        "sent": sum(1 for s in samples if s.sent),
        "completed": sum(1 for s in samples if s.outcome == COMPLETED),
        "abandoned": sum(1 for s in samples if s.outcome == ABANDONED),
        "dropped_backlog": sum(1 for s in samples if s.outcome == DROPPED_BACKLOG),
        "not_dispatched": sum(1 for s in samples if s.outcome == NOT_DISPATCHED),
    }


def error_rate(samples: Sequence[Sample]) -> float:
    """Fraction of sent requests that errored (5xx, 429, no response)."""
    sent = [s for s in samples if s.sent]
    if not sent:
        return 0.0
    return sum(1 for s in sent if s.is_error) / len(sent)


def throughput(samples: Sequence[Sample]) -> Dict[str, Any]:
    """Achieved versus targeted, in requests per second.

    Achieved is the inter-arrival rate over the real dispatch span: N sent
    requests span N - 1 intervals, so 25 requests sent exactly 0.1s apart are
    10/s. None with fewer than two sent requests, since there is no span.
    """
    sent = sorted(s.dispatched_at for s in samples if s.sent)
    targeted = max((s.target_rate for s in samples), default=None)
    span = (sent[-1] - sent[0]) if len(sent) >= 2 else None
    return {
        "targeted_rps": targeted,
        "achieved_rps": ((len(sent) - 1) / span) if span else None,
        "total_requests": len(sent),
        "dispatch_span_seconds": span,
    }


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    """One time slice: what was targeted, what went out, how it behaved."""

    start: float
    targeted_rate: float
    achieved_rate: float
    expected: float
    count: int
    errors: int
    error_rate: float
    shed: int
    dropped_backlog: int
    hold: bool
    status_classes: Dict[str, int] = field(default_factory=dict)
    service_ms: Dict[str, Optional[float]] = field(default_factory=dict)
    offered_ms: Dict[str, Optional[float]] = field(default_factory=dict)
    #: Seconds of the plan this bucket covers: the bucket width, or less for a
    #: last bucket that runs past the planned duration. None when unknown.
    span: Optional[float] = None

    @property
    def p50_latency_ms(self) -> Optional[float]:
        return self.service_ms.get("p50")


def _planned_rate_integral(stages: Sequence, start: float, end: float) -> float:
    """Requests the plan asked for in [start, end), from (rate, duration)
    stages laid end to end from 0."""
    total = 0.0
    at = 0.0
    for stage in stages:
        rate, duration = _stage_tuple(stage)
        lo, hi = max(start, at), min(end, at + duration)
        if hi > lo:
            total += rate * (hi - lo)
        at += duration
    return total


def _stage_tuple(stage):
    if isinstance(stage, (tuple, list)):
        return float(stage[0]), float(stage[1])
    return float(stage.rate), float(stage.duration)


def bucketize(
    samples: Sequence[Sample],
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    *,
    origin: Optional[float] = None,
    stages: Optional[Sequence] = None,
    hold_start: Optional[float] = None,
) -> List[Bucket]:
    """Fixed time buckets over the run.

    Sent requests land in the bucket of their dispatch time; requests dropped
    from the backlog in the bucket of their scheduled time. Empty buckets in
    the middle are kept, because a stretch where nothing went out is exactly
    what this report exists to show.

    With `stages`, each bucket's target is what the plan asked for in that
    window (so a bucket spanning several ramp steps, or a partial last bucket,
    is judged against the right number) and the series covers the planned
    duration even if nothing was sent. Without, the target is the highest
    stage rate seen in the bucket. `hold_start` marks buckets in the hold.
    """
    if bucket_seconds <= 0:
        raise ValueError(f"bucket_seconds must be > 0, got {bucket_seconds!r}")
    sent = [s for s in samples if s.sent]
    dropped = [s for s in samples if s.outcome == DROPPED_BACKLOG]
    if origin is None:
        anchors = [s.dispatched_at for s in sent] or [s.scheduled_at for s in samples]
        if not anchors:
            return []
        origin = min(anchors)

    def key(t: float) -> int:
        return max(0, int((t - origin) // bucket_seconds))

    by_key: Dict[int, List[Sample]] = {}
    for s in sent:
        by_key.setdefault(key(s.dispatched_at), []).append(s)
    dropped_by_key: Dict[int, int] = {}
    for s in dropped:
        k = key(s.scheduled_at)
        dropped_by_key[k] = dropped_by_key.get(k, 0) + 1

    keys = list(by_key) + list(dropped_by_key)
    n = (max(keys) + 1) if keys else 0
    if stages:
        planned = sum(_stage_tuple(st)[1] for st in stages)
        n = max(n, int(-(-(planned - 1e-9) // bucket_seconds)))
    buckets: List[Bucket] = []
    for k in range(n):
        start = origin + k * bucket_seconds
        members = by_key.get(k, [])
        span = bucket_seconds
        if stages:
            expected = _planned_rate_integral(stages, start, start + bucket_seconds)
            targeted = expected / bucket_seconds
            span = max(0.0, min(start + bucket_seconds, planned) - max(start, 0.0))
        elif members:
            targeted = max(s.target_rate for s in members)
            expected = targeted * bucket_seconds
        else:
            targeted = buckets[-1].targeted_rate if buckets else 0.0
            expected = targeted * bucket_seconds
        errors = sum(1 for s in members if s.is_error)
        buckets.append(Bucket(
            start=start,
            targeted_rate=targeted,
            achieved_rate=len(members) / bucket_seconds,
            expected=expected,
            count=len(members),
            errors=errors,
            error_rate=(errors / len(members)) if members else 0.0,
            shed=sum(1 for s in members if s.is_shed),
            dropped_backlog=dropped_by_key.get(k, 0),
            hold=True if hold_start is None else start >= hold_start - 1e-9,
            status_classes=status_classes(members),
            service_ms=latency_percentiles(members, "service"),
            offered_ms=latency_percentiles(members, "offered"),
            span=span,
        ))
    return buckets


# ---------------------------------------------------------------------------
# The knee
# ---------------------------------------------------------------------------


def _baseline(buckets: Sequence[Bucket]) -> tuple:
    """Median service latency of the hold stage's first two buckets that have
    one, averaged, and the buckets it came from."""
    chosen = [b for b in buckets if b.hold and b.p50_latency_ms is not None][:2]
    if not chosen:
        return None, []
    return sum(b.p50_latency_ms for b in chosen) / len(chosen), chosen


def client_ceiling_rps(bucket: Bucket, concurrency: Optional[int]) -> Optional[float]:
    """Closed loop: the most `concurrency` workers could send per second at the
    bucket's median service latency, since each waits for its last response
    before sending again. None when unknown (open loop, or nothing completed)."""
    p50 = bucket.p50_latency_ms
    if not concurrency or p50 is None or p50 <= 0:
        return None
    return concurrency * 1000.0 / p50


def _client_limited(bucket: Bucket, concurrency: Optional[int], divergence: float) -> bool:
    """True when a closed-loop bucket's shortfall is the client's: the plan
    asked for more than the workers could offer at the latency observed, and
    the bucket did send about what they could. Such a shortfall says nothing
    about the endpoint. Judged in requests over the planned part of the
    bucket, so a short last bucket is held to what fits in it."""
    ceiling = client_ceiling_rps(bucket, concurrency)
    span = bucket.span
    if ceiling is None or not span:
        return False
    capacity = ceiling * span
    if capacity >= bucket.expected:
        return False
    return bucket.count >= capacity * (1 - divergence)


def _falls_short(bucket: Bucket, divergence: float) -> bool:
    return (
        bucket.expected >= KNEE_MIN_EXPECTED
        and bucket.count < bucket.expected * (1 - divergence)
    )


def _judge(bucket, baseline, baseline_buckets, error_threshold, latency_factor, divergence,
           concurrency=None):
    if bucket.count and bucket.error_rate >= error_threshold:
        five = bucket.status_classes.get("5xx", 0)
        none = bucket.status_classes.get("no_response", 0)
        return "shedding", (
            f"error rate {bucket.error_rate:.1%} crossed the {error_threshold:.1%} "
            f"threshold (5xx {five}, shed 429 {bucket.shed}, no response {none})"
        )
    if _falls_short(bucket, divergence) and not _client_limited(bucket, concurrency, divergence):
        return "falling behind", (
            f"sent {bucket.count} of the {bucket.expected:.0f} the plan targeted in "
            f"this window ({bucket.achieved_rate:.2f}/s of {bucket.targeted_rate:.2f}/s)"
        )
    if (
        baseline
        and not any(bucket is b for b in baseline_buckets)
        and bucket.p50_latency_ms is not None
        and bucket.p50_latency_ms > baseline * latency_factor
    ):
        return "slowing", (
            f"median service latency {bucket.p50_latency_ms:.1f}ms exceeded "
            f"{latency_factor:g}x the {baseline:.1f}ms hold baseline"
        )
    return None


def find_knee(
    buckets: Sequence[Bucket],
    error_threshold: float = KNEE_ERROR_RATE,
    latency_factor: float = KNEE_LATENCY_FACTOR,
    divergence: float = KNEE_DIVERGENCE,
    persistence: int = KNEE_PERSISTENCE,
    *,
    concurrency: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """The first bucket that starts a run of `persistence` consecutive bad
    buckets, or None when the endpoint held.

    A bucket is bad when it is shedding (errors at or above the threshold),
    falling behind (sent well short of what the plan targeted), or slowing
    (median service latency above `latency_factor` times the baseline from the
    hold stage's first two buckets). Requiring persistence means one jittery
    second is not reported as a breaking point.

    `concurrency` is the closed loop's worker count (None for open loop).
    There, offered load is min(rate, workers / latency), so a bucket that sent
    about what its workers could at the latency it saw is not falling behind:
    the shortfall is the client's (see `concurrency_limit`).
    """
    baseline, baseline_buckets = _baseline(buckets)
    verdicts = [
        _judge(b, baseline, baseline_buckets, error_threshold, latency_factor, divergence,
               concurrency)
        for b in buckets
    ]
    for i in range(len(buckets) - persistence + 1):
        window = verdicts[i:i + persistence]
        if all(window):
            reason, detail = window[0]
            bucket = buckets[i]
            return {
                "at_seconds": bucket.start,
                "reason": reason,
                "detail": detail,
                "targeted_rps": bucket.targeted_rate,
                "achieved_rps": bucket.achieved_rate,
            }
    return None


def concurrency_limit(
    buckets: Sequence[Bucket],
    concurrency: Optional[int],
    divergence: float = KNEE_DIVERGENCE,
) -> Optional[Dict[str, Any]]:
    """Closed loop: the buckets that sent short of the plan because the
    workers could not offer more at the latency observed, or None.

    `ceiling_rps` is the lowest per-bucket ceiling among them (workers / median
    service latency), the number to compare with the targeted rate."""
    limited = [
        b for b in buckets
        if _falls_short(b, divergence) and _client_limited(b, concurrency, divergence)
    ]
    if not limited:
        return None
    ceilings = [client_ceiling_rps(b, concurrency) for b in limited]
    return {
        "concurrency": concurrency,
        "buckets": len(limited),
        "ceiling_rps": round(min(c for c in ceilings if c is not None), 3),
        "targeted_rps": max(b.expected / b.span for b in limited),
    }


def _closed_loop_concurrency(config: Dict[str, Any]) -> Optional[int]:
    """The worker count when the run was closed loop, else None."""
    if config.get("mode") != "closed":
        return None
    workers = config.get("concurrency")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        return None
    return workers


# ---------------------------------------------------------------------------
# Artifact and human summary
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1


def _r(value: Optional[float], places: int) -> Optional[float]:
    return None if value is None else round(value, places)


def record_rows(samples: Sequence[Sample]) -> List[Dict[str, Any]]:
    """The per-request `records` key: one row per scheduled request, in index
    order, the rows `compare` diffs two load runs from."""
    return [
        {
            "index": s.index,
            "scheduled_at": _r(s.scheduled_at, 6),
            "dispatched_at": _r(s.dispatched_at, 6),
            "status": s.status,
            "service_ms": _r(s.service_ms, 3),
            "offered_ms": _r(s.offered_ms, 3),
            "outcome": s.outcome,
        }
        for s in sorted(samples, key=lambda s: s.index)
    ]


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    samples: Sequence[Sample],
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    *,
    stages: Optional[Sequence] = None,
    hold_start: Optional[float] = None,
    origin: Optional[float] = None,
    aborted: bool = False,
) -> Dict[str, Any]:
    """The full Barrage run artifact. Deterministic for deterministic input."""
    buckets = bucketize(
        samples, bucket_seconds, origin=origin, stages=stages, hold_start=hold_start
    )
    concurrency = _closed_loop_concurrency(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "barrage",
        "aborted": bool(aborted),
        "seed": seed,
        "config": dict(config),
        "summary": {
            "requests": request_counts(samples),
            "throughput": throughput(samples),
            "latency_ms": latency_summary(samples),
            "status_classes": status_classes(samples),
            "shed": sum(1 for s in samples if s.is_shed),
            "error_rate": error_rate(samples),
            "knee": find_knee(buckets, concurrency=concurrency),
            "concurrency_limited": concurrency_limit(buckets, concurrency),
        },
        "buckets": [
            {
                "start": b.start,
                "hold": b.hold,
                "targeted_rps": b.targeted_rate,
                "achieved_rps": b.achieved_rate,
                "count": b.count,
                "errors": b.errors,
                "error_rate": b.error_rate,
                "shed": b.shed,
                "dropped_backlog": b.dropped_backlog,
                "status_classes": b.status_classes,
                "service_ms": b.service_ms,
                "offered_ms": b.offered_ms,
            }
            for b in buckets
        ],
        "records": record_rows(samples),
    }


def _ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def _rps(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}/s"


def format_summary(artifact: Dict[str, Any]) -> str:
    """Render a Barrage artifact as text. A run that held and a run that fell
    over must read differently, so the knee always gets its own line."""
    summary = artifact.get("summary") or {}
    requests = summary.get("requests") or {}
    tp = summary.get("throughput") or {}
    latency = summary.get("latency_ms") or {}
    classes = summary.get("status_classes") or {}
    knee = summary.get("knee")
    config = artifact.get("config") or {}

    lines: List[str] = []
    mode = f", {config['mode']}-loop" if config.get("mode") else ""
    lines.append(
        f"Barrage run seed={artifact.get('seed')}{mode}, "
        f"{tp.get('total_requests', 0)} request(s) sent"
    )
    if artifact.get("aborted"):
        lines.append("ABORTED: interrupted before the plan finished; partial results below.")
    lines.append(
        f"  scheduled {requests.get('scheduled', 0)}, completed {requests.get('completed', 0)}, "
        f"abandoned {requests.get('abandoned', 0)}, dropped (backlog) "
        f"{requests.get('dropped_backlog', 0)}, not dispatched {requests.get('not_dispatched', 0)}"
    )
    lines.append("")
    lines.append("Throughput:")
    lines.append(f"  targeted: {_rps(tp.get('targeted_rps'))}")
    span = tp.get("dispatch_span_seconds")
    over = f" over a {span:.2f}s dispatch span" if span else ""
    lines.append(f"  achieved: {_rps(tp.get('achieved_rps'))}{over}")
    lines.append("")
    lines.append("Latency (service: on the wire; offered: scheduled arrival to response):")
    lines.append(f"  {'':16s}{'p50':>10s}{'p90':>10s}{'p99':>10s}")
    for kind in ("service", "offered"):
        for scope in ("overall", "hold"):
            row = (latency.get(kind) or {}).get(scope) or {}
            lines.append(
                f"  {kind + ' ' + scope:16s}{_ms(row.get('p50')):>10s}"
                f"{_ms(row.get('p90')):>10s}{_ms(row.get('p99')):>10s}"
            )
    lines.append("")
    shown = ", ".join(f"{k} {v}" for k, v in classes.items() if v) or "none sent"
    lines.append(f"Status: {shown}; shed (429): {summary.get('shed', 0)}")
    lines.append(
        f"Error rate: {summary.get('error_rate', 0.0):.1%} (5xx, 429 shed, no response)"
    )
    lines.append("")
    if knee:
        lines.append(
            f"Knee: endpoint began {knee['reason']} at {knee['at_seconds']:.1f}s "
            f"({knee['detail']})"
        )
        lines.append(
            f"  targeted {_rps(knee.get('targeted_rps'))}, "
            f"achieved {_rps(knee.get('achieved_rps'))} at that point"
        )
    else:
        lines.append("Knee: none found, the endpoint held for the whole run.")
    limited = summary.get("concurrency_limited")
    if isinstance(limited, dict):
        lines.append(
            f"Closed loop: {limited.get('buckets')} bucket(s) sent short of the plan "
            f"because {limited.get('concurrency')} worker(s) at the latency observed can "
            f"offer at most {_rps(limited.get('ceiling_rps'))} of the "
            f"{_rps(limited.get('targeted_rps'))} targeted."
        )
        lines.append(
            "  The run was limited by --concurrency, so that shortfall is this "
            "tool's, not the endpoint's. Raise --concurrency."
        )
    lines.append("")
    lines.append("Per bucket:")
    for b in artifact.get("buckets") or []:
        service = b.get("service_ms") or {}
        offered = b.get("offered_ms") or {}
        lines.append(
            f"  t={b['start']:6.1f}s{'' if b.get('hold', True) else ' ramp'}  "
            f"{b['achieved_rps']:6.2f}/s of {b['targeted_rps']:6.2f}/s  "
            f"errors {b['error_rate']:5.1%}  "
            f"p50 {_ms(service.get('p50'))}  p99 {_ms(service.get('p99'))}  "
            f"offered p99 {_ms(offered.get('p99'))}"
        )
    return "\n".join(lines)
