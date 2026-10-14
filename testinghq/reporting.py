"""One reader for every run artifact TestingHQ writes.

Six tools write an artifact and each one grew its own shape. `blast` and
`barrage` came first, then the pipeline tools, then `compare`, and every one of
them chose its own keys. A CI system that wants to know what happened therefore
has to know which tool produced a file before it can read it, and that is the
kind of knowledge a script should not need.

This module is the one place that knows. It detects which tool wrote an
artifact, derives the verdict that tool would have printed, and produces a
summary with a stable shape, so a consumer parses one thing.

THE HARD PART, and the reason this is not a thin wrapper. Two things a summary
needs are not in the artifact:

The verdict. `verify` computes "VERIFIED" or "MISMATCHED" at print time and
never writes it down, because it was written before anything read the file
later. `loop` does the same. This module reimplements those two derivations,
and `tests/unit/test_report_command.py` pins them against the tools' own
formatters, so the two copies cannot drift without a test noticing.

The exit code. No artifact records one, because the exit code is the process's
answer and the artifact is the run's. This module derives the code the same way
each tool does, from the same fields, so `report --json` gives a CI system the
number it would have got from the shell. The field is called `exit_code`,
because that is the name the contract calls for and a consumer has one shape to
parse. Where it came from is a separate field, `exit_code_source`, exactly as
`verdict_source` is for the verdict: derived where the tool computed it only at
print time, recorded where the artifact says so outright. That way the honesty is
kept without renaming a key somebody is relying on, which is the trade this
module briefly got wrong.

DETECTION IS BY SHAPE, which is honest about a thing the file structure does not
make easy. Only the pipeline tools set `config.tool`; `blast`, `barrage` and
`compare` set nothing that names them. So each reader is a predicate over the
top-level keys, ordered most-specific first, and every one of them is a
deliberate claim about what that tool's artifact looks like. A tool that changes
its shape breaks its own reader here, loudly, rather than being silently
miscounted by a script somewhere else.

An artifact this module cannot identify is REFUSED, never guessed at. A summary
of the wrong artifact is worse than no summary, because it is confidently wrong
and a consumer has no way to tell.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.exit_codes import EXIT_FINDING, EXIT_OK


class ArtifactError(RuntimeError):
    """Raised for an artifact that cannot be read, or cannot be identified.

    Fails loud for the same reason `ReadbackError` does: a report that silently
    describes the wrong thing is worse than one that refuses.
    """


# ---------------------------------------------------------------------------
# The stable shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """Numbers a consumer can rely on, whatever wrote the file.

    Present and zero rather than absent when a tool has no such concept, so a
    consumer can read `counts.duplicated` without first checking which tool it
    is looking at. An absent key and a zero mean different things to a parser
    and a zero is the safe one to default to: it cannot be misread as "unknown"
    and acted on.
    """

    sent: int = 0
    found: int = 0
    passed: int = 0
    failed: int = 0
    missing: int = 0
    duplicated: int = 0
    extra: int = 0
    produced: int = 0
    records: int = 0
    scenarios: int = 0
    buckets: int = 0


@dataclass(frozen=True)
class Finding:
    """One thing that went wrong, as a string a human reads.

    Deliberately flat. The tools' findings are variously counts, lists of
    objects, and lists of strings, and a consumer that has to know which is
    which is the problem this module exists to remove.
    """

    kind: str
    detail: str


@dataclass(frozen=True)
class ArtifactSummary:
    """What every artifact reduces to.

    `tool` is what wrote it. `verdict` is the tool's own word where it has one
    and a derived word where it does not, in which case `verdict_source` says so
    rather than leaving the reader to assume. `exit_code_source` does the same
    for the exit code.
    """

    tool: str
    verdict: str
    verdict_source: str
    exit_code: int
    exit_code_source: str
    counts: Counts
    findings: Tuple[Finding, ...] = ()
    seed: Optional[int] = None
    target: Optional[str] = None
    dry_run: bool = False

    def to_json(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["findings"] = [asdict(f) for f in self.findings]
        payload["counts"] = asdict(self.counts)
        return payload


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _config(artifact: Dict[str, Any]) -> Dict[str, Any]:
    value = artifact.get("config")
    return value if isinstance(value, dict) else {}


def _summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    value = artifact.get("summary")
    return value if isinstance(value, dict) else {}


def _is_pipeline_tool(artifact: Dict[str, Any], name: str) -> bool:
    return _config(artifact).get("tool") == name


def detect_tool(artifact: Dict[str, Any]) -> Optional[str]:
    """Which tool wrote this, or None.

    Ordered most specific first, because the generic signals overlap. `records`
    is written by blast, verify and ledger; `records` plus `by_status_class` in
    the summary is blast, because no pipeline summary has a status breakdown.
    `buckets` is barrage alone, since it is the only artifact with a time series
    and no records at all.

    Returning None is a real answer and not a failure. The caller refuses on it.
    """
    if not isinstance(artifact, dict):
        return None

    # compare writes a difference report: no seed, no config, and a `regressed`
    # boolean it is the only producer of.
    if "regressed" in artifact and "seed" not in artifact and "config" not in artifact:
        return "compare"

    tool = _config(artifact).get("tool")
    if isinstance(tool, str) and tool:
        return tool

    if "buckets" in artifact:
        return "barrage"
    if "records" in artifact and "by_status_class" in _summary(artifact):
        return "blast"
    if "records" in artifact:
        # No tool key and no status breakdown. Nothing in the tree writes this,
        # so it is not guessed at: an artifact whose producer cannot be named
        # gets refused rather than attributed to a tool on the strength of one
        # shared key.
        return None
    return None


# ---------------------------------------------------------------------------
# Per-tool readers
# ---------------------------------------------------------------------------


def _base(artifact: Dict[str, Any], tool: str) -> Dict[str, Any]:
    config = _config(artifact)
    return {
        "tool": tool,
        "seed": artifact.get("seed") if isinstance(artifact.get("seed"), int) else None,
        "target": config.get("target") if isinstance(config.get("target"), str) else None,
        "dry_run": bool(config.get("dry_run", False)),
    }


#: The newest blast and compare artifact schema these readers know.
_BLAST_SCHEMA_VERSION = 1


def _check_schema_version(artifact: Dict[str, Any], tool: str, known: int) -> int:
    """The artifact's schema_version: missing is 0, above `known` is refused."""
    version = artifact.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version!r}, which is not "
            "a non-negative integer"
        )
    if version > known:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version}; this build "
            f"reads {tool} artifacts up to schema_version {known}"
        )
    return version


