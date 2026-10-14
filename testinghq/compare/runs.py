"""The comparison itself: two run artifacts in, one difference report out.

Design notes, since the choices here are not obvious.

ALIGNMENT. Records are aligned by `id`, which the engine builds as
`{category}-{seed}-{index:04d}`, and fall back to positional index when an id
is missing. Positional alignment is the weaker option and is labelled as such
in the report. When no record aligns at all there is nothing to compare, and
that is refused rather than reported as "no regressions".

DRY RUNS. A dry run has no responses, so comparing one against a real run
would report every flag as a fix or a regression. Either side being a dry run
is refused.

COMPARABILITY. A diff between two runs that used different seeds is not a diff
of your change, it is a diff of two different corpora. That is reported as a
warning, not an error: comparing runs from different targets or different
category mixes can be exactly what someone wants, and refusing would be
presumptuous. But a same-corpus comparison is the case the tool is built for,
so it is the case it reports on most precisely.

OUTCOME, NOT STATUS. The unit of comparison is `classify_record`'s verdict, not
the HTTP status. A candidate run that returns 400 where the baseline returned
422 has not regressed if the payload is structurally malformed, because a clean
4xx is a PASS for that category. Diffing raw statuses would flag it anyway and
teach people to ignore the output. Status movement is still reported, as a
separate, clearly subordinate section.

DIRECTION. `ok` to anything else is a regression. Anything else to `ok` is a
fix. Failure to a different failure is a change, neither good nor bad on its
own, because swapping one kind of breakage for another is not obviously an
improvement and should not be counted as one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import report
from . import load

# Compare used to report 0 no-regression, 1 regression, 2 usage error. That
# collides with every other tool, where 1 is a refusal and 2 is a dry run, so a
# script that read 1 as "refused" reported a regression as the tool declining to
# run. It now shares the package convention: a regression is a finding (3) and a
# usage error is a refusal (1).
#
# The three names stay, because they say what this tool means by each code, but
# they are the shared values and not a second set of numbers. They and the
# shared names are re-exported for cli/barrage.py and for scripts.
from ..core.exit_codes import (
    EXIT_FINDING,
    EXIT_NO_REGRESSION,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REGRESSION,
    EXIT_USAGE,
)

__all__ = [
    "EXIT_FINDING",
    "EXIT_NO_REGRESSION",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_REGRESSION",
    "EXIT_USAGE",
    "MAX_LISTED",
    "SCHEMA_VERSION",
    "STATUS_CLASS_KEYS",
    "CompareError",
    "compare",
    "compare_paths",
    "format_any",
    "format_diff",
    "load_artifact",
    "read_artifact",
    "validate_artifact",
]

#: The difference report's schema; written as `schema_version`.
SCHEMA_VERSION = 1

#: Cap on how many individual record transitions are listed. A change that
#: breaks four hundred payloads is a real finding, not four hundred findings,
#: and a terminal that scrolls past is a terminal nobody reads. The counts are
#: always exact; only the detail list is truncated, and it says so.
MAX_LISTED = 25


class CompareError(ValueError):
    """Raised when an artifact cannot be read or is not shaped like one."""


def read_artifact(path: str) -> Any:
    """Read a JSON file, refusing one that is missing or not JSON."""
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompareError(f"could not read {path!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompareError(f"{path!r} is not valid JSON: {exc}") from exc


def load_artifact(path: str) -> Dict[str, Any]:
    """Read and shape-check a run artifact written by `blast fire`."""
    return validate_artifact(read_artifact(path), path)


def _kind(data: Any) -> str:
    """Which tool wrote `data`, as far as compare cares: "barrage", "blast"
    (also any file naming no tool, which the blast checks then judge), or
    the other tool's name."""
    if load.is_barrage(data):
        return "barrage"
    if not isinstance(data, dict):
        return "blast"
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    tool = data.get("tool") or config.get("tool")
    return tool if isinstance(tool, str) and tool else "blast"


