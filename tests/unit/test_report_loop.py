"""`report` on a loop artifact says what `loop` itself said.

The reader was written before `loop` existed, from the artifact shape in the
spec, and it dropped the one qualifier `loop` insists on: a run with no outbound
sink did not check auto-replies, and its headline says so. These tests build the
summary with `loop`'s own `summarize`, not a hand-written dict, so the reader
and the tool cannot drift apart again without a failure here.
"""
from __future__ import annotations

from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK
from testinghq.pipeline.loop import LoopResult, summarize
from testinghq.reporting import summarize as report


def _artifact(summary):
    return {
        "seed": 13,
        "config": {"tool": "loop", "seed": 13, "count": 4, "target": "local",
                   "dry_run": False},
        "summary": summary,
        "messages": [],
    }


def _result(tag, replied, findings=()):
    return LoopResult(
        tag=tag, kind="auto-reply", marker="Auto-Submitted: auto-replied",
        ticked=False, replied=replied, is_loop_bait=False,
        findings=tuple(findings),
    )


def test_a_run_without_an_outbound_sink_keeps_its_qualifier():
    results = [_result("hq-1-0000", None), _result("hq-1-0001", None)]
    out = report(_artifact(summarize(results, results, "none")))
    assert out.verdict == "LOOP-SAFE (auto-reply NOT CHECKED)"
    assert out.exit_code == EXIT_OK
    assert [f.kind for f in out.findings] == ["not-checked"]
    assert "2 message(s)" in out.findings[0].detail


def test_a_run_that_checked_every_reply_has_no_qualifier():
    results = [_result("hq-1-0000", False), _result("hq-1-0001", False)]
    out = report(_artifact(summarize(results, results, "none")))
    assert out.verdict == "LOOP-SAFE"
    assert out.findings == ()
    assert out.counts.passed == 2 and out.counts.failed == 0


def test_passed_is_checked_minus_failed_not_checked():
    results = [
        _result("hq-1-0000", False),
        _result("hq-1-0001", True, findings=("auto-replied to machine mail",)),
        _result("hq-1-0002", False),
    ]
    out = report(_artifact(summarize(results, results, "none")))
    assert out.verdict == "LOOPS-DETECTED"
    assert out.exit_code == EXIT_FINDING
    assert (out.counts.sent, out.counts.passed, out.counts.failed) == (3, 2, 1)