def _read_blast(artifact: Dict[str, Any]) -> ArtifactSummary:
    _check_schema_version(artifact, "blast", _BLAST_SCHEMA_VERSION)
    summary = _summary(artifact)
    records = artifact.get("records") or []
    flags = summary.get("flags") or []
    aborted = artifact.get("aborted") is True

    passed = sum(
        1 for r in records
        if isinstance(r, dict) and (r.get("assertion") or {}).get("passed") is True
    )
    failed = len(records) - passed

    # Each flag names its own reason, including why a request got no
    # response, so the flags are the findings.
    findings = [Finding(kind="flag", detail=str(flag)) for flag in flags]
    if aborted:
        findings.append(
            Finding(
                kind="aborted",
                detail="interrupted before every payload was sent; the counts are partial",
            )
        )
        verdict = "ABORTED"
    else:
        verdict = "FLAGGED" if flags else "CLEAN"
    return ArtifactSummary(
        verdict=verdict,
        # blast prints its verdict but the artifact carries the flags it is
        # derived from, so it is derived here the same way.
        verdict_source="derived",
        exit_code=EXIT_FINDING if (flags or aborted) else EXIT_OK,
        exit_code_source="derived",
        counts=Counts(
            sent=len(records),
            passed=passed,
            failed=failed,
            records=len(records),
        ),
        findings=tuple(findings),
        **_base(artifact, "blast"),
    )


