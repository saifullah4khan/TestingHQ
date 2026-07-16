"""Unit tests for testinghq/core/report.py: expectation rules, summary, and
the artifact builder. Also cross-checks this lane's rules against
web/expectations.py on both shipped fixtures, per the engine brief: any
disagreement between the two independently-maintained rule copies is a
defect that must be reported, not silently reconciled.
"""
from __future__ import annotations

import json
from pathlib import Path

from testinghq.core import report

FIXTURES_DIR = (
    Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures"
)


def _load_fixture(name):
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def _record(category, status, rid="r-0"):
    return {
        "id": rid,
        "category": category,
        "payload_sha256": "0" * 64,
        "intended": {"from": "a@example.com", "subject": "s", "body_core": "b", "attachments": 0},
        "response": {"status": status, "latency_ms": 1.0, "body_snippet": ""},
        "assertion": {"passed": True, "mismatches": []},
    }


# ---------------------------------------------------------------------------
# The three product rules, stated directly. Do not soften these.
# ---------------------------------------------------------------------------


def test_degenerate_4xx_is_pass():
    rec = _record(report.DEGENERATE, 400)
    assert report.classify_record(rec) == report.OK
    assert report.flag_for_record(rec) is None


def test_degenerate_5xx_is_fail():
    rec = _record(report.DEGENERATE, 500)
    assert report.classify_record(rec) == report.DEGENERATE_FAILED
    assert report.flag_for_record(rec) == "degenerate r-0 returned 500"


def test_degenerate_timeout_is_fail():
    rec = _record(report.DEGENERATE, None)
    assert report.classify_record(rec) == report.DEGENERATE_FAILED
    assert report.flag_for_record(rec) == "degenerate r-0 timed out"


def test_clean_2xx_is_pass():
    rec = _record(report.CLEAN, 200)
    assert report.classify_record(rec) == report.OK
    assert report.flag_for_record(rec) is None


def test_clean_non_2xx_is_fail():
    rec = _record(report.CLEAN, 400)
    assert report.classify_record(rec) == report.CLEAN_FAILED
    assert report.flag_for_record(rec) == "clean payload r-0 did not 2xx"


def test_clean_5xx_is_fail_not_just_4xx():
    rec = _record(report.CLEAN, 500)
    assert report.classify_record(rec) == report.CLEAN_FAILED


def test_other_categories_default_ok_on_2xx_or_4xx():
    for category in (
        report.MESSY_BUT_VALID,
        report.MULTILINGUAL_GIBBERISH,
        report.STRUCTURALLY_MALFORMED,
    ):
        for status in (200, 400):
            rec = _record(category, status)
            assert report.classify_record(rec) == report.OK


def test_assertion_failed_when_matcher_says_so_and_category_rules_do_not_apply():
    rec = _record(report.MESSY_BUT_VALID, 200)
    rec["assertion"] = {"passed": False, "mismatches": ["subject mismatch"]}
    assert report.classify_record(rec) == report.ASSERTION_FAILED


def test_run_where_everything_500s_vs_everything_200s_look_different():
    all_500 = [_record(report.DEGENERATE, 500, rid=f"d-{i}") for i in range(3)]
    all_200 = [_record(report.DEGENERATE, 400, rid=f"d-{i}") for i in range(3)]
    summary_500 = report.compute_summary(all_500, seed=1, config={})
    summary_200 = report.compute_summary(all_200, seed=1, config={})
    assert summary_500["flags"] != summary_200["flags"]
    assert summary_500["by_status_class"] != summary_200["by_status_class"]
    assert len(summary_500["flags"]) == 3
    assert len(summary_200["flags"]) == 0


# ---------------------------------------------------------------------------
# category_label: internal recipe names -> schema labels
# ---------------------------------------------------------------------------


def test_category_label_maps_underscored_recipe_names():
    assert report.category_label("messy_but_valid") == "messy-but-valid"
    assert report.category_label("multilingual_gibberish") == "multilingual-gibberish"
    assert report.category_label("structurally_malformed") == "structurally-malformed"
    assert report.category_label("degenerate") == "degenerate"
    assert report.category_label("clean") == "clean"


def test_category_label_passthrough_for_already_hyphenated():
    assert report.category_label("messy-but-valid") == "messy-but-valid"


# ---------------------------------------------------------------------------
# build_artifact reproduces the shipped fixtures exactly
# ---------------------------------------------------------------------------


