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

from typing import Any, Dict, List, Optional

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