def _read_barrage(artifact: Dict[str, Any]) -> ArtifactSummary:
    summary = _summary(artifact)
    buckets = artifact.get("buckets") or []
    knee = summary.get("knee")

    findings: List[Finding] = []
    if isinstance(knee, dict):
        findings.append(
            Finding(
                kind="knee",
                detail=f"{knee.get('reason', 'knee')} at "
                       f"{knee.get('at_seconds')}s: {knee.get('detail', '')}".strip(),
            )
        )
    total = sum(b.get("count", 0) for b in buckets if isinstance(b, dict))

    return ArtifactSummary(
        verdict="KNEE" if isinstance(knee, dict) else "STEADY",
        verdict_source="derived",
        # barrage's execute returns EXIT_OK for any completed run. Its health is
        # in the artifact, not the exit code, and pretending otherwise would
        # change what a CI script gating on the code does.
        exit_code=EXIT_OK,
        exit_code_source="derived",
        counts=Counts(sent=total, buckets=len(buckets)),
        findings=tuple(findings),
        **_base(artifact, "barrage"),
    )


#: The newest artifact schema the verify, ledger and redeliver readers know.
_CHECK_SCHEMA_VERSION = 1


def _check_schema(artifact: Dict[str, Any], tool: str) -> int:
    """The artifact's schema_version: missing is 0, above what this build
    knows is refused."""
    version = artifact.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version!r}, which is not "
            "a non-negative integer"
        )
    if version > _CHECK_SCHEMA_VERSION:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version}; this build "
            f"reads {tool} artifacts up to schema_version {_CHECK_SCHEMA_VERSION}"
        )
    return version


def _read_aborted(artifact: Dict[str, Any], label: str) -> Optional[ArtifactSummary]:
    """A verify, ledger or redeliver run stopped by Ctrl-C, or None.

    Nothing in such an artifact was graded, so it reads as ABORTED with exit 3
    and the number of messages sent (when known), never as a pass."""
    if artifact.get("aborted") is not True:
        return None
    summary = _summary(artifact)
    sent = summary.get("sent")
    phase = summary.get("aborted_during") or "unknown"
    detail = f"interrupted during the {phase} phase; nothing was graded"
    if not isinstance(sent, int):
        detail += ", and how many messages were sent is unknown"
    return ArtifactSummary(
        verdict=str(summary.get("verdict") or "ABORTED"),
        verdict_source="recorded",
        exit_code=EXIT_FINDING,
        exit_code_source="derived",
        counts=Counts(
            sent=sent if isinstance(sent, int) else 0,
            records=len(artifact.get("records") or []),
        ),
        findings=(Finding(kind="aborted", detail=detail),),
        **_base(artifact, label),
    )


def _read_verify(artifact: Dict[str, Any]) -> ArtifactSummary:
    _check_schema(artifact, "verify")
    aborted = _read_aborted(
        artifact, f"verify {_config(artifact).get('mode', 'fire')}"
    )
    if aborted is not None:
        return aborted
    summary = _summary(artifact)
    sent = summary.get("sent", 0)
    found = summary.get("found", 0)
    verified = summary.get("verified", 0)
    failed = summary.get("failed", 0)
    config = _config(artifact)
    mode = config.get("mode", "fire")

    findings = list(_skip_findings(summary.get("checks_skipped_by_reason") or {}))

    recorded = summary.get("verdict")
    if isinstance(recorded, str) and recorded:
        verdict, source = recorded, "recorded"
    else:
        # Schema 0 artifacts never wrote a verdict; derived the way verify's
        # own formatter derives it.
        source = "derived"
        if summary.get("unsettled"):
            verdict = "UNSETTLED"
        elif verified + failed == 0:
            verdict = "NOTHING CHECKED"
        elif failed == 0:
            verdict = "VERIFIED"
        else:
            verdict = "MISMATCHED"
    if verdict == "UNSETTLED":
        findings.append(
            Finding(
                kind="unsettled",
                detail="the readback never settled; messages not found may still "
                       "be in flight",
            )
        )

    return ArtifactSummary(
        verdict=verdict,
        verdict_source=source,
        exit_code=EXIT_OK if verdict in ("VERIFIED", "NOTHING CHECKED") else EXIT_FINDING,
        exit_code_source="derived",
        counts=Counts(
            sent=sent, found=found, passed=verified, failed=failed,
            records=len(artifact.get("records") or []),
        ),
        findings=tuple(findings),
        **_base(artifact, f"verify {mode}"),
    )


