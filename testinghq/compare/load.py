"""Comparing two barrage runs: load metrics, not per-payload outcomes.

A barrage artifact's `records` are one row per scheduled request (index,
scheduled_at, dispatched_at, status, service_ms, offered_ms, outcome). Two
load runs share no payload identity worth aligning on, so the comparison is
of the distributions: error rate and its parts, the outcome categories, p50
and p99 latency, and achieved rate.

Every number is computed by `testinghq.barrage.report` from the records, the
same functions that wrote the barrage summary, so this module has no notion
of its own of what an error is.

A candidate REGRESSED when, against the baseline:

- the error rate (5xx, 429 shed, no response, abandoned; over sent requests)
  rose by more than ERROR_RATE_POINTS,
- the unsent rate (dropped from the backlog or never dispatched; over
  scheduled requests) rose by more than UNSENT_RATE_POINTS,
- the hold stage's p99 service or offered latency rose by more than
  P99_GROWTH and by at least P99_MIN_MS, or
- achieved rate fell by more than ACHIEVED_RPS_DROP, judged only when both
  runs targeted the same rate in the same mode.

The same margins in the other direction are reported as improvements. A move
inside the margins is reported and not judged: two runs of one build differ
by that much.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..barrage import report as barrage_report

#: Error rate (a fraction of sent requests) may rise by this much, one
#: percentage point, before it is a regression.
ERROR_RATE_POINTS = 0.01
#: Unsent rate (a fraction of scheduled requests), likewise one point.
UNSENT_RATE_POINTS = 0.01
#: Hold-stage p99 latency may grow by this fraction (20%)...
P99_GROWTH = 0.20
#: ...and must also grow by at least this many milliseconds, so a loopback
#: run moving from 2ms to 3ms is not reported as a 50% regression.
P99_MIN_MS = 5.0
#: Achieved rate may fall by this fraction (10%) before it is a regression.
ACHIEVED_RPS_DROP = 0.10

THRESHOLDS = {
    "error_rate_points": ERROR_RATE_POINTS,
    "unsent_rate_points": UNSENT_RATE_POINTS,
    "p99_growth": P99_GROWTH,
    "p99_min_ms": P99_MIN_MS,
    "achieved_rps_drop": ACHIEVED_RPS_DROP,
}

OUTCOMES = (
    barrage_report.COMPLETED,
    barrage_report.ABANDONED,
    barrage_report.DROPPED_BACKLOG,
    barrage_report.NOT_DISPATCHED,
)

#: Config keys that change what a load run measures. A difference is a
#: warning, not a refusal: comparing two rates can be the point.
_PLAN_KEYS = ("mode", "rate", "duration", "warmup", "ramp_steps", "concurrency",
              "target", "keepalive", "timeout")


class LoadCompareError(ValueError):
    """A barrage artifact that cannot be compared."""


def is_barrage(data: Any) -> bool:
    """Whether `data` is a barrage artifact: the 1.0 `tool` key, or for an
    older file the `buckets` series only barrage writes."""
    return isinstance(data, dict) and (data.get("tool") == "barrage" or "buckets" in data)


def validate(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Shape-check a barrage artifact for comparison. Refuses a dry run, a
    newer schema, and a file from before per-request records were written."""
    version = data.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version > barrage_report.SCHEMA_VERSION:
        raise LoadCompareError(
            f"{source!r}: schema_version {version!r} is newer than this build "
            f"reads (up to {barrage_report.SCHEMA_VERSION})"
        )
    config = data.get("config")
    if not isinstance(config, dict):
        raise LoadCompareError(f"{source!r}: 'config' must be an object")
    if config.get("dry_run") is True:
        raise LoadCompareError(
            f"{source!r} is a dry run: nothing was sent, so it has no responses "
            "to compare"
        )
    records = data.get("records")
    if not isinstance(records, list):
        raise LoadCompareError(
            f"{source!r} has no per-request 'records', which barrage writes "
            "since 1.0; re-run it with this version to compare it"
        )
    for i, row in enumerate(records):
        if not isinstance(row, dict) or row.get("outcome") not in OUTCOMES:
            raise LoadCompareError(
                f"{source!r}: record {i} is not a barrage request record "
                f"(outcome must be one of {list(OUTCOMES)})"
            )
        if not isinstance(row.get("scheduled_at"), (int, float)):
            raise LoadCompareError(f"{source!r}: record {i} has no scheduled_at")
    return data


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _samples(data: Dict[str, Any]) -> List[barrage_report.Sample]:
    """The records as the samples barrage itself computes from, with the
    hold stage marked the way barrage marks it (scheduled at or after the
    warmup)."""
    config = data["config"]
    rate = _num(config.get("rate")) or 0.0
    warmup = _num(config.get("warmup")) or 0.0
    samples = []
    for row in data["records"]:
        status = row.get("status")
        samples.append(barrage_report.Sample(
            index=row.get("index") if isinstance(row.get("index"), int) else len(samples),
            scheduled_at=float(row["scheduled_at"]),
            dispatched_at=_num(row.get("dispatched_at")),
            status=status if isinstance(status, int) and not isinstance(status, bool) else None,
            service_ms=_num(row.get("service_ms")),
            offered_ms=_num(row.get("offered_ms")),
            target_rate=rate,
            outcome=row["outcome"],
            in_hold=float(row["scheduled_at"]) >= warmup - 1e-9,
        ))
    return samples


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def measure(data: Dict[str, Any]) -> Dict[str, Any]:
    """One run's comparable numbers."""
    samples = _samples(data)
    sent = [s for s in samples if s.sent]
    counts = barrage_report.request_counts(samples)
    hold = [s for s in samples if s.in_hold]
    service = barrage_report.latency_percentiles(hold, "service")
    offered = barrage_report.latency_percentiles(hold, "offered")
    unsent = counts["dropped_backlog"] + counts["not_dispatched"]
    return {
        "requests": counts,
        "error_rate": barrage_report.error_rate(samples),
        "5xx_rate": _rate(sum(1 for s in sent if s.responded and s.status >= 500), len(sent)),
        "shed_rate": _rate(sum(1 for s in sent if s.is_shed), len(sent)),
        "no_response_rate": _rate(
            sum(1 for s in sent if s.outcome == barrage_report.COMPLETED and s.status is None),
            len(sent),
        ),
        "abandoned_rate": _rate(counts["abandoned"], len(sent)),
        "unsent_rate": _rate(unsent, counts["scheduled"]),
        "dropped_backlog_rate": _rate(counts["dropped_backlog"], counts["scheduled"]),
        "not_dispatched_rate": _rate(counts["not_dispatched"], counts["scheduled"]),
        "service_p50_ms": service["p50"],
        "service_p99_ms": service["p99"],
        "offered_p50_ms": offered["p50"],
        "offered_p99_ms": offered["p99"],
        "achieved_rps": barrage_report.throughput(samples)["achieved_rps"],
    }


