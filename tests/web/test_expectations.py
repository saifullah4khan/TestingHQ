"""Unit tests for the expectation-based reading logic in web/expectations.py.

These are hand-built record dicts, not generator output, so they pin down
the actual product rule instead of just mirroring whatever the generator
happens to produce:

- degenerate + clean 4xx => PASS
- degenerate + 500 => FAIL
- degenerate + hang (null status) => FAIL
- clean + 2xx => PASS
- clean + anything else (4xx, 5xx, timeout) => FAIL
- other categories default to whatever their own assertion says
"""
from web import expectations


def _record(category, status, passed=True, mismatches=None):
    return {
        "id": "rec-1",
        "category": category,
        "response": {"status": status, "latency_ms": 10.0, "body_snippet": ""},
        "assertion": {"passed": passed, "mismatches": mismatches or []},
    }


def test_degenerate_clean_400_is_pass():
    record = _record("degenerate", 400)
    assert expectations.classify_record(record) == expectations.OK
    assert expectations.flag_for_record(record) is None


def test_degenerate_422_is_pass():
    record = _record("degenerate", 422)
    assert expectations.classify_record(record) == expectations.OK


def test_degenerate_500_is_fail():
    record = _record("degenerate", 500)
    assert expectations.classify_record(record) == expectations.DEGENERATE_FAILED
    assert expectations.flag_for_record(record) == "degenerate rec-1 returned 500"


def test_degenerate_hang_is_fail():
    record = _record("degenerate", None)
    assert expectations.classify_record(record) == expectations.DEGENERATE_FAILED
    assert expectations.flag_for_record(record) == "degenerate rec-1 timed out"


def test_clean_200_is_pass():
    record = _record("clean", 200)
    assert expectations.classify_record(record) == expectations.OK
    assert expectations.flag_for_record(record) is None


def test_clean_non_2xx_is_fail():
    record = _record("clean", 400)
    assert expectations.classify_record(record) == expectations.CLEAN_FAILED
    assert expectations.flag_for_record(record) == "clean payload rec-1 did not 2xx"


def test_clean_500_is_fail():
    record = _record("clean", 500)
    assert expectations.classify_record(record) == expectations.CLEAN_FAILED


def test_clean_timeout_is_fail():
    record = _record("clean", None)
    assert expectations.classify_record(record) == expectations.CLEAN_FAILED


def test_other_category_with_failed_assertion_is_flagged():
    record = _record("messy-but-valid", 200, passed=False, mismatches=["subject mismatch"])
    assert expectations.classify_record(record) == expectations.ASSERTION_FAILED
    assert expectations.flag_for_record(record) is None  # no canonical flag string for this class


def test_other_category_with_passing_assertion_is_ok():
    record = _record("structurally-malformed", 400, passed=True)
    assert expectations.classify_record(record) == expectations.OK


def test_compute_summary_buckets_and_flags():
    records = [
        _record("clean", 200),
        _record("clean", 500),  # clean failed
        _record("degenerate", 400),
        _record("degenerate", 500),  # degenerate failed
        _record("degenerate", None),  # degenerate failed (timeout)
        _record("structurally-malformed", 400),
    ]
    summary = expectations.compute_summary(records, seed=0, config={})

    assert summary["by_status_class"] == {"2xx": 1, "4xx": 2, "5xx": 2, "timeout": 1}
    assert summary["by_category"]["clean"] == 2
    assert summary["by_category"]["degenerate"] == 3
    assert summary["by_category"]["structurally-malformed"] == 1
    assert summary["by_category"]["messy-but-valid"] == 0
    assert summary["by_category"]["multilingual-gibberish"] == 0

    assert "clean payload rec-1 did not 2xx" in summary["flags"]
    assert "degenerate rec-1 returned 500" in summary["flags"]
    assert "degenerate rec-1 timed out" in summary["flags"]
    assert len(summary["flags"]) == 3