def _read_ledger(artifact: Dict[str, Any]) -> ArtifactSummary:
    _check_schema(artifact, "ledger")
    aborted = _read_aborted(artifact, "ledger")
    if aborted is not None:
        return aborted
    summary = _summary(artifact)
    findings: List[Finding] = []

    for entry in summary.get("missing") or []:
        findings.append(Finding(kind="missing", detail=str(entry)))
    for entry in summary.get("duplicated") or []:
        if isinstance(entry, dict):
            findings.append(
                Finding(
                    kind="duplicated",
                    detail=f"{entry.get('tag')} produced {entry.get('count')} "
                           f"records: {entry.get('tickets')}",
                )
            )
    for entry in summary.get("wrong") or []:
        if isinstance(entry, dict):
            findings.append(
                Finding(
                    kind="misparsed",
                    detail=f"{entry.get('tag')}: "
                           f"{', '.join(str(m) for m in entry.get('mismatches') or [])}",
                )
            )
    extra = summary.get("extra")
    extra_count = summary.get("extra_count")
    if extra is None:
        # Not "no strays", but "strays were not searched for". A consumer has
        # to be able to tell those apart, so it is its own finding.
        reason = summary.get("strays_not_searched_reason")
        findings.append(
            Finding(
                kind="strays-not-searched",
                detail="the adapter could not enumerate, so stray records were "
                       "never looked for" + (f" ({reason})" if reason else ""),
            )
        )
    else:
        for entry in extra:
            if isinstance(entry, dict):
                findings.append(
                    Finding(kind="extra", detail=str(entry.get("ticket") or entry))
                )
        if isinstance(extra_count, int) and extra_count > len(extra):
            findings.append(
                Finding(
                    kind="truncated",
                    detail=f"only the first {len(extra)} of {extra_count} extra "
                           "records are listed in the artifact",
                )
            )
        if summary.get("strays_partial"):
            reason = summary.get("strays_not_searched_reason")
            findings.append(
                Finding(
                    kind="strays-partial",
                    detail="the store was only partly enumerated, so more stray "
                           "records may exist" + (f" ({reason})" if reason else ""),
                )
            )
    other_runs = summary.get("other_runs_count")
    if isinstance(other_runs, int) and other_runs:
        findings.append(
            Finding(
                kind="other-runs",
                detail=f"{other_runs} record(s) carry another run's tag "
                       "(informational, not counted as extra)",
            )
        )
    if summary.get("verdict") == "UNSETTLED":
        findings.append(
            Finding(
                kind="unsettled",
                detail="the readback never settled; messages not found may still "
                       "be in flight",
            )
        )
    if not isinstance(extra_count, int):
        extra_count = len(extra or [])

    return ArtifactSummary(
        # ledger is the one tool that writes its verdict down, so it is read
        # rather than derived.
        verdict=str(summary.get("verdict", "UNKNOWN")),
        verdict_source="recorded",
        exit_code=EXIT_OK if summary.get("balanced") else EXIT_FINDING,
        exit_code_source="derived",
        counts=Counts(
            sent=summary.get("sent", 0),
            produced=summary.get("produced", 0),
            passed=summary.get("exactly_once", 0),
            missing=len(summary.get("missing") or []),
            duplicated=len(summary.get("duplicated") or []),
            extra=extra_count,
            records=len(artifact.get("records") or []),
        ),
        findings=tuple(findings),
        **_base(artifact, "ledger"),
    )


