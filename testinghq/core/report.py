"""Blast run artifacts: the grading rules, the summary, and the artifact.

Different categories have different definitions of success:

- Any payload that gets a 5xx, or no response at all (refused, DNS, timeout,
  TLS), is flagged, whatever its category. The endpoint crashed or was not
  there.
- A clean payload that does not get a 2xx is flagged: the parser broke on
  well-formed input.
- A 4xx on a malformed or degenerate payload is recorded, not flagged: the
  parser rejected garbage cleanly.
- A 2xx whose body matches the run's `--fail-on-body` pattern is flagged: the
  endpoint said yes in the status and no in the body.

Pure functions only, no I/O, no wall-clock reads, no network. The same
records, seed and config always yield the same artifact dict.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Protocol, runtime_checkable

#: Artifact schema this module writes. Readers refuse a higher one.
SCHEMA_VERSION = 1

#: The top-level `tool` key of a blast artifact.
TOOL = "blast"

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

# blast/corrupt.py's recipe names use underscores; the artifact's category
# labels use hyphens. This is the one place that translates between them.
CORRUPT_CATEGORY_LABELS: Dict[str, str] = {
    "clean": CLEAN,
    "messy_but_valid": MESSY_BUT_VALID,
    "multilingual_gibberish": MULTILINGUAL_GIBBERISH,
    "structurally_malformed": STRUCTURALLY_MALFORMED,
    "degenerate": DEGENERATE,
}

# Outcomes returned by classify_record(). Every one but OK and
# ASSERTION_FAILED raises a flag.
CLEAN_FAILED = "clean_failed"
DEGENERATE_FAILED = "degenerate_failed"
SERVER_ERROR = "server_error"
NO_RESPONSE = "no_response"
BODY_FAILED = "body_failed"
ASSERTION_FAILED = "assertion_failed"
OK = "ok"

FLAGGED_OUTCOMES = (CLEAN_FAILED, DEGENERATE_FAILED, SERVER_ERROR, NO_RESPONSE, BODY_FAILED)

#: Response classes in the summary, in print order. `no_response` is every
#: record whose request got no HTTP response at all.
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", "other", "no_response")

#: Why a request got no response, in print order.
TRANSPORT_ERROR_KINDS = ("refused", "dns", "timeout", "tls", "other")

_ERROR_REASONS = {
    "refused": "connection refused",
    "dns": "DNS lookup failed",
    "tls": "TLS handshake failed",
}

_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "name resolution",
    "no address associated",
    "[errno -2]",
    "[errno -3]",
    "[errno -5]",
    "[errno 11001]",
    "[errno 8]",
)


def category_label(internal_name: str) -> str:
    """Map a blast/corrupt.py recipe name (underscored) to the schema's
    hyphenated category label. Passes through unrecognized names unchanged,
    so an already-hyphenated label is a no-op."""
    return CORRUPT_CATEGORY_LABELS.get(internal_name, internal_name)


def transport_error_kind(error: Optional[str]) -> str:
    """One of TRANSPORT_ERROR_KINDS for a transport error message."""
    text = (error or "").lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "refused" in text:
        return "refused"
    if any(marker in text for marker in _DNS_MARKERS):
        return "dns"
    if "ssl" in text or "tls" in text or "certificate" in text:
        return "tls"
    return "other"


def _error_kind(response: Dict[str, Any]) -> str:
    """The transport error kind of a response with no status. A record from
    before reasons were recorded carries none and counts as a timeout, which
    is what it was reported as."""
    kind = response.get("error_kind")
    if kind in TRANSPORT_ERROR_KINDS:
        return kind
    if response.get("error"):
        return transport_error_kind(response["error"])
    return "timeout"


def _no_response_reason(response: Dict[str, Any]) -> str:
    kind = _error_kind(response)
    if kind == "timeout":
        return "timed out"
    reason = _ERROR_REASONS.get(kind) or str(response.get("error") or "no response")
    return f"got no response: {reason}"


def status_class(status: Optional[int]) -> str:
    """The STATUS_CLASSES entry for one status (None is `no_response`)."""
    if status is None:
        return "no_response"
    if 100 <= status < 600:
        return f"{status // 100}xx"
    return "other"


def _is_2xx(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 300


def _is_5xx(status: Optional[int]) -> bool:
    return status is not None and 500 <= status < 600


def classify_record(record: Dict[str, Any]) -> str:
    """Return the record's outcome: one of FLAGGED_OUTCOMES, ASSERTION_FAILED
    or OK.

    `record` is a dict matching the run artifact schema's record shape (must
    have "category" and "response" with a "status" key at minimum).
    """
    category = record.get("category")
    response = record.get("response") or {}
    status = response.get("status")

    if category == CLEAN and not _is_2xx(status):
        return CLEAN_FAILED
    if status is None or _is_5xx(status):
        if category == DEGENERATE:
            return DEGENERATE_FAILED
        return NO_RESPONSE if status is None else SERVER_ERROR
    if _is_2xx(status) and response.get("body_failure") is True:
        return BODY_FAILED

    assertion = record.get("assertion") or {}
    if assertion.get("passed") is False:
        return ASSERTION_FAILED

    return OK


def flag_for_record(record: Dict[str, Any]) -> Optional[str]:
    """Return the human-readable flag string for a flagged record, or None.

    Shapes: "clean payload <id> did not 2xx", "<category> <id> returned
    <status>", "<category> <id> timed out", "<category> <id> got no response:
    connection refused", and "<category> <id> returned <status> with a failure
    body".
    """
    outcome = classify_record(record)
    if outcome not in FLAGGED_OUTCOMES:
        return None
    rid = record.get("id")
    category = record.get("category")
    response = record.get("response") or {}
    status = response.get("status")

    if outcome == CLEAN_FAILED:
        if status is None:
            return f"clean payload {rid} did not 2xx: {_no_response_reason(response)}"
        return f"clean payload {rid} did not 2xx"
    if outcome == BODY_FAILED:
        return f"{category} {rid} returned {status} with a failure body"
    if status is None:
        return f"{category} {rid} {_no_response_reason(response)}"
    return f"{category} {rid} returned {status}"


def empty_status_classes() -> Dict[str, int]:
    return {name: 0 for name in STATUS_CLASSES}


def compute_summary(
    records: List[Dict[str, Any]], seed: int, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the "summary" block of a run artifact from a list of records.

    `seed` and `config` are accepted for signature symmetry and not
    consulted: the summary is derived from the records alone.
    """
    by_status_class = empty_status_classes()
    transport_errors = {kind: 0 for kind in TRANSPORT_ERROR_KINDS}
    by_category = {c: 0 for c in CATEGORIES}
    flags: List[str] = []

    for record in records:
        response = record.get("response") or {}
        status = response.get("status")
        by_status_class[status_class(status)] += 1
        if status is None:
            transport_errors[_error_kind(response)] += 1

        category = record.get("category")
        if category in by_category:
            by_category[category] += 1

        flag = flag_for_record(record)
        if flag:
            flags.append(flag)

    return {
        "by_status_class": by_status_class,
        "transport_errors": transport_errors,
        "by_category": by_category,
        "flags": flags,
    }


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    records: List[Dict[str, Any]],
    *,
    aborted: bool = False,
) -> Dict[str, Any]:
    """Assemble the full run artifact: schema_version, tool, seed, config,
    summary, records, aborted.

    `records` must already be in the schema's record shape (see
    build_record); they are not mutated or reordered. The verdict is FLAGGED
    when the summary has flags and CLEAN otherwise; an aborted run is
    ABORTED. Pure: no I/O.
    """
    summary = compute_summary(records, seed, config)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "seed": seed,
        "config": dict(config),
        "summary": summary,
        "records": list(records),
        "aborted": aborted,
    }


