"""Expectation-based classification, delegated to the canonical engine.

There is exactly one implementation of these rules:
`testinghq.core.report`. This module re-exports it and adds nothing. Anything
importing from here gets the engine's functions, not a copy.

WHY THIS IS A DELEGATION, since it was not always one. This file used to carry
a second, hand-maintained copy of `classify_record`, `flag_for_record` and
`compute_summary`, written when `core/report.py` did not exist yet. It is the
same mistake the web lane already made once with the guardrails, and it ended
the same way: the security lane hardened the canonical `core/guardrails.py`
with an `allow_public_hosts` escape hatch and a reserved-host allow-list, and
within hours a target the CLI refused was one the UI would have fired at,
because this lane's copy did not inherit any of it.

The web lane has now copied the same rules three times over: once in this
file, and once more in `web/static/app.js` for the browser. The JavaScript
copy is gone as of the change that produced this docstring. What remains is
this delegation.

If you are reading this because a rule needs to change: change it in
`testinghq/core/report.py` and nowhere else. Do not add a rule here. Do not
add one to `web/static/app.js` either; the server annotates each record with
its outcome from the engine, and the browser reads that.

`tests/test_repo_invariants.py` enforces the structural half of this: a re-inline
of a rule here fails the build.
"""
from __future__ import annotations

from testinghq.core.report import (
    ASSERTION_FAILED,
    CATEGORIES,
    CLEAN,
    CLEAN_FAILED,
    CORRUPT_CATEGORY_LABELS,
    DEGENERATE,
    DEGENERATE_FAILED,
    MESSY_BUT_VALID,
    MULTILINGUAL_GIBBERISH,
    OK,
    STRUCTURALLY_MALFORMED,
    category_label,
    classify_record,
    compute_summary,
    flag_for_record,
)

__all__ = [
    "ASSERTION_FAILED",
    "CATEGORIES",
    "CLEAN",
    "CLEAN_FAILED",
    "CORRUPT_CATEGORY_LABELS",
    "DEGENERATE",
    "DEGENERATE_FAILED",
    "MESSY_BUT_VALID",
    "MULTILINGUAL_GIBBERISH",
    "OK",
    "STRUCTURALLY_MALFORMED",
    "category_label",
    "classify_record",
    "compute_summary",
    "flag_for_record",
]