def _read_redeliver(artifact: Dict[str, Any]) -> ArtifactSummary:
    _check_schema(artifact, "redeliver")
    aborted = _read_aborted(artifact, "redeliver")
    if aborted is not None:
        return aborted
    summary = _summary(artifact)
    scenarios = artifact.get("scenarios") or []
    findings: List[Finding] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        for finding in scenario.get("findings") or []:
            findings.append(
                Finding(
                    kind=f"scenario:{scenario.get('scenario', '?')}",
                    detail=str(finding),
                )
            )
    for entry in summary.get("unattributed_strays") or []:
        if isinstance(entry, dict):
            findings.append(
                Finding(
                    kind="extra",
                    detail=f"ticket {entry.get('ticket')} carries this run's tag "
                           f"prefix ({entry.get('tag')}) but no scenario's",
                )
            )
    other_runs = summary.get("other_runs_count")
    if isinstance(other_runs, int) and other_runs:
        findings.append(
            Finding(
                kind="other-runs",
                detail=f"{other_runs} record(s) carry another run's tag "
                       "(informational)",
            )
        )
    if summary.get("verdict") == "UNSETTLED":
        findings.append(
            Finding(
                kind="unsettled",
                detail="the readback never settled; messages not found may still "
                       "be in flight",
            )
        )
    verdict = str(summary.get("verdict", "UNKNOWN"))

    return ArtifactSummary(
        verdict=verdict,
        verdict_source="recorded",
        exit_code=(
            EXIT_OK
            if summary.get("failed", 0) == 0 and verdict != "UNSETTLED"
            else EXIT_FINDING
        ),
        exit_code_source="derived",
        counts=Counts(
            sent=summary.get("deliveries", 0),
            passed=summary.get("passed", 0),
            failed=summary.get("failed", 0),
            scenarios=summary.get("scenarios", len(scenarios)),
        ),
        findings=tuple(findings),
        **_base(artifact, "redeliver"),
    )


#: The newest loop, steady and sigcheck artifact schema these readers know.
_WS6_SCHEMA_VERSION = 1


def _known_schema(artifact: Dict[str, Any], tool: str) -> int:
    """The artifact's `schema_version`: missing reads as 0, a value above what
    this build knows is refused rather than half-read."""
    version = artifact.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version!r}, which is not a "
            "non-negative integer"
        )
    if version > _WS6_SCHEMA_VERSION:
        raise ArtifactError(
            f"this {tool} artifact has schema_version {version}; this build reads "
            f"{tool} artifacts up to schema_version {_WS6_SCHEMA_VERSION}"
        )
    return version


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_loop(artifact: Dict[str, Any]) -> ArtifactSummary:
    """`loop`'s own verdict, recorded since schema 1 and derived before it,
    including the NOT CHECKED qualifier: a run that skipped the auto-reply
    check and one that passed it must not read the same on one line.

    `passed` is messages checked minus messages with a finding.
    """
    _known_schema(artifact, "loop")
    summary = _summary(artifact)
    aborted = artifact.get("aborted") is True or summary.get("aborted") is True
    findings_count = _int(summary.get("findings"))
    message_findings = _int(summary.get("message_findings", findings_count))
    checked = _int(summary.get("checked"))
    skipped = _int(summary.get("auto_reply_skipped"))

    recorded = summary.get("verdict")
    if isinstance(recorded, str) and recorded:
        verdict, source = recorded, "recorded"
    else:
        source = "derived"
        verdict = "LOOP-SAFE" if findings_count == 0 else "LOOPS-DETECTED"
        if skipped:
            verdict = f"{verdict} (auto-reply NOT CHECKED)"

    notes: List[Finding] = []
    if aborted:
        notes.append(Finding(kind="aborted",
                             detail="interrupted before the run finished; nothing was judged"))
    for line in summary.get("run_findings") or []:
        kind = "unattributed" if str(line).startswith("outbound, unattributed") else "run"
        if str(line).startswith("UNSETTLED"):
            kind = "unsettled"
        notes.append(Finding(kind=kind, detail=str(line)))
    for message in artifact.get("messages") or []:
        result = message.get("result") if isinstance(message, dict) else None
        if not isinstance(result, dict):
            continue
        for finding in result.get("findings") or []:
            kind = "loop-bait-answered" if result.get("is_loop_bait") and "bait" in str(finding) \
                else "machine-mail"
            notes.append(Finding(kind=kind, detail=f"{result.get('tag')}: {finding}"))
    if skipped:
        notes.append(
            Finding(
                kind="not-checked",
                detail=f"auto-reply check not run for {skipped} message(s): no sink "
                       "configured ([loop.outbound])",
            )
        )
    elif summary.get("outbound_enumerated") is False:
        notes.append(
            Finding(
                kind="not-checked",
                detail="the outbound sink cannot be enumerated, so an auto-reply "
                       "that carries no tag would not have been seen",
            )
        )
    parent = summary.get("bait_parent")
    if isinstance(parent, dict) and parent.get("source") == "fabricated":
        notes.append(
            Finding(
                kind="bait-fabricated",
                detail="the loop bait replied to a fabricated Message-ID"
                       + (f": {parent['reason']}" if parent.get("reason") else ""),
            )
        )

    failed = findings_count != 0 or aborted or verdict.startswith("UNSETTLED")
    return ArtifactSummary(
        verdict=verdict,
        verdict_source=source,
        exit_code=EXIT_FINDING if failed else EXIT_OK,
        exit_code_source="derived",
        counts=Counts(
            sent=_int(summary.get("sent")),
            passed=max(checked - message_findings, 0),
            failed=findings_count,
            records=checked,
        ),
        findings=tuple(notes),
        **_base(artifact, "loop"),
    )