def build_dry_run_artifact(
    seed: int, config: Dict[str, Any], records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """The artifact for a run that deliberately sent nothing: no response
    classes, no flags, the real category tally."""
    by_category = {c: 0 for c in CATEGORIES}
    for record in records:
        if record.get("category") in by_category:
            by_category[record["category"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "seed": seed,
        "config": dict(config),
        "summary": {
            "by_status_class": empty_status_classes(),
            "transport_errors": {kind: 0 for kind in TRANSPORT_ERROR_KINDS},
            "by_category": by_category,
            "flags": [],
        },
        "records": list(records),
        "aborted": False,
    }


def verdict(artifact: Dict[str, Any]) -> str:
    """CLEAN, FLAGGED or ABORTED for a built artifact."""
    if artifact.get("aborted") is True:
        return "ABORTED"
    return "FLAGGED" if (artifact.get("summary") or {}).get("flags") else "CLEAN"


# ---------------------------------------------------------------------------
# Human summary: counts by response class, counts by category, and the
# "look here" list of flags.
# ---------------------------------------------------------------------------

#: Response classes always printed; the others print only when non-zero.
_ALWAYS_PRINTED = ("2xx", "4xx", "5xx", "no_response")


def format_summary(artifact: Dict[str, Any]) -> str:
    """Render an artifact's summary as human-readable text.

    Pure formatting, no I/O. Deterministic: category order follows
    CATEGORIES, response classes follow STATUS_CLASSES, flags follow record
    order.
    """
    seed = artifact.get("seed")
    summary = artifact.get("summary") or {}
    by_status_class = summary.get("by_status_class") or {}
    by_category = summary.get("by_category") or {}
    transport_errors = summary.get("transport_errors") or {}
    flags = summary.get("flags") or []
    total = len(artifact.get("records") or [])

    lines: List[str] = []
    lines.append(f"Blast run seed={seed}, {total} payload(s)")
    lines.append("")
    lines.append("By response class:")
    for name in STATUS_CLASSES:
        count = by_status_class.get(name, 0)
        if name not in _ALWAYS_PRINTED and not count:
            continue
        label = "no response" if name == "no_response" else name
        lines.append(f"  {label}: {count}")
        if name == "no_response" and count:
            breakdown = ", ".join(
                f"{kind} {transport_errors.get(kind, 0)}" for kind in TRANSPORT_ERROR_KINDS
            )
            lines.append(f"    ({breakdown})")
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
    if artifact.get("aborted") is True:
        lines.append("")
        lines.append("Interrupted: the counts cover only the payloads sent before it stopped.")
    lines.append("")
    lines.append(f"Verdict: {verdict(artifact)}")

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
    """The default Matcher. Ignores `record["intended"]` and `readback` and
    grades only the transport: any real non-5xx response passes, a 5xx or no
    response fails.
    """

    def match(self, record: Dict[str, Any], response: Dict[str, Any], readback: Any = None) -> MatchResult:
        response = response or {}
        status = response.get("status")
        if status is None:
            reason = _no_response_reason(response)
            mismatch = "response timed out" if reason == "timed out" else f"response {reason}"
            return MatchResult(passed=False, mismatches=[mismatch])
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
            hasher.update(serialize_module.part_value_bytes(part))
        else:
            hasher.update(part.filename.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(part.content_type.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(part.content)
        hasher.update(b"\x01")
    return hasher.hexdigest()


def response_from_transport(result, fail_on_body: Optional[Pattern[str]] = None) -> Dict[str, Any]:
    """The record's `response` block for a transport.TransportResult.

    `error` is the transport's message and `error_kind` one of
    TRANSPORT_ERROR_KINDS, both None when a response arrived. With
    `fail_on_body`, `body_failure` says whether a 2xx body snippet matched it.
    """
    error = getattr(result, "error", None) if result.status is None else None
    response: Dict[str, Any] = {
        "status": result.status,
        "latency_ms": result.latency_ms,
        "body_snippet": result.body_snippet,
        "error": error,
        "error_kind": transport_error_kind(error) if result.status is None else None,
    }
    if fail_on_body is not None:
        response["body_failure"] = bool(
            _is_2xx(result.status) and fail_on_body.search(result.body_snippet or "")
        )
    return response


def compile_body_pattern(pattern: Optional[str]) -> Optional[Pattern[str]]:
    """Compile a `--fail-on-body` pattern; raises ValueError naming the
    problem when it is not a valid regular expression."""
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"--fail-on-body is not a valid regular expression: {exc}") from None


def build_record(
    email,
    category: str,
    seed: int,
    index: int,
    response: Dict[str, Any],
    matcher: Optional[Matcher] = None,
    readback: Any = None,
    case: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one schema-shaped record from a generated email, its category,
    and the response an endpoint gave back for it.

    `category` is the schema's hyphenated label (see category_label()). `id`
    is "{category}-{seed}-{index:04d}". `case` names the catalog case the
    payload came from, recorded as "case" when given.

    The record's "assertion" is `matcher.match(...)` (default
    StatusOnlyMatcher), except that a flagged outcome (see classify_record)
    replaces it with that flag as the only mismatch.
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
    if case is not None:
        record["case"] = case

    match_result = active_matcher.match(record, record["response"], readback)
    record["assertion"] = {
        "passed": match_result.passed,
        "mismatches": list(match_result.mismatches),
    }

    if classify_record(record) in FLAGGED_OUTCOMES:
        flag = flag_for_record(record)
        record["assertion"] = {"passed": False, "mismatches": [flag] if flag else []}

    return record