#: (key, label, kind) in display order. kind says how a row is judged:
#: "points" a rate judged in percentage points, "p99" a latency judged by
#: growth, "rps" a throughput judged by drop, None reported only.
ROWS = (
    ("error_rate", "error rate", "points"),
    ("5xx_rate", "  5xx", None),
    ("shed_rate", "  shed (429)", None),
    ("no_response_rate", "  no response", None),
    ("abandoned_rate", "  abandoned at deadline", None),
    ("unsent_rate", "unsent rate", "points"),
    ("dropped_backlog_rate", "  dropped (backlog)", None),
    ("not_dispatched_rate", "  not dispatched", None),
    ("service_p50_ms", "service p50 (hold)", None),
    ("service_p99_ms", "service p99 (hold)", "p99"),
    ("offered_p50_ms", "offered p50 (hold)", None),
    ("offered_p99_ms", "offered p99 (hold)", "p99"),
    ("achieved_rps", "achieved rate", "rps"),
)


def _judge(kind: Optional[str], key: str, before: Optional[float],
           after: Optional[float], rps_judged: bool) -> Optional[str]:
    """"worse", "better" or None (inside the margin, or not judged)."""
    if kind is None or before is None or after is None:
        return None
    if kind == "points":
        limit = ERROR_RATE_POINTS if key == "error_rate" else UNSENT_RATE_POINTS
        if after - before > limit:
            return "worse"
        if before - after > limit:
            return "better"
        return None
    if kind == "p99":
        if after > before * (1 + P99_GROWTH) and after - before >= P99_MIN_MS:
            return "worse"
        if before > after * (1 + P99_GROWTH) and before - after >= P99_MIN_MS:
            return "better"
        return None
    if kind == "rps" and rps_judged:
        if after < before * (1 - ACHIEVED_RPS_DROP):
            return "worse"
        if before < after * (1 - ACHIEVED_RPS_DROP):
            return "better"
    return None