def _read_steady(artifact: Dict[str, Any]) -> ArtifactSummary:
    """`steady`'s recorded verdict and gate. The exit code is the gate's."""
    _known_schema(artifact, "steady")
    stability = artifact.get("stability")
    stability = stability if isinstance(stability, dict) else {}
    gate = stability.get("gate") if isinstance(stability.get("gate"), dict) else {}
    aborted = artifact.get("aborted") is True

    recorded = stability.get("verdict")
    verdict = recorded if isinstance(recorded, str) and recorded else (
        "ABORTED" if aborted else "UNKNOWN"
    )
    source = "recorded" if isinstance(recorded, str) and recorded else "derived"
    failed = bool(gate.get("failed")) or aborted or not (isinstance(recorded, str) and recorded)

    findings: List[Finding] = []
    if aborted:
        findings.append(Finding(kind="aborted",
                                detail="interrupted before the run finished; nothing was measured"))
    if gate.get("failed") and gate.get("reason"):
        findings.append(Finding(kind="gate", detail=str(gate["reason"])))
    for family in stability.get("families") or []:
        if not isinstance(family, dict) or family.get("consistent", True):
            continue
        dissent = ", ".join(
            f"{d.get('transform')} ({d.get('language')}) got {d.get('label')!r}"
            for d in family.get("dissenters") or [] if isinstance(d, dict)
        )
        findings.append(Finding(
            kind="dissent",
            detail=f"{family.get('intent_id')}: majority {family.get('majority')!r}; {dissent}",
        ))
    unlabelled = stability.get("unlabelled_families") or []
    if unlabelled:
        findings.append(Finding(
            kind="not-measured",
            detail=f"{len(unlabelled)} family(ies) not fully labelled: "
                   + ", ".join(str(u) for u in unlabelled),
        ))
    reads = stability.get("label_reads") if isinstance(stability.get("label_reads"), dict) else {}
    unread = {k: v for k, v in reads.items() if k != "labelled" and _int(v)}
    if unread:
        findings.append(Finding(
            kind="label-reads",
            detail=", ".join(f"{v} {k}" for k, v in sorted(unread.items())),
        ))
    if stability.get("constant_label") is not None:
        findings.append(Finding(
            kind="constant-label",
            detail=f"every family read {stability['constant_label']!r}; check the label field",
        ))

    labelled = _int(stability.get("labelled_variants"))
    dissenters = _int(stability.get("dissenters"))
    variants = sum(
        len(f.get("variants") or []) for f in artifact.get("families") or [] if isinstance(f, dict)
    )
    return ArtifactSummary(
        verdict=verdict,
        verdict_source=source,
        exit_code=EXIT_FINDING if failed else EXIT_OK,
        exit_code_source="derived",
        counts=Counts(
            sent=_int(stability.get("sent")),
            found=labelled,
            passed=max(labelled - dissenters, 0),
            failed=dissenters,
            records=variants,
        ),
        findings=tuple(findings),
        **_base(artifact, "steady"),
    )


