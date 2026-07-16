"""Run artifact building: reporting, expectation rules, and the assertion hook.

This is what turns Blast from a payload cannon into a bug finder. A run
artifact is not a status-code dump; different categories have different
definitions of success:

- A degenerate input returning a clean 4xx (or any non-5xx, non-hanging
  response) is a PASS: the parser correctly rejected garbage.
- A degenerate input returning a 5xx, or hanging (timeout, null status), is a
  FAIL: the parser choked instead of rejecting cleanly.
- A clean input that does NOT return 2xx is a FAIL: the parser broke on
  well-formed input.
- Everything else defaults to "ok" (informational) unless the record's own
  assertion says it mismatched.

This module owns the same rules as web/expectations.py, on purpose: the two
lanes each maintain their own copy so the web lane's fixtures and this
lane's artifact builder can be independently cross-checked rather than one
silently trusting the other. tests/unit/test_report.py cross-checks every
record in web/tests/fixtures/*.json against both copies and fails loud on
any disagreement.

Pure functions only, no I/O, no wall-clock reads, no network. Determinism is
load-bearing: the same records, seed, and config always yield the same
artifact dict.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

CLEAN = "clean"
MESSY_BUT_VALID = "messy-but-valid"
MULTILINGUAL_GIBBERISH = "multilingual-gibberish"
STRUCTURALLY_MALFORMED = "structurally-malformed"
DEGENERATE = "degenerate"

CATEGORIES = (
    CLEAN,
    MESSY_BUT_VALID,
    MULTILINGUAL_GIBBERISH,
    STRUCTURALLY_MALFORMED,
    DEGENERATE,
)

# blast/corrupt.py's RECIPES keys use underscores (its own internal naming);
# the artifact schema's category label uses hyphens (matching web/
# expectations.py and the shipped fixtures). This is the one place that
# translates between the two so a caller building records from
# corrupt_corpus() output gets schema-correct category labels.
CORRUPT_CATEGORY_LABELS: Dict[str, str] = {
    "clean": CLEAN,
    "messy_but_valid": MESSY_BUT_VALID,
    "multilingual_gibberish": MULTILINGUAL_GIBBERISH,
    "structurally_malformed": STRUCTURALLY_MALFORMED,
    "degenerate": DEGENERATE,
}

# Highlight classes returned by classify_record().
CLEAN_FAILED = "clean_failed"
DEGENERATE_FAILED = "degenerate_failed"
ASSERTION_FAILED = "assertion_failed"
OK = "ok"


def category_label(internal_name: str) -> str:
    """Map a blast/corrupt.py recipe name (underscored) to the schema's
    hyphenated category label. Passes through unrecognized names unchanged,
    so an already-hyphenated label is a no-op."""
    return CORRUPT_CATEGORY_LABELS.get(internal_name, internal_name)


def _is_2xx(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 300


def _is_5xx(status: Optional[int]) -> bool:
    return status is not None and 500 <= status < 600


def _is_timeout(status: Optional[int]) -> bool:
    return status is None


def classify_record(record: Dict[str, Any]) -> str:
    """Return one of CLEAN_FAILED, DEGENERATE_FAILED, ASSERTION_FAILED, OK.

    `record` is a dict matching the run artifact schema's record shape (must
    have "category" and "response" with a "status" key at minimum).
    """
    category = record.get("category")
    status = (record.get("response") or {}).get("status")

    if category == CLEAN and not _is_2xx(status):
        return CLEAN_FAILED

    if category == DEGENERATE and (_is_5xx(status) or _is_timeout(status)):
        return DEGENERATE_FAILED

    assertion = record.get("assertion") or {}
    if assertion.get("passed") is False:
        return ASSERTION_FAILED

    return OK


def flag_for_record(record: Dict[str, Any]) -> Optional[str]:
    """Return the human-readable flag string for a failing record, or None.

    Mirrors the schema's example flag strings: "clean payload <id> did not
    2xx", "degenerate <id> returned <status>", and "degenerate <id> timed
    out".
    """
    outcome = classify_record(record)
    rid = record.get("id")
    status = (record.get("response") or {}).get("status")

    if outcome == CLEAN_FAILED:
        return f"clean payload {rid} did not 2xx"
    if outcome == DEGENERATE_FAILED:
        if _is_timeout(status):
            return f"degenerate {rid} timed out"
        return f"degenerate {rid} returned {status}"
    return None


def compute_summary(
    records: List[Dict[str, Any]], seed: int, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the "summary" block of a run artifact from a list of records.

    `seed` and `config` are accepted for signature symmetry with the rest of
    the artifact builder (and because a future summary field may want them)
    but are not currently consulted: the summary is derived entirely from
    the records themselves, so a run where everything 500s and a run where
    everything 200s produce visibly different summaries from the records
    alone.
    """
    by_status_class = {"2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
    by_category = {c: 0 for c in CATEGORIES}
    flags: List[str] = []

    for record in records:
        status = (record.get("response") or {}).get("status")
        if _is_timeout(status):
            by_status_class["timeout"] += 1
        elif _is_2xx(status):
            by_status_class["2xx"] += 1
        elif status is not None and 400 <= status < 500:
            by_status_class["4xx"] += 1
        elif _is_5xx(status):
            by_status_class["5xx"] += 1

        category = record.get("category")
        if category in by_category:
            by_category[category] += 1

        flag = flag_for_record(record)
        if flag:
            flags.append(flag)

    return {
        "by_status_class": by_status_class,
        "by_category": by_category,
        "flags": flags,
    }


def build_artifact(
    seed: int, config: Dict[str, Any], records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Assemble the full run artifact: seed, config, summary, records.

    `records` must already be in the schema's record shape (see the module
    docstring and the RUN ARTIFACT SCHEMA in the engine brief); this
    function does not mutate or reorder them, it only computes the summary
    over them and wraps everything into the top-level artifact dict.
    """
    return {
        "seed": seed,
        "config": dict(config),
        "summary": compute_summary(records, seed, config),
        "records": list(records),
    }


# ---------------------------------------------------------------------------
# Human summary: the CLI-facing report. Counts by response class, counts by
# category, and the category-versus-outcome "look here" list, so a run where
# everything 500s and a run where everything 200s read as visibly different
# reports, not just different numbers buried in JSON.
# ---------------------------------------------------------------------------

_STATUS_CLASS_ORDER = ("2xx", "4xx", "5xx", "timeout")


def format_summary(artifact: Dict[str, Any]) -> str:
    """Render an artifact's summary as human-readable text.

    Pure formatting, no I/O: the caller decides whether to print it, write
    it to a file, or both. Deterministic given a deterministic artifact
    (category order follows CATEGORIES, status class order follows
    _STATUS_CLASS_ORDER, flags follow record order, so output is stable
    across runs of the same seed and config).
    """
    seed = artifact.get("seed")
    summary = artifact.get("summary") or {}
    by_status_class = summary.get("by_status_class") or {}
    by_category = summary.get("by_category") or {}
    flags = summary.get("flags") or []
    total = len(artifact.get("records") or [])

    lines: List[str] = []
    lines.append(f"Blast run seed={seed}, {total} payload(s)")
    lines.append("")
    lines.append("By response class:")
    for status_class in _STATUS_CLASS_ORDER:
        lines.append(f"  {status_class}: {by_status_class.get(status_class, 0)}")
    lines.append("")
    lines.append("By category:")
    for category in CATEGORIES:
        lines.append(f"  {category}: {by_category.get(category, 0)}")
    lines.append("")
    if flags:
        lines.append(f"Look here ({len(flags)}):")
        for flag in flags:
            lines.append(f"  - {flag}")
    else:
        lines.append("Look here: nothing flagged.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The assertion hook: a generic Matcher a caller can use to compare intended
# ground truth against what a parser actually extracted. Nothing here knows
# about any particular business domain; a Matcher only ever sees the three
# generic inputs below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """The outcome of comparing a record's intended ground truth against
    what actually came back. `mismatches` is a list of short human-readable
    strings, empty when `passed` is True."""

    passed: bool
    mismatches: List[str] = field(default_factory=list)


@runtime_checkable
class Matcher(Protocol):
    """Interface a caller implements to grade a record.

    `record` is a dict with at least "id", "category", and "intended" (the
    ground-truth fields: from, subject, body_core, attachments). `response`
    is a dict with "status", "latency_ms", "body_snippet". `readback` is
    whatever the caller has that represents what the endpoint under test
    actually parsed out of the payload; its shape is entirely up to the
    caller (this hook does not require or interpret any particular shape),
    which is what keeps this module generic: nothing business-specific ever
    lives here.
    """

    def match(self, record: Dict[str, Any], response: Dict[str, Any], readback: Any) -> MatchResult:
        ...


class StatusOnlyMatcher:
    """The default Matcher. Ignores `record["intended"]` and `readback`
    entirely (there may be no readback wired up at all yet, e.g. in a dry
    run) and grades purely on whether the transport itself succeeded: a
    real, non-5xx response counts as a pass, a 5xx or a timeout (null
    status) counts as a fail. This is deliberately the same "did the
    endpoint respond without crashing or hanging" signal used elsewhere in
    this module, so a caller with no readback wiring yet does not get
    spurious per-record assertion failures layered on top of the
    category-based flags build_record already applies.
    """

    def match(self, record: Dict[str, Any], response: Dict[str, Any], readback: Any = None) -> MatchResult:
        status = (response or {}).get("status")
        if _is_timeout(status):
            return MatchResult(passed=False, mismatches=["response timed out"])
        if _is_5xx(status):
            return MatchResult(passed=False, mismatches=[f"response returned {status}"])
        return MatchResult(passed=True, mismatches=[])


DEFAULT_MATCHER = StatusOnlyMatcher()


def payload_sha256(email) -> str:
    """A deterministic sha256 of an InboundEmail's serialized wire form.

    Built from blast/serialize.py's ordered multipart parts, so the same
    email always hashes to the same digest, and any change to the payload's
    actual on-the-wire content (not just its Python repr) changes the hash.
    Imports blast.serialize lazily to avoid a hard import-order dependency
    for callers of report.py that never touch payload hashing (e.g. a
    caller replaying an artifact that already has payload_sha256 values
    baked in).
    """
    from ..blast import serialize as serialize_module

    parts = serialize_module.to_multipart_parts(email)
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.name.encode("utf-8"))
        hasher.update(b"\x00")
        if hasattr(part, "value"):
            hasher.update(part.value.encode("utf-8"))
        else:
            hasher.update(part.filename.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(part.content_type.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(part.content)
        hasher.update(b"\x01")
    return hasher.hexdigest()


def build_record(
    email,
    category: str,
    seed: int,
    index: int,
    response: Dict[str, Any],
    matcher: Optional[Matcher] = None,
    readback: Any = None,
) -> Dict[str, Any]:
    """Build one schema-shaped record from a generated email, its category,
    and the response an endpoint gave back for it.

    `category` should already be the schema's hyphenated label (see
    category_label() to translate from blast/corrupt.py's internal
    underscored recipe names). `id` follows the shipped fixtures' pattern:
    "{category}-{seed}-{index:04d}".

    The record's "assertion" is the combination of two independent checks:
    1. `matcher.match(...)` (default StatusOnlyMatcher) grades transport
       success and/or ground-truth-vs-readback correctness, generically.
    2. The category-specific expectation rules (clean must 2xx, degenerate
       must not 5xx/timeout) computed via classify_record/flag_for_record.

    When rule 2 fires (CLEAN_FAILED or DEGENERATE_FAILED), it overrides the
    matcher's verdict: the record fails with exactly that rule's flag text
    as its sole mismatch, matching the shipped fixtures byte for byte.
    Otherwise the matcher's own verdict stands. This keeps category rules
    and content matching as two independently swappable layers: replacing
    the matcher never changes how clean/degenerate status expectations are
    enforced, and vice versa.
    """
    active_matcher = matcher if matcher is not None else DEFAULT_MATCHER

    record: Dict[str, Any] = {
        "id": f"{category}-{seed}-{index:04d}",
        "category": category,
        "payload_sha256": payload_sha256(email),
        "intended": {
            "from": email.ground_truth.from_addr,
            "subject": email.ground_truth.subject,
            "body_core": email.ground_truth.body_core,
            "attachments": len(email.attachments),
        },
        "response": dict(response),
        "assertion": {"passed": True, "mismatches": []},
    }

    match_result = active_matcher.match(record, record["response"], readback)
    record["assertion"] = {
        "passed": match_result.passed,
        "mismatches": list(match_result.mismatches),
    }

    outcome = classify_record(record)
    if outcome in (CLEAN_FAILED, DEGENERATE_FAILED):
        flag = flag_for_record(record)
        record["assertion"] = {"passed": False, "mismatches": [flag] if flag else []}

    return record
