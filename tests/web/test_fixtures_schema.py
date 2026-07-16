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


def test_failures_fixture_contains_both_highlight_classes():
    artifact = _load("sample_run_with_failures.json")
    outcomes = {expectations.classify_record(r) for r in artifact["records"]}
    assert expectations.CLEAN_FAILED in outcomes
    assert expectations.DEGENERATE_FAILED in outcomes