def _read_sigcheck(artifact: Dict[str, Any]) -> ArtifactSummary:
    """`sigcheck`'s recorded verdict; each variant the endpoint got wrong is a
    finding, and informational answers are listed apart."""
    _known_schema(artifact, "sigcheck")
    summary = _summary(artifact)
    aborted = artifact.get("aborted") is True
    findings: List[Finding] = []
    if aborted:
        findings.append(Finding(kind="aborted",
                                detail="interrupted before every variant was sent"))
    passed = failed = 0
    for item in artifact.get("variants") or []:
        if not isinstance(item, dict):
            continue
        verdict = item.get("verdict")
        if verdict == "FINDING":
            failed += 1
            findings.append(Finding(kind=str(item.get("variant")), detail=str(item.get("detail"))))
        elif verdict == "OK":
            passed += 1
        elif verdict == "INFO":
            findings.append(Finding(kind=f"info:{item.get('variant')}",
                                    detail=str(item.get("detail"))))
    for note in summary.get("notes") or []:
        findings.append(Finding(kind="note", detail=str(note)))
    recorded = summary.get("verdict")
    verdict = recorded if isinstance(recorded, str) and recorded else (
        "ENFORCED" if failed == 0 and not aborted else "GAPS"
    )
    return ArtifactSummary(
        verdict=verdict,
        verdict_source="recorded" if isinstance(recorded, str) and recorded else "derived",
        exit_code=EXIT_FINDING if failed or aborted else EXIT_OK,
        exit_code_source="derived",
        counts=Counts(sent=_int(summary.get("sent")), passed=passed, failed=failed,
                      records=len(artifact.get("variants") or [])),
        findings=tuple(findings),
        **_base(artifact, "sigcheck"),
    )


def _read_compare(artifact: Dict[str, Any]) -> ArtifactSummary:
    _check_schema_version(artifact, "compare", _BLAST_SCHEMA_VERSION)
    regressions = artifact.get("regressions") or {}
    fixes = artifact.get("fixes") or {}
    regressed = bool(artifact.get("regressed"))

    findings = [
        Finding(kind="regression", detail=str(r.get("id")))
        for r in (regressions.get("records") or [])
        if isinstance(r, dict)
    ]
    if regressions.get("truncated"):
        findings.append(
            Finding(
                kind="truncated",
                detail=f"only the first {regressions.get('count')} regressions are "
                       "listed; the artifact carries the count, not all of them",
            )
        )

    warnings = artifact.get("warnings") or []
    if not artifact.get("comparable", True):
        findings.append(
            Finding(
                kind="not-comparable",
                detail="; ".join(str(w) for w in warnings) or "the two artifacts "
                       "could not be aligned",
            )
        )

    return ArtifactSummary(
        verdict="REGRESSED" if regressed else "NO REGRESSION",
        verdict_source="recorded",
        exit_code=EXIT_FINDING if regressed else EXIT_OK,
        exit_code_source="derived",
        counts=Counts(
            records=artifact.get("records_compared", 0),
            failed=regressions.get("count", 0),
            passed=fixes.get("count", 0),
        ),
        findings=tuple(findings),
        **_base(artifact, "compare"),
    )


_READERS = {
    "blast": _read_blast,
    "barrage": _read_barrage,
    "verify": _read_verify,
    "ledger": _read_ledger,
    "redeliver": _read_redeliver,
    "loop": _read_loop,
    "steady": _read_steady,
    "sigcheck": _read_sigcheck,
    "compare": _read_compare,
}


