"""Validate the sample fixtures under web/tests/fixtures/ against the
documented RUN ARTIFACT SCHEMA, and cross-check the "failures" fixture
against web/expectations.py so the two stay in agreement.

These fixtures are the contract artifact this lane builds and tests
against while the engine lane is being built in parallel; if they drift
from the schema, that is caught here rather than downstream in the UI.
"""
import json
from pathlib import Path

import pytest

from web import expectations

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "tests" / "fixtures"

TOP_LEVEL_KEYS = {"seed", "config", "summary", "records"}
RECORD_KEYS = {"id", "category", "payload_sha256", "intended", "response", "assertion"}
INTENDED_KEYS = {"from", "subject", "body_core", "attachments"}
RESPONSE_KEYS = {"status", "latency_ms", "body_snippet"}
ASSERTION_KEYS = {"passed", "mismatches"}
SUMMARY_KEYS = {"by_status_class", "by_category", "flags"}


def _load(name):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "filename", ["sample_run_clean.json", "sample_run_with_failures.json"]
)
def test_fixture_matches_schema_shape(filename):
    artifact = _load(filename)
    assert set(artifact.keys()) == TOP_LEVEL_KEYS
    assert isinstance(artifact["seed"], int)
    assert set(artifact["summary"].keys()) == SUMMARY_KEYS

    for record in artifact["records"]:
        assert set(record.keys()) == RECORD_KEYS
        assert record["category"] in expectations.CATEGORIES
        assert set(record["intended"].keys()) == INTENDED_KEYS
        assert set(record["response"].keys()) == RESPONSE_KEYS
        assert set(record["assertion"].keys()) == ASSERTION_KEYS


def test_fixture_records_use_reserved_domains_only():
    for filename in ("sample_run_clean.json", "sample_run_with_failures.json"):
        artifact = _load(filename)
        for record in artifact["records"]:
            sender = record["intended"]["from"]
            domain = sender.split("@", 1)[1]
            assert domain.endswith(
                (
                    "example.com",
                    "example.net",
                    "example.org",
                    "example.edu",
                    ".test",
                    ".invalid",
                    ".example",
                    ".localhost",
                )
            ), f"non-reserved domain in fixture: {sender}"


def test_clean_fixture_has_no_flags():
    artifact = _load("sample_run_clean.json")
    assert artifact["summary"]["flags"] == []
    for record in artifact["records"]:
        assert expectations.classify_record(record) in (
            expectations.OK,
        )


def test_failures_fixture_flags_agree_with_expectations_module():
    artifact = _load("sample_run_with_failures.json")
    recomputed_flags = []
    for record in artifact["records"]:
        flag = expectations.flag_for_record(record)
        if flag:
            recomputed_flags.append(flag)
    assert recomputed_flags == artifact["summary"]["flags"]


# ---------------------------------------------------------------------------
# The summary counts are checked against an INDEPENDENT recount, not against
# expectations.compute_summary().
#
# sample_run_with_failures.json shipped with by_status_class saying 5xx: 1
# when two of its six records carried a 500. Nothing caught it, because the
# tests that existed checked the flags and the schema shape but never the
# counts, and the engine lane's own test asserted the corrected number
# against a hardcoded literal rather than against the file. A fixture is
# data other people read and the UI demos from, so a hand-edited one that
# disagrees with its own records is a defect, not a cosmetic slip.
#
# Recounting here rather than calling compute_summary() is deliberate: that
# function is one of the two implementations under suspicion. Agreeing with
# it proves the file matches a rule set, not that the rule set matches the
# records. This counts the records by hand.
# ---------------------------------------------------------------------------


def _recount_status_classes(records):
    counts = {"2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0}
    for record in records:
        status = (record.get("response") or {}).get("status")
        if status is None:
            counts["timeout"] += 1
        elif 200 <= status < 300:
            counts["2xx"] += 1
        elif 400 <= status < 500:
            counts["4xx"] += 1
        elif 500 <= status < 600:
            counts["5xx"] += 1
        else:
            raise AssertionError(
                f"record {record.get('id')!r} has status {status!r}, which "
                "falls in no response class the schema defines"
            )
    return counts


def _recount_categories(records):
    counts = {category: 0 for category in expectations.CATEGORIES}
    for record in records:
        counts[record["category"]] += 1
    return counts


@pytest.mark.parametrize(
    "filename", ["sample_run_clean.json", "sample_run_with_failures.json"]
)
def test_declared_summary_counts_match_an_independent_recount(filename):
    artifact = _load(filename)
    records = artifact["records"]
    declared = artifact["summary"]

    assert declared["by_status_class"] == _recount_status_classes(records), (
        f"{filename}: declared by_status_class disagrees with the records it "
        "contains. A hand-edited fixture that lies about its own records must "
        "not be able to pass the suite again."
    )
    assert declared["by_category"] == _recount_categories(records), (
        f"{filename}: declared by_category disagrees with its records"
    )


@pytest.mark.parametrize(
    "filename", ["sample_run_clean.json", "sample_run_with_failures.json"]
)
def test_every_record_is_counted_exactly_once(filename):
    """Closes the gap a recount alone would leave: a status outside every
    defined class, or a record silently dropped, would make the counts wrong
    in a way that could still balance. The classes must partition the run."""
    artifact = _load(filename)
    counts = _recount_status_classes(artifact["records"])
    assert sum(counts.values()) == len(artifact["records"])
    assert sum(_recount_categories(artifact["records"]).values()) == len(
        artifact["records"]
    )


@pytest.mark.parametrize(
    "filename", ["sample_run_clean.json", "sample_run_with_failures.json"]
)
def test_declared_summary_equals_what_the_module_computes(filename):
    """And the declared summary must also be what web/expectations.py would
    produce, so the fixtures stay usable as the cross-check corpus the engine
    lane relies on in tests/unit/test_report.py."""
    artifact = _load(filename)
    assert expectations.compute_summary(
        artifact["records"], artifact["seed"], artifact["config"]
    ) == artifact["summary"]



def test_failures_fixture_contains_both_highlight_classes():
    artifact = _load("sample_run_with_failures.json")
    outcomes = {expectations.classify_record(r) for r in artifact["records"]}
    assert expectations.CLEAN_FAILED in outcomes
    assert expectations.DEGENERATE_FAILED in outcomes