def test_build_artifact_matches_sample_run_clean():
    fixture = _load_fixture("sample_run_clean.json")
    artifact = report.build_artifact(
        fixture["seed"], fixture["config"], fixture["records"]
    )
    assert artifact["summary"] == fixture["summary"]
    assert artifact["seed"] == fixture["seed"]
    assert artifact["config"] == fixture["config"]
    assert artifact["records"] == fixture["records"]


def test_build_artifact_matches_sample_run_with_failures():
    # NOTE: web/tests/fixtures/sample_run_with_failures.json's checked-in
    # summary.by_status_class["5xx"] is 1, but two records in that fixture
    # have response.status == 500 (clean-7-0001 and degenerate-7-0000), so
    # the correct count is 2. This is confirmed to be a defect in the
    # fixture itself, not a rule disagreement: running web/expectations.py's
    # own compute_summary() against this same fixture also yields 2, not
    # the 1 baked into the file. See the engine M3 report for the flagged
    # defect. This test asserts against the correctly-recomputed summary
    # (by_status_class only) rather than byte-matching the fixture's stale
    # inline summary block, so it does not silently encode the bug.
    fixture = _load_fixture("sample_run_with_failures.json")
    artifact = report.build_artifact(
        fixture["seed"], fixture["config"], fixture["records"]
    )
    assert artifact["records"] == fixture["records"]
    assert artifact["summary"]["by_category"] == fixture["summary"]["by_category"]
    assert artifact["summary"]["flags"] == fixture["summary"]["flags"]
    assert artifact["summary"]["by_status_class"] == {
        "2xx": 1,
        "4xx": 2,
        "5xx": 2,
        "timeout": 1,
    }


# ---------------------------------------------------------------------------
# Cross-check against web/expectations.py: the other lane's independent copy
# of the same rules. Any disagreement here is a defect to report, not paper
# over.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# format_summary: the human-readable CLI report
# ---------------------------------------------------------------------------


def test_format_summary_contains_counts_and_look_here_list():
    fixture = _load_fixture("sample_run_with_failures.json")
    artifact = report.build_artifact(
        fixture["seed"], fixture["config"], fixture["records"]
    )
    text = report.format_summary(artifact)
    assert "seed=7" in text
    assert "6 payload(s)" in text
    assert "2xx: 1" in text
    assert "5xx: 2" in text
    assert "timeout: 1" in text
    assert "degenerate: 3" in text
    assert "Look here (3):" in text
    assert "clean payload clean-7-0001 did not 2xx" in text
    assert "degenerate degenerate-7-0000 returned 500" in text
    assert "degenerate degenerate-7-0001 timed out" in text


def test_format_summary_no_flags_says_so():
    fixture = _load_fixture("sample_run_clean.json")
    artifact = report.build_artifact(
        fixture["seed"], fixture["config"], fixture["records"]
    )
    text = report.format_summary(artifact)
    assert "Look here: nothing flagged." in text
    assert "Look here (" not in text


def test_format_summary_all_500_vs_all_200_read_differently():
    records_500 = [_record(report.DEGENERATE, 500, rid=f"d-{i}") for i in range(3)]
    records_200 = [_record(report.CLEAN, 200, rid=f"c-{i}") for i in range(3)]
    artifact_500 = report.build_artifact(1, {}, records_500)
    artifact_200 = report.build_artifact(1, {}, records_200)
    text_500 = report.format_summary(artifact_500)
    text_200 = report.format_summary(artifact_200)
    assert text_500 != text_200
    assert "Look here (3):" in text_500
    assert "Look here: nothing flagged." in text_200


def test_agrees_with_web_expectations_on_both_fixtures():
    from web import expectations as web_expectations

    for fixture_name in ("sample_run_clean.json", "sample_run_with_failures.json"):
        fixture = _load_fixture(fixture_name)
        for record in fixture["records"]:
            ours = report.classify_record(record)
            theirs = web_expectations.classify_record(record)
            assert ours == theirs, (
                f"{fixture_name} record {record['id']!r}: "
                f"report.classify_record={ours!r} vs "
                f"web.expectations.classify_record={theirs!r}"
            )
            assert report.flag_for_record(record) == web_expectations.flag_for_record(
                record
            )

        our_summary = report.compute_summary(
            fixture["records"], fixture["seed"], fixture["config"]
        )
        their_summary = web_expectations.compute_summary(
            fixture["records"], fixture["seed"], fixture["config"]
        )
        assert our_summary == their_summary