def compare_paths(baseline_path: str, candidate_path: str) -> Dict[str, Any]:
    """Read, check and compare two artifacts of the same tool: blast runs
    by per-payload outcome, barrage runs by load metrics. Refuses a pair
    from two different tools and any other tool's artifact."""
    baseline = read_artifact(baseline_path)
    candidate = read_artifact(candidate_path)
    kinds = {baseline_path: _kind(baseline), candidate_path: _kind(candidate)}
    for path, kind in kinds.items():
        if kind not in ("blast", "barrage"):
            raise CompareError(
                f"{path!r} is a {kind} artifact; compare reads blast and "
                "barrage run artifacts"
            )
    base_kind, cand_kind = kinds[baseline_path], kinds[candidate_path]
    if base_kind != cand_kind:
        raise CompareError(
            f"cannot compare a {base_kind} artifact ({baseline_path!r}) with a "
            f"{cand_kind} artifact ({candidate_path!r}): blast runs are compared "
            "payload by payload and barrage runs by load metrics, so both "
            "sides must come from the same tool"
        )
    if base_kind == "blast":
        return compare(
            validate_artifact(baseline, baseline_path),
            validate_artifact(candidate, candidate_path),
        )
    try:
        return load.compare(
            load.validate(baseline, baseline_path),
            load.validate(candidate, candidate_path),
        )
    except load.LoadCompareError as exc:
        raise CompareError(str(exc)) from None


def format_any(diff: Dict[str, Any]) -> str:
    """Render either kind of difference report."""
    if diff.get("kind") == "barrage":
        return load.format_diff(diff)
    return format_diff(diff)


