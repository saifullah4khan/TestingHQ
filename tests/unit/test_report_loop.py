"""`report` on a loop artifact says what `loop` itself said.

The reader was written before `loop` existed, from the artifact shape in the
spec, and it dropped the one qualifier `loop` insists on: a run with no outbound
sink did not check auto-replies, and its headline says so. These tests build the
summary with `loop`'s own `summarize`, not a hand-written dict, so the reader
and the tool cannot drift apart again without a failure here.
"""
from __future__ import annotations

from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK
import pytest

from testinghq.pipeline import loop
from testinghq.pipeline.loop import LoopResult, summarize
from testinghq.reporting import ArtifactError
from testinghq.reporting import summarize as report


def _artifact(summary, **extra):
    return {
        "schema_version": 1,
        "tool": "loop",
        "seed": 13,
        "config": {"tool": "loop", "seed": 13, "count": 4, "target": "local",
                   "dry_run": False},
        "summary": summary,
        "messages": [],
        **extra,
    }


def _result(tag, replied, findings=()):
    state = {None: loop.REPLY_NOT_CHECKED, True: loop.REPLY_YES, False: loop.REPLY_NO}[replied]
    return LoopResult(
        tag=tag, kind="auto-reply", marker="Auto-Submitted: auto-replied",
        ticked=False, replied=replied, is_loop_bait=False,
        findings=tuple(findings), reply_state=state,
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
    out = report(_artifact(summarize(results, results, "none", outbound_enumerated=True)))
    assert out.verdict == "LOOP-SAFE"
    assert out.verdict_source == "recorded"
    assert out.findings == ()
    assert out.counts.passed == 2 and out.counts.failed == 0


def test_passed_is_checked_minus_failed_not_checked():
    results = [
        _result("hq-1-0000", False),
        _result("hq-1-0001", True, findings=("auto-replied to machine mail",)),
        _result("hq-1-0002", False),
    ]
    out = report(_artifact(summarize(results, results, "none", outbound_enumerated=True)))
    assert out.verdict == "LOOPS-DETECTED"
    assert out.exit_code == EXIT_FINDING
    assert (out.counts.sent, out.counts.passed, out.counts.failed) == (3, 2, 1)


def test_unattributed_outbound_is_a_finding_in_the_report():
    results = [LoopResult(
        tag="hq-1-0000", kind="auto-reply", marker="Auto-Submitted: auto-replied",
        ticked=False, replied=None, is_loop_bait=False,
        reply_state=loop.REPLY_UNATTRIBUTED,
    )]
    summary = summarize(results, results, "none", unattributed=["x"], outbound_enumerated=True)
    out = report(_artifact(summary))
    assert out.verdict == "LOOPS-DETECTED"
    assert out.exit_code == EXIT_FINDING
    assert "unattributed" in [f.kind for f in out.findings]


def test_an_aborted_loop_run_exits_3():
    summary = {"sent": 1, "findings": 0, "checked": 0, "aborted": True, "verdict": "ABORTED"}
    out = report(_artifact(summary, aborted=True))
    assert out.verdict == "ABORTED"
    assert out.exit_code == EXIT_FINDING
    assert out.findings[0].kind == "aborted"


def test_a_schema_0_artifact_is_still_read_and_a_newer_one_is_refused():
    results = [_result("hq-1-0000", False)]
    old = _artifact(summarize(results, results, "none", outbound_enumerated=True))
    del old["schema_version"]
    del old["summary"]["verdict"]
    out = report(old)
    assert out.verdict == "LOOP-SAFE" and out.verdict_source == "derived"
    with pytest.raises(ArtifactError, match="schema_version 2"):
        report(_artifact(old["summary"], schema_version=2))


def test_a_fabricated_bait_parent_is_reported():
    results = [_result("hq-1-0000", False)]
    summary = summarize(results, results, "none", outbound_enumerated=True,
                        bait_parent={"source": "fabricated", "message_id": None,
                                     "reason": "no [loop.outbound] sink is configured"})
    out = report(_artifact(summary))
    assert [f.kind for f in out.findings] == ["bait-fabricated"]
    assert out.exit_code == EXIT_OK
