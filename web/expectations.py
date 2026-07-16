"""Expectation-based reading of a run artifact.

This is the product insight the whole tool exists to surface: the results
panel is not a status-code dump. Different categories have different
definitions of success.

- A degenerate input returning a clean 4xx (or any non-500, non-hanging
  response) is a PASS - the parser correctly rejected garbage.
- A degenerate input returning a 500, or hanging (timeout, null status), is a
  FAIL - the parser choked instead of rejecting cleanly.
- A clean input that does NOT return 2xx is a FAIL - the parser broke on
  well-formed input.
- Everything else defaults to "ok" (informational, not a highlighted failure
  class) unless the record's own assertion says it mismatched.

Pure functions only, no I/O, so this module is trivial to unit test directly
against the two failure classes the product cares about.
"""
from __future__ import annotations

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

# Highlight classes returned by classify_record().
CLEAN_FAILED = "clean_failed"
DEGENERATE_FAILED = "degenerate_failed"
ASSERTION_FAILED = "assertion_failed"
OK = "ok"


def _is_2xx(status):
    return status is not None and 200 <= status < 300


def _is_5xx(status):
    return status is not None and 500 <= status < 600


def _is_timeout(status):
    return status is None


def classify_record(record):
    """Return one of CLEAN_FAILED, DEGENERATE_FAILED, ASSERTION_FAILED, OK.

    `record` is a dict matching the RUN ARTIFACT SCHEMA record shape (must
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


def flag_for_record(record):
    """Return the human-readable flag string for a failing record, or None.

    Mirrors the schema's example flag strings: "clean payload <id> did not
    2xx" and "degenerate <id> returned 500".
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


def compute_summary(records, seed, config):
    """Build the "summary" block of a run artifact from a list of records."""
    by_status_class = {"2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
    by_category = {c: 0 for c in CATEGORIES}
    flags = []

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