#: Tools this module can name. A consumer can check membership rather than
#: catching an exception, and a new tool that lands without a reader is visible
#: as a missing entry.
KNOWN_TOOLS = tuple(sorted(_READERS))


# ---------------------------------------------------------------------------
# Skipped checks
# ---------------------------------------------------------------------------


def _skip_findings(by_reason: Dict[str, Any]) -> Tuple[Finding, ...]:
    """Skipped checks, one finding per reason rather than per check.

    Per check would produce dozens of near-identical lines for a run where the
    adapter simply could not see the body. The reason and the count is the
    information, and a consumer can still read the counts out of the artifact.
    """
    findings = []
    for reason, checks in sorted(by_reason.items()):
        if not isinstance(checks, dict):
            continue
        names = sorted(checks)
        if not names:
            continue
        total = sum(v for v in checks.values() if isinstance(v, int))
        findings.append(
            Finding(
                kind=f"skipped:{reason}",
                detail=f"{total} check(s) not verified: {', '.join(names)}",
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def summarize(data: Any) -> ArtifactSummary:
    """Summarize an already-parsed artifact.

    Refuses rather than guesses. Every rejection here names what was wrong and
    what was expected, because the person holding a mystery JSON file is trying
    to find out whether it is a run artifact at all.
    """
    if not isinstance(data, dict):
        raise ArtifactError(
            f"a run artifact must be a JSON object, got {type(data).__name__}"
        )

    tool = detect_tool(data)
    if tool is None:
        raise ArtifactError(
            "could not tell which tool wrote this artifact. Nothing in it "
            f"matches a known shape; top-level keys were {sorted(data)[:8]}. "
            f"Known tools are {list(KNOWN_TOOLS)}."
        )

    reader = _READERS.get(tool)
    if reader is None:
        raise ArtifactError(
            f"this artifact says it was written by {tool!r}, which this "
            f"command has no reader for. Known tools are {list(KNOWN_TOOLS)}. "
            "Add a reader rather than letting it fall through to a guess."
        )

    return reader(data)


def load(path: Path) -> ArtifactSummary:
    """Read and summarize an artifact file.

    A file that is not there, or is not JSON, or is JSON but not a run artifact,
    all refuse. The first two are obvious; the third is the one worth being
    careful about, because a config file is valid JSON and would otherwise be
    summarized as a run that found nothing.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ArtifactError(f"no such artifact: {path}") from None
    except OSError as exc:
        raise ArtifactError(f"could not read {path}: {exc}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{path} is not valid JSON: {exc.msg} at line {exc.lineno} "
            f"column {exc.colno}. A run artifact is written by one of "
            f"{list(KNOWN_TOOLS)}; if this is a config file, it is not an artifact."
        ) from None

    return summarize(data)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(summary: ArtifactSummary) -> str:
    """A readable summary. Plain text, one line per fact, no colour.

    Deliberately boring: this is what a person reads in a CI log and what a
    terminal shows with no width. The machine-readable form is `--json`.
    """
    lines = [
        f"tool:    {summary.tool}",
        f"verdict: {summary.verdict}"
        + ("" if summary.verdict_source == "recorded" else " (derived)"),
    ]

    if summary.seed is not None:
        lines.append(f"seed:    {summary.seed}")
    if summary.target:
        lines.append(f"target:  {summary.target}")
    if summary.dry_run:
        lines.append("dry run: yes, nothing was sent")

    counts = {
        k: v for k, v in summary.counts.__dict__.items() if v
    }
    if counts:
        lines.append("")
        lines.append("counts:")
        lines.extend(f"  {name:<11} {value}" for name, value in counts.items())

    lines.append("")
    lines.append(f"findings: {len(summary.findings)}")
    for finding in summary.findings:
        lines.append(f"  [{finding.kind}] {finding.detail}")

    lines.append("")
    lines.append(
        f"exit code: {summary.exit_code} ({summary.exit_code_source}, not "
        "recorded in the artifact)"
    )
    return "\n".join(lines)