def _show(key: str, value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if key.endswith("_rate"):
        return f"{value:.1%}"
    if key.endswith("_ms"):
        return f"{value:.1f}ms"
    return f"{value:.2f}/s"


def _show_delta(key: str, before: Optional[float], after: Optional[float]) -> str:
    if before is None or after is None:
        return ""
    delta = after - before
    if key.endswith("_rate"):
        return f"{delta * 100:+.1f} pt"
    if key.endswith("_ms"):
        return f"{delta:+.1f}ms"
    return f"{delta:+.2f}/s"


def _warnings(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> List[str]:
    warnings = []
    base_cfg, cand_cfg = baseline["config"], candidate["config"]
    for key in _PLAN_KEYS:
        if base_cfg.get(key) != cand_cfg.get(key):
            warnings.append(f"{key} differs ({base_cfg.get(key)} vs {cand_cfg.get(key)})")
    for name, data in (("baseline", baseline), ("candidate", candidate)):
        if data.get("aborted") is True:
            warnings.append(f"the {name} run was aborted, so its numbers are partial")
    return warnings


def compare(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two validated barrage artifacts. Raises LoadCompareError when
    either sent nothing, since a rate over zero requests compares nothing."""
    before = measure(baseline)
    after = measure(candidate)
    for name, numbers in (("baseline", before), ("candidate", after)):
        if numbers["requests"]["sent"] == 0:
            raise LoadCompareError(
                f"nothing to compare: the {name} run sent no requests"
            )
    warnings = _warnings(baseline, candidate)
    rps_judged = all(
        baseline["config"].get(k) == candidate["config"].get(k) for k in ("mode", "rate")
    )

    metrics: List[Dict[str, Any]] = []
    regressions: List[Dict[str, Any]] = []
    improvements: List[Dict[str, Any]] = []
    for key, label, kind in ROWS:
        b, a = before[key], after[key]
        verdict = _judge(kind, key, b, a, rps_judged)
        row = {
            "metric": key, "baseline": b, "candidate": a,
            "delta": None if b is None or a is None else a - b,
            "judged": kind is not None and (kind != "rps" or rps_judged),
            "verdict": verdict,
        }
        metrics.append(row)
        if verdict:
            entry = {
                "id": key,
                "detail": f"{label.strip()} {_show(key, b)} -> {_show(key, a)} "
                          f"({_show_delta(key, b, a)})",
            }
            (regressions if verdict == "worse" else improvements).append(entry)

    return {
        "schema_version": 1,
        "tool": "compare",
        "kind": "barrage",
        "comparable": not warnings,
        "warnings": warnings,
        "baseline_seed": baseline.get("seed"),
        "candidate_seed": candidate.get("seed"),
        "records_compared": before["requests"]["scheduled"] + after["requests"]["scheduled"],
        "requests": {"baseline": before["requests"], "candidate": after["requests"]},
        "metrics": metrics,
        "thresholds": dict(THRESHOLDS),
        "achieved_rps_judged": rps_judged,
        "regressions": {"count": len(regressions), "records": regressions, "truncated": False},
        "fixes": {"count": len(improvements), "records": improvements, "truncated": False},
        "regressed": bool(regressions),
    }


def describe_thresholds() -> str:
    return (
        f"error and unsent rate up by more than {ERROR_RATE_POINTS * 100:g} point; "
        f"hold p99 up by more than {P99_GROWTH:.0%} and {P99_MIN_MS:g}ms; "
        f"achieved rate down by more than {ACHIEVED_RPS_DROP:.0%} "
        "(only when mode and rate match)"
    )


def format_diff(diff: Dict[str, Any]) -> str:
    """The load comparison for a terminal, verdict first."""
    base_req = diff["requests"]["baseline"]
    cand_req = diff["requests"]["candidate"]
    lines: List[str] = [
        f"compare (barrage): {'REGRESSED' if diff['regressed'] else 'no regressions'}  "
        f"(baseline {base_req['sent']} of {base_req['scheduled']} sent, "
        f"candidate {cand_req['sent']} of {cand_req['scheduled']} sent)",
        f"  baseline seed {diff['baseline_seed']}  ->  candidate seed {diff['candidate_seed']}",
    ]
    for warning in diff["warnings"]:
        lines.append(f"  warning: {warning}")
    for title, section in (("REGRESSIONS", diff["regressions"]),
                           ("IMPROVEMENTS", diff["fixes"])):
        lines.append("")
        if not section["count"]:
            lines.append(f"  {title}: none")
            continue
        lines.append(f"  {title}: {section['count']}")
        for entry in section["records"]:
            lines.append(f"    {entry['detail']}")
    lines.append("")
    lines.append(f"  {'METRIC':<26}{'baseline':>11}{'candidate':>11}  delta")
    labels = {key: label for key, label, _ in ROWS}
    for row in diff["metrics"]:
        key = row["metric"]
        mark = {"worse": "  worse", "better": "  better"}.get(row["verdict"] or "", "")
        if key == "achieved_rps" and not diff["achieved_rps_judged"]:
            mark = "  not judged: mode or rate differs"
        lines.append(
            f"    {labels[key]:<24}{_show(key, row['baseline']):>11}"
            f"{_show(key, row['candidate']):>11}  "
            f"{_show_delta(key, row['baseline'], row['candidate'])}{mark}"
        )
    lines.append("")
    lines.append(f"  thresholds: {describe_thresholds()}")
    return "\n".join(lines)
