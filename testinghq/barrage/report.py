"""Load-oriented reporting for Barrage.

Barrage is a load generator that fires provider-shaped payloads at an
endpoint the operator controls, at a high but controlled rate, and reports
throughput, latency distribution, and error behaviour under sustained
load. It is a load tester against your own infrastructure. It is NOT an
email sender, NOT a flooding tool, and NOT for endpoints you do not own.

Where Blast's core/report.py asks "did the parser correctly reject this
garbage", Barrage asks a different question entirely: "did the pipeline
hold up". So this module reports throughput achieved versus targeted,
latency percentiles, error rate over time, and the knee (where the
endpoint starts shedding or slowing), rather than per-payload
category-versus-status expectations.

It deliberately mirrors core/report.py's shape rather than importing its
rules: same build_artifact/format_summary pattern, same "pure functions
only" discipline. The expectation rules there are about parser
correctness and do not apply to load.

Pure functions only: no I/O, no network, and no wall-clock reads. Every
function here takes already-measured samples and returns derived numbers,
so the reporting math is fully testable on synthetic inputs. Latency and
wall-clock values enter only as data passed in by the caller; nothing here
reads a clock, which keeps this module's output a pure function of its
input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# Bucket width for the error-rate-over-time and knee series. One second is
# the natural resolution for a load run measured in requests per second.
DEFAULT_BUCKET_SECONDS = 1.0

# Knee detection thresholds. The knee is where the endpoint starts shedding
# (errors climb) or slowing (latency degrades sharply). These are
# deliberately conservative: a knee report is a "look here" signal for an
# operator, not an automated verdict.
KNEE_ERROR_RATE = 0.05
KNEE_LATENCY_FACTOR = 2.0


@dataclass(frozen=True)
class Sample:
    """One completed request's measurement.

    `dispatch_time` is seconds from the start of the run (the caller
    normalizes; this module never reads a clock). `latency_ms` is the
    observed round trip. `status` is the HTTP status, or None for a
    transport failure or timeout, matching core/transport.py's
    TransportResult. `target_rate` is the rate the run was aiming for when
    this request was dispatched, which is what makes achieved-versus-
    targeted throughput reportable per bucket.
    """

    dispatch_time: float
    latency_ms: float
    status: Optional[int]
    target_rate: float

    @property
    def is_error(self) -> bool:
        """A request counts as an error if it never got a response (timeout
        or transport failure, status None) or the endpoint returned 5xx.
        A 4xx is the endpoint correctly rejecting something under load, not
        the pipeline failing to hold, so it is not counted as an error
        here."""
        return self.status is None or self.status >= 500


# ---------------------------------------------------------------------------
# Latency distribution
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """The `fraction` percentile of `values` using the nearest-rank method.

    Nearest-rank is chosen over interpolation on purpose: it always returns
    an actually-observed latency, so a reported p99 is a real request's
    latency rather than a number no request ever experienced. Returns None
    for an empty input rather than raising or inventing a zero: no data
    means no percentile, and a silent 0.0 would read as "extremely fast".
    """
    if not values:
        return None
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction!r}")
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * fraction // 1))  # ceil, integer safe
    return ordered[int(rank) - 1]


def latency_percentiles(samples: Sequence[Sample]) -> Dict[str, Optional[float]]:
    """p50, p90, and p99 of observed latency, in milliseconds.

    Computed over requests that actually got a response: a timeout's
    latency is "how long we waited before giving up", which is a property
    of the timeout setting, not of the endpoint's speed, and mixing it into
    the distribution would silently skew every percentile toward the
    timeout value. Timeouts are reported through the error rate instead.
    """
    responded = [s.latency_ms for s in samples if s.status is not None]
    return {
        "p50": percentile(responded, 0.50),
        "p90": percentile(responded, 0.90),
        "p99": percentile(responded, 0.99),
    }


# ---------------------------------------------------------------------------
# Throughput and error rate over time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    """One time slice of a run: what was targeted, what was achieved, and
    how it behaved."""

    start: float
    targeted_rate: float
    achieved_rate: float
    count: int
    errors: int
    error_rate: float
    p50_latency_ms: Optional[float]


def bucketize(
    samples: Sequence[Sample], bucket_seconds: float = DEFAULT_BUCKET_SECONDS
) -> List[Bucket]:
    """Group samples into fixed time buckets and derive per-bucket
    throughput, error rate, and median latency.

    Buckets are indexed from 0 relative to the earliest dispatch_time seen,
    so a run's series always starts at bucket 0 regardless of what the
    caller's zero point was. Empty buckets in the middle of a run are
    emitted with zero counts rather than skipped: a gap where the endpoint
    accepted nothing is exactly the kind of shedding this report exists to
    show, and dropping the bucket would hide it.
    """
    if bucket_seconds <= 0:
        raise ValueError(f"bucket_seconds must be > 0, got {bucket_seconds!r}")
    if not samples:
        return []

    origin = min(s.dispatch_time for s in samples)
    grouped: Dict[int, List[Sample]] = {}
    for sample in samples:
        key = int((sample.dispatch_time - origin) // bucket_seconds)
        grouped.setdefault(key, []).append(sample)

    buckets: List[Bucket] = []
    for key in range(max(grouped) + 1):
        members = grouped.get(key, [])
        count = len(members)
        errors = sum(1 for s in members if s.is_error)
        responded = [s.latency_ms for s in members if s.status is not None]
        # An empty bucket has no sample to read a target rate from; carry
        # the previous bucket's target forward so the targeted series stays
        # continuous across a stretch the endpoint dropped entirely.
        if members:
            targeted = max(s.target_rate for s in members)
        elif buckets:
            targeted = buckets[-1].targeted_rate
        else:
            targeted = 0.0
        buckets.append(
            Bucket(
                start=origin + key * bucket_seconds,
                targeted_rate=targeted,
                achieved_rate=count / bucket_seconds,
                count=count,
                errors=errors,
                error_rate=(errors / count) if count else 0.0,
                p50_latency_ms=percentile(responded, 0.50),
            )
        )
    return buckets


def throughput(
    samples: Sequence[Sample], bucket_seconds: float = DEFAULT_BUCKET_SECONDS
) -> Dict[str, Any]:
    """Overall achieved throughput versus targeted, in requests per second.

    Achieved is measured over the run's actual dispatch span. A run with a
    single sample has no span to divide by, so its achieved rate is
    reported as None rather than as a division by zero or a misleading
    infinity.
    """
    if not samples:
        return {"targeted_rps": None, "achieved_rps": None, "total_requests": 0}

    buckets = bucketize(samples, bucket_seconds)
    span = len(buckets) * bucket_seconds
    targeted = max(s.target_rate for s in samples)
    return {
        "targeted_rps": targeted,
        "achieved_rps": (len(samples) / span) if span > 0 else None,
        "total_requests": len(samples),
    }


def error_rate(samples: Sequence[Sample]) -> float:
    """Overall fraction of requests that errored (5xx or no response)."""
    if not samples:
        return 0.0
    return sum(1 for s in samples if s.is_error) / len(samples)


# ---------------------------------------------------------------------------
# The knee
# ---------------------------------------------------------------------------


def find_knee(
    buckets: Sequence[Bucket],
    error_threshold: float = KNEE_ERROR_RATE,
    latency_factor: float = KNEE_LATENCY_FACTOR,
) -> Optional[Dict[str, Any]]:
    """The first bucket where the endpoint starts shedding or slowing.

    Two independent signals, whichever fires first:
    - Shedding: the bucket's error rate crosses `error_threshold`.
    - Slowing: the bucket's median latency exceeds `latency_factor` times
      the baseline median established by the first bucket.

    The baseline is the first bucket with a measured latency, which is the
    lightest point of a ramped run and therefore the endpoint's healthy
    reference. Returns None when the endpoint held throughout, which is a
    real and expected result, not a missing value.
    """
    baseline = next((b.p50_latency_ms for b in buckets if b.p50_latency_ms is not None), None)

    for bucket in buckets:
        if bucket.count and bucket.error_rate >= error_threshold:
            return {
                "at_seconds": bucket.start,
                "reason": "shedding",
                "detail": (
                    f"error rate {bucket.error_rate:.1%} crossed the "
                    f"{error_threshold:.1%} threshold"
                ),
                "targeted_rps": bucket.targeted_rate,
                "achieved_rps": bucket.achieved_rate,
            }
        if (
            baseline is not None
            and baseline > 0
            and bucket.p50_latency_ms is not None
            and bucket.p50_latency_ms > baseline * latency_factor
        ):
            return {
                "at_seconds": bucket.start,
                "reason": "slowing",
                "detail": (
                    f"median latency {bucket.p50_latency_ms:.1f}ms exceeded "
                    f"{latency_factor:g}x the {baseline:.1f}ms baseline"
                ),
                "targeted_rps": bucket.targeted_rate,
                "achieved_rps": bucket.achieved_rate,
            }
    return None


# ---------------------------------------------------------------------------
# Artifact and human summary
# ---------------------------------------------------------------------------


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    samples: Sequence[Sample],
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
) -> Dict[str, Any]:
    """Assemble the full Barrage run artifact: seed, config, summary, and
    the per-bucket time series.

    Mirrors core/report.py's build_artifact shape (seed, config, summary,
    then the per-item detail) so both tools' artifacts read the same way,
    while the summary itself answers Barrage's question rather than
    Blast's. Deterministic given deterministic inputs: same samples, seed,
    and config always yield the same dict.
    """
    buckets = bucketize(samples, bucket_seconds)
    return {
        "seed": seed,
        "config": dict(config),
        "summary": {
            "throughput": throughput(samples, bucket_seconds),
            "latency_ms": latency_percentiles(samples),
            "error_rate": error_rate(samples),
            "knee": find_knee(buckets),
        },
        "buckets": [
            {
                "start": b.start,
                "targeted_rps": b.targeted_rate,
                "achieved_rps": b.achieved_rate,
                "count": b.count,
                "errors": b.errors,
                "error_rate": b.error_rate,
                "p50_latency_ms": b.p50_latency_ms,
            }
            for b in buckets
        ],
    }


def _format_ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def _format_rps(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}/s"


def format_summary(artifact: Dict[str, Any]) -> str:
    """Render a Barrage artifact as human-readable text.

    Pure formatting, no I/O: the caller decides whether to print it, write
    it, or both. A run that held and a run that fell over must read as
    visibly different reports, not as two similar tables of numbers, so the
    knee gets its own called-out line either way.
    """
    seed = artifact.get("seed")
    summary = artifact.get("summary") or {}
    tp = summary.get("throughput") or {}
    latency = summary.get("latency_ms") or {}
    knee = summary.get("knee")
    buckets = artifact.get("buckets") or []

    lines: List[str] = []
    lines.append(f"Barrage run seed={seed}, {tp.get('total_requests', 0)} request(s)")
    lines.append("")
    lines.append("Throughput:")
    lines.append(f"  targeted: {_format_rps(tp.get('targeted_rps'))}")
    lines.append(f"  achieved: {_format_rps(tp.get('achieved_rps'))}")
    lines.append("")
    lines.append("Latency:")
    lines.append(f"  p50: {_format_ms(latency.get('p50'))}")
    lines.append(f"  p90: {_format_ms(latency.get('p90'))}")
    lines.append(f"  p99: {_format_ms(latency.get('p99'))}")
    lines.append("")
    lines.append(f"Error rate: {summary.get('error_rate', 0.0):.1%}")
    lines.append("")
    if knee:
        lines.append(
            f"Knee: endpoint began {knee['reason']} at {knee['at_seconds']:.1f}s "
            f"({knee['detail']})"
        )
        lines.append(
            f"  targeted {_format_rps(knee.get('targeted_rps'))}, "
            f"achieved {_format_rps(knee.get('achieved_rps'))} at that point"
        )
    else:
        lines.append("Knee: none found, the endpoint held for the whole run.")
    lines.append("")
    lines.append("Error rate over time:")
    for bucket in buckets:
        lines.append(
            f"  t={bucket['start']:6.1f}s  "
            f"{bucket['achieved_rps']:6.2f}/s of {bucket['targeted_rps']:6.2f}/s  "
            f"errors {bucket['error_rate']:5.1%}  "
            f"p50 {_format_ms(bucket['p50_latency_ms'])}"
        )

    return "\n".join(lines)