def validate_artifact(data: Any, source: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise CompareError(f"{source!r}: top level must be a JSON object")
    for key in ("seed", "config", "summary", "records"):
        if key not in data:
            raise CompareError(f"{source!r}: missing required key {key!r}")
    if not isinstance(data["records"], list):
        raise CompareError(f"{source!r}: 'records' must be a list")
    config = data["config"]
    if not isinstance(config, dict) or "count" not in config or "seed" not in config:
        raise CompareError(
            f"{source!r}: 'config' must be an object carrying at least 'seed' "
            "and 'count'"
        )
    version = data.get("schema_version", 0)
    if not isinstance(version, int) or version > report.SCHEMA_VERSION:
        raise CompareError(
            f"{source!r}: schema_version {version!r} is newer than this build "
            f"reads (up to {report.SCHEMA_VERSION})"
        )
    if config.get("dry_run") is True:
        raise CompareError(
            f"{source!r} is a dry run: nothing was sent, so it has no responses "
            "to compare"
        )
    return data


def _outcome(record: Dict[str, Any]) -> str:
    """The canonical verdict for one record.

    Delegates to `core.report`, which is also what produced the artifacts being
    compared. If that function raises on a malformed record, that is a bug in
    the artifact producer and should surface, not be swallowed into a
    convenient "unknown".
    """
    return report.classify_record(record)


def _record_key(record: Dict[str, Any], index: int) -> Tuple[str, int]:
    record_id = record.get("id")
    if isinstance(record_id, str) and record_id:
        return ("id", record_id)
    return ("index", index)


def _status_class(record: Dict[str, Any]) -> str:
    return report.status_class((record.get("response") or {}).get("status"))


def _align(records: List[Dict[str, Any]]) -> Tuple[Dict[Any, Dict[str, Any]], bool]:
    """Index records by id where possible, by position otherwise.

    Returns (key -> record, used_positional_fallback).
    """
    keyed: Dict[Any, Dict[str, Any]] = {}
    positional = False
    for index, record in enumerate(records):
        key = _record_key(record, index)
        if key[0] == "index":
            positional = True
        keyed[key] = record
    return keyed, positional


def _status_counts(summary: Dict[str, Any]) -> Dict[str, int]:
    """by_status_class, with a schema 0 artifact's `timeout` read as
    `no_response`, which is what it counted."""
    counts = dict(summary.get("by_status_class") or {})
    if "timeout" in counts and "no_response" not in counts:
        counts["no_response"] = counts.pop("timeout")
    return counts


def _comparable(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> List[str]:
    """Reasons these two runs are not a clean A/B of one change."""
    warnings: List[str] = []
    if baseline["seed"] != candidate["seed"]:
        warnings.append(
            f"seeds differ ({baseline['seed']} vs {candidate['seed']}), so the "
            "corpora are not the same payloads and this diff is not a clean "
            "comparison of one change"
        )
    if baseline["config"].get("count") != candidate["config"].get("count"):
        warnings.append(
            f"counts differ ({baseline['config'].get('count')} vs "
            f"{candidate['config'].get('count')})"
        )
    if baseline["config"].get("mix") != candidate["config"].get("mix"):
        warnings.append("category mixes differ")
    # An artifact from before attachment modes were recorded had none.
    base_attachments = baseline["config"].get("attachments", "none")
    cand_attachments = candidate["config"].get("attachments", "none")
    if base_attachments != cand_attachments:
        warnings.append(
            f"attachment modes differ ({base_attachments} vs {cand_attachments})"
        )
    if baseline["config"].get("target") != candidate["config"].get("target"):
        warnings.append(
            f"targets differ ({baseline['config'].get('target')} vs "
            f"{candidate['config'].get('target')})"
        )
    return warnings


def _delta(baseline_counts: Dict[str, int], candidate_counts: Dict[str, int], keys):
    """Per-key movement, in the given order (`keys`) so the output is stable.
    Called once for status classes and once for categories."""
    rows = []
    for key in keys:
        before = baseline_counts.get(key, 0)
        after = candidate_counts.get(key, 0)
        rows.append(
            {"key": key, "baseline": before, "candidate": after, "delta": after - before}
        )
    return rows


#: Status classes in the engine's own canonical order, so this tool's output
#: ordering matches `core.report`'s.
STATUS_CLASS_KEYS = report.STATUS_CLASSES


def compare(
    baseline: Dict[str, Any], candidate: Dict[str, Any]
) -> Dict[str, Any]:
    """Compare two validated artifacts and return a difference report.

    The report is a plain dict so it can be written to JSON as readily as it
    is printed. Raises CompareError when no record aligns, because a report
    of zero comparisons would read as "no regressions".
    """
    warnings = _comparable(baseline, candidate)

    base_by_key, base_positional = _align(baseline["records"])
    cand_by_key, cand_positional = _align(candidate["records"])
    used_positional = base_positional or cand_positional

    only_baseline = [k for k in base_by_key if k not in cand_by_key]
    only_candidate = [k for k in cand_by_key if k not in base_by_key]
    shared = [k for k in base_by_key if k in cand_by_key]
    if not shared:
        raise CompareError(
            "nothing to compare: no record in the baseline aligns with one in "
            f"the candidate ({len(base_by_key)} vs {len(cand_by_key)} record(s)"
            + (f"; {'; '.join(warnings)}" if warnings else "")
            + ")"
        )

    regressions: List[Dict[str, Any]] = []
    fixes: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    unchanged = 0

    for key in shared:
        base_record = base_by_key[key]
        cand_record = cand_by_key[key]
        before = _outcome(base_record)
        after = _outcome(cand_record)
        entry = {
            "id": cand_record.get("id") or f"index {key[1]}",
            "category": cand_record.get("category"),
            "from": before,
            "to": after,
            "status_from": _status_class(base_record),
            "status_to": _status_class(cand_record),
        }
        if before == after:
            unchanged += 1
        elif before == report.OK:
            entry["flag"] = report.flag_for_record(cand_record)
            regressions.append(entry)
        elif after == report.OK:
            entry["flag"] = report.flag_for_record(base_record)
            fixes.append(entry)
        else:
            changes.append(entry)

    if only_baseline or only_candidate:
        warnings.append(
            f"corpora differ in shape: {len(only_baseline)} record(s) only in "
            f"baseline, {len(only_candidate)} only in candidate"
        )
    if used_positional:
        warnings.append(
            "at least one artifact has records with no 'id', so records were "
            "aligned by position; that alignment is not reliable if the "
            "corpora differ in shape"
        )

    base_summary = baseline["summary"]
    cand_summary = candidate["summary"]
    rows = _delta(
        _status_counts(base_summary),
        _status_counts(cand_summary),
        STATUS_CLASS_KEYS,
    )
    rows += _delta(
        base_summary.get("by_category", {}),
        cand_summary.get("by_category", {}),
        report.CATEGORIES,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "compare",
        "comparable": not warnings,
        "warnings": warnings,
        "aligned_by": "index" if used_positional else "id",
        "baseline_seed": baseline["seed"],
        "candidate_seed": candidate["seed"],
        "records_compared": len(shared),
        "unchanged": unchanged,
        "regressions": {
            "count": len(regressions),
            "records": regressions[:MAX_LISTED],
            "truncated": len(regressions) > MAX_LISTED,
        },
        "fixes": {
            "count": len(fixes),
            "records": fixes[:MAX_LISTED],
            "truncated": len(fixes) > MAX_LISTED,
        },
        "changes": {
            "count": len(changes),
            "records": changes[:MAX_LISTED],
            "truncated": len(changes) > MAX_LISTED,
        },
        "totals": rows,
        "flags": {
            "baseline": len(base_summary.get("flags", [])),
            "candidate": len(cand_summary.get("flags", [])),
        },
        "regressed": len(regressions) > 0,
    }


def _trend(before: int, after: int) -> str:
    if after > before:
        return f"+{after - before}"
    if after < before:
        return f"{after - before}"
    return "="


def format_diff(diff: Dict[str, Any]) -> str:
    """Render the report for a terminal.

    Leads with the verdict, because the first question is always "do I care".
    Counts are always exact; only the record detail lists truncate, and they
    say how much was dropped.
    """
    lines: List[str] = []
    verdict = "REGRESSED" if diff["regressed"] else "no regressions"
    lines.append(
        f"compare: {verdict}  ({diff['records_compared']} record(s) "
        f"compared, aligned by {diff['aligned_by']})"
    )
    lines.append(
        f"  baseline seed {diff['baseline_seed']}  ->  "
        f"candidate seed {diff['candidate_seed']}"
    )

    for warning in diff["warnings"]:
        lines.append(f"  warning: {warning}")

    lines.append("")
    lines.append(
        f"  flags: {diff['flags']['baseline']} -> {diff['flags']['candidate']}"
    )

    sections = (
        ("REGRESSIONS (passed before, fails now)", diff["regressions"]),
        ("FIXES (failed before, passes now)", diff["fixes"]),
        ("CHANGES (fails both ways, differently)", diff["changes"]),
    )
    for title, section in sections:
        lines.append("")
        if section["count"] == 0:
            lines.append(f"  {title}: none")
            continue
        lines.append(f"  {title}: {section['count']}")
        for entry in section["records"]:
            detail = f"    {entry['id']}  [{entry['category']}]"
            detail += f"  {entry['from']} -> {entry['to']}"
            detail += f"  ({entry['status_from']} -> {entry['status_to']})"
            if entry.get("flag"):
                detail += f"  {entry['flag']}"
            lines.append(detail)
        if section["truncated"]:
            remaining = section["count"] - len(section["records"])
            lines.append(f"    ... and {remaining} more, not shown")

    status_moved = [
        row for row in diff["totals"] if row["delta"] != 0 and row["key"] in STATUS_CLASS_KEYS
    ]
    category_moved = [
        row for row in diff["totals"] if row["delta"] != 0 and row["key"] not in STATUS_CLASS_KEYS
    ]
    if status_moved or category_moved:
        lines.append("")
        lines.append("  TOTALS (only rows that moved)")
        for label, moved in (
            ("response class", status_moved),
            ("category", category_moved),
        ):
            if not moved:
                continue
            lines.append(f"    by {label}:")
            for row in moved:
                lines.append(
                    f"      {row['key']:<24} {row['baseline']:>6} -> "
                    f"{row['candidate']:<6} {_trend(row['baseline'], row['candidate'])}"
                )
    else:
        lines.append("")
        lines.append("  TOTALS: nothing moved")

    return "\n".join(lines)
