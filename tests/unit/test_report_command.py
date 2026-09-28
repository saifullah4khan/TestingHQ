"""`testinghq report`: one reader for every tool's artifact.

The tests here do three jobs that are not the same job.

The easy half is that each tool's artifact is detected and summarized. That is
a table of examples and it would be covered by one test per tool.

The interesting half is that the summary does not lie. Two things it reports are
not in the artifact: the verdict, which `verify` and `loop` compute at print
time and never write down, and the exit code, which no tool records at all.
Both are reconstructed here, so both are pinned against the tool that owns
them. `test_the_derived_verdicts_match_the_tools_own_formatters` is the one
that matters: it calls `verify.format_verification` and compares. If someone
changes how verify decides it is verified, this test fails rather than the two
answers quietly diverging.

The last half is refusal. An unknown artifact has to be refused rather than
guessed at, and the cases worth checking are the ones that LOOK like an
artifact: valid JSON of the wrong shape, and JSON that is a list.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from testinghq.core import exit_codes
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.reporting import (
    KNOWN_TOOLS,
    ArtifactError,
    detect_tool,
    load,
    render,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "web" / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures, one per tool, built the way each tool builds them
# ---------------------------------------------------------------------------


def blast_artifact(flags=(), dry_run=True):
    return {
        "seed": 1,
        "config": {"mix": ["clean"], "count": 2, "seed": 1,
                   "dry_run": dry_run, "target": None},
        "summary": {
            "by_status_class": {"2xx": 2, "4xx": 0, "5xx": 0, "timeout": 0},
            "by_category": {"clean": 2},
            "flags": list(flags),
        },
        "records": [
            {"id": "clean-1-0000", "category": "clean",
             "assertion": {"passed": True, "mismatches": []}},
            {"id": "clean-1-0001", "category": "clean",
             "assertion": {"passed": True, "mismatches": []}},
        ],
    }


def barrage_artifact(knee=None):
    return {
        "seed": 3,
        "config": {"mode": "open", "rate": 20.0, "duration": 5.0, "warmup": 1.0,
                   "concurrency": 4, "seed": 3, "pool_size": 4, "target": "local",
                   "dry_run": False},
        "summary": {
            "throughput": {"targeted_rps": 20.0, "achieved_rps": 19.4,
                           "total_requests": 97},
            "latency_ms": {"p50": 12.0, "p90": 30.0, "p99": 48.0},
            "error_rate": 0.0,
            "knee": knee,
        },
        "buckets": [
            {"start": 0.0, "targeted_rps": 20.0, "achieved_rps": 20.0,
             "count": 20, "errors": 0, "error_rate": 0.0, "p50_latency_ms": 11.0},
            {"start": 1.0, "targeted_rps": 20.0, "achieved_rps": 19.0,
             "count": 19, "errors": 0, "error_rate": 0.0, "p50_latency_ms": 13.0},
        ],
    }


def verify_artifact(sent=2, found=2, verified=2, failed=0, skipped=None):
    summary = {
        "sent": sent, "found": found, "verified": verified, "failed": failed,
        "transport_unanswered": 0, "transport_non_2xx": 0,
        "checks_passed_by_name": {"sender": verified},
        "checks_skipped_by_reason": skipped or {},
    }
    return {
        "seed": 5,
        "config": {"tool": "verify", "seed": 5, "count": sent,
                   "tag_prefix": "hq-5", "target": "local", "readback": {},
                   "expect_route": None, "body_exact": False,
                   "readback_poll": {}, "dry_run": False},
        "summary": summary,
        "records": [],
    }


def ledger_artifact(balanced=True, extra=None, verdict="BALANCED"):
    return {
        "seed": 9,
        "config": {"tool": "ledger", "seed": 9, "count": 2, "tag_prefix": "hq-9",
                   "target": "local", "readback": {}, "expect_route": None,
                   "readback_poll": {}, "dry_run": False},
        "summary": {
            "sent": 2, "produced": 2, "exactly_once": 2,
            "missing": [],
            "duplicated": [{"tag": "hq-9-0001", "count": 2,
                            "tickets": ["T1", "T2"]}] if not balanced else [],
            "wrong": [], "extra": extra, "strays_searched": extra is not None,
            "balanced": balanced, "verdict": verdict,
        },
        "records": [],
    }


def redeliver_artifact(failed=0, findings=()):
    return {
        "seed": 11,
        "config": {"tool": "redeliver", "seed": 11, "count": 1,
                   "tag_prefix": "hq-11", "target": "local", "readback": {},
                   "retry_after": 120.0,
                   "scenarios": ["duplicate", "reply-first"], "dry_run": False},
        "summary": {"scenarios": 2, "passed": 2 - failed, "failed": failed,
                    "deliveries": 3, "findings": failed,
                    "strays_searched": True,
                    "verdict": "DELIVERY-SAFE" if not failed else "DELIVERY-BUGS"},
        "scenarios": [
            {"scenario": "duplicate", "question": "dedupe?", "deliveries": 2,
             "unique_messages": 1, "strays_searched": True, "passed": not failed,
             "findings": list(findings), "messages": []},
        ],
    }


def loop_artifact(findings=0):
    return {
        "seed": 13,
        "config": {"tool": "loop", "seed": 13, "count": 4, "target": "local",
                   "outbound": "local", "ticket_policy": "open",
                   "reply_address": "loop@example.test", "dry_run": False},
        "summary": {"sent": 4, "checked": 4, "findings": findings,
                    "ticket_policy": "open", "tickets_opened": 4,
                    "auto_replies_emitted": 0, "auto_reply_checked": 0,
                    "auto_reply_skipped": 0, "loop_bait_answered": 0},
        "messages": [],
    }


def compare_artifact(regressed=True):
    return {
        "comparable": True, "warnings": [], "aligned_by": "id",
        "baseline_seed": 1, "candidate_seed": 1, "records_compared": 4,
        "unchanged": 3,
        "regressions": {"count": 1 if regressed else 0,
                        "records": [{"id": "clean-1-0001", "category": "clean",
                                     "from": "clean", "to": "lost",
                                     "status_from": 200, "status_to": 500,
                                     "flag": "did not 2xx"}] if regressed else [],
                        "truncated": False},
        "fixes": {"count": 0, "records": [], "truncated": False},
        "changes": {"count": 0, "records": [], "truncated": False},
        "totals": [{"key": "2xx", "baseline": 4, "candidate": 3, "delta": -1}],
        "flags": {"baseline": 0, "candidate": 1},
        "regressed": regressed,
    }


ALL_FIXTURES = {
    "blast": blast_artifact(),
    "barrage": barrage_artifact(),
    "verify": verify_artifact(),
    "ledger": ledger_artifact(),
    "redeliver": redeliver_artifact(),
    "loop": loop_artifact(),
    "compare": compare_artifact(),
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(ALL_FIXTURES))
def test_every_tool_is_detected(tool):
    assert detect_tool(ALL_FIXTURES[tool]) == tool


def test_the_known_tools_are_the_ones_with_readers():
    assert set(KNOWN_TOOLS) == set(ALL_FIXTURES), (
        "a reader exists for a tool with no fixture, or a fixture exists for a "
        "tool with no reader; one of the two halves is out of date"
    )


def test_detection_prefers_the_pipeline_tool_key_over_the_generic_shape():
    """`records` is not a blast signature. verify and ledger write it too, and
    getting this backwards would attribute a pipeline run to blast and report
    its checks as HTTP statuses."""
    assert detect_tool(verify_artifact()) == "verify"
    assert detect_tool(ledger_artifact()) == "ledger"
    assert "by_status_class" not in verify_artifact()["summary"]


def test_detect_looks_for_the_pipeline_tool_key_before_guessing_from_shape():
    """A future tool that sets config.tool gets its own reader by setting one
    key, and never falls through to blast because it happens to have records."""
    future = {
        "seed": 1,
        "config": {"tool": "brandnew", "seed": 1},
        "summary": {},
        "records": [],
    }
    assert detect_tool(future) == "brandnew"
    with pytest.raises(ArtifactError) as excinfo:
        summarize(future)
    assert "brandnew" in str(excinfo.value)
    assert "no reader" in str(excinfo.value), (
        "a known-but-unread artifact must say so, rather than being guessed at"
    )


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(ALL_FIXTURES))
def test_every_tool_summarizes_to_the_same_shape(tool):
    """The whole point. A consumer reads one set of keys whatever wrote the
    file, so every key has to be present for every tool."""
    summary = summarize(ALL_FIXTURES[tool])
    payload = summary.to_json()
    for key in ("tool", "verdict", "verdict_source", "exit_code", "exit_code_source",
                "counts", "findings", "seed", "target", "dry_run"):
        assert key in payload, f"{tool} summary is missing {key}"
    for key in ("sent", "found", "passed", "failed", "missing", "duplicated",
                "extra", "produced", "records", "scenarios", "buckets"):
        assert key in payload["counts"], f"{tool} counts is missing {key}"


@pytest.mark.parametrize("tool", sorted(ALL_FIXTURES))
def test_every_tool_serializes_to_json(tool):
    """--json is the machine-readable contract, so it has to be JSON for every
    tool, not just the ones someone tried."""
    payload = summarize(ALL_FIXTURES[tool]).to_json()
    assert json.loads(json.dumps(payload)) == payload


def test_a_finding_is_flat_so_a_consumer_never_special_cases_one():
    """The tools' own findings are variously counts, lists of objects and lists
    of strings. Flattening them is the point of this module; a summary that
    kept each tool's shape would be the problem it was written to remove."""
    summary = summarize(ledger_artifact(balanced=False, verdict="UNACCOUNTED"))
    for finding in summary.findings:
        assert isinstance(finding.kind, str)
        assert isinstance(finding.detail, str)
        assert json.loads(json.dumps({"kind": finding.kind, "detail": finding.detail}))


def test_a_clean_run_and_a_flagged_run_say_different_things():
    clean = summarize(blast_artifact(flags=[]))
    flagged = summarize(blast_artifact(flags=["clean-1-0001 did not 2xx"]))
    assert clean.verdict == "CLEAN"
    assert flagged.verdict == "FLAGGED"
    assert clean.exit_code == EXIT_OK
    assert flagged.exit_code == EXIT_FINDING
    assert flagged.findings and not clean.findings


# ---------------------------------------------------------------------------
# The derived verdicts, pinned against the tools that own them
# ---------------------------------------------------------------------------


def test_the_derived_verdicts_match_the_tools_own_formatters():
    """The test this module most needs.

    `verify` never writes its verdict down; it computes VERIFIED or MISMATCHED
    at print time. So this module reimplements that, and a reimplementation is
    a second answer waiting to disagree. This calls verify's own formatter with
    the same summary and requires the same word back.

    Three cases, because the three branches are where a reimplementation goes
    wrong: all passed, one failed, and nothing checkable at all. The last is
    the one that matters most, because "nothing checked" is a different claim
    from "everything passed" and conflating them is a green tick nobody earned.
    """
    from testinghq.pipeline import verify as pipeline_verify

    for sent, found, verified, failed in (
        (2, 2, 2, 0),     # everything passed
        (2, 2, 1, 1),     # something failed
        (2, 0, 0, 0),     # nothing was checkable
    ):
        artifact = verify_artifact(sent=sent, found=found,
                                   verified=verified, failed=failed)
        mine = summarize(artifact).verdict

        # The real formatter, on the real artifact, so this compares two
        # independent readings of the same file rather than two of a fixture.
        theirs = pipeline_verify.format_verification(artifact)

        assert mine in theirs, (
            f"report says {mine!r} where verify's own formatter leads with "
            f"{theirs.splitlines()[0]!r} for sent={sent} found={found} "
            f"verified={verified} failed={failed}. One of the two is now wrong."
        )


def test_the_derived_exit_code_matches_each_tool_s_own_decision():
    """Same reason as the verdict. Each tool computes its exit code from its
    own summary fields, so each is checked against those fields rather than
    against a number this module picked."""
    # verify: failed == 0
    assert summarize(verify_artifact(failed=0)).exit_code == EXIT_OK
    assert summarize(verify_artifact(failed=1)).exit_code == EXIT_FINDING

    # ledger: summary.balanced
    assert summarize(ledger_artifact(balanced=True)).exit_code == EXIT_OK
    assert summarize(
        ledger_artifact(balanced=False, verdict="UNACCOUNTED")
    ).exit_code == EXIT_FINDING

    # redeliver: summary.failed == 0
    assert summarize(redeliver_artifact(failed=0)).exit_code == EXIT_OK
    assert summarize(redeliver_artifact(failed=1)).exit_code == EXIT_FINDING

    # loop: summary.findings == 0
    assert summarize(loop_artifact(findings=0)).exit_code == EXIT_OK
    assert summarize(loop_artifact(findings=2)).exit_code == EXIT_FINDING

    # compare: regressed
    assert summarize(compare_artifact(regressed=True)).exit_code == EXIT_FINDING
    assert summarize(compare_artifact(regressed=False)).exit_code == EXIT_OK


def test_barrage_always_reports_the_code_its_own_execute_returns():
    """barrage's execute returns EXIT_OK for any completed run. Its health lives
    in the artifact, not the exit code.

    Asserted because it is the one tool where a well-meaning "and a knee is a
    finding, so exit 3" change would silently alter what a CI script gating on
    the code does. The knee is a finding in the summary; the code stays 0.
    """
    steady = summarize(barrage_artifact(knee=None))
    knee = summarize(barrage_artifact(knee={
        "at_seconds": 3.0, "reason": "shedding", "detail": "p90 above ceiling",
        "targeted_rps": 20.0, "achieved_rps": 12.0,
    }))
    assert steady.exit_code == EXIT_OK
    assert knee.exit_code == EXIT_OK
    assert knee.verdict == "KNEE"
    assert knee.findings, "the knee is still reported as a finding"


# ---------------------------------------------------------------------------
# Reading from disk
# ---------------------------------------------------------------------------


def test_the_two_shipped_blast_fixtures_are_read():
    """The artifacts already in the tree, built by the real blast run, not by
    the helpers above. The only run artifacts committed anywhere."""
    clean = load(FIXTURES / "sample_run_clean.json")
    assert clean.tool == "blast"
    assert clean.verdict == "CLEAN"
    assert clean.counts.records == 6

    failures = load(FIXTURES / "sample_run_with_failures.json")
    assert failures.tool == "blast"
    assert failures.verdict == "FLAGGED"
    assert failures.counts.failed == 3
    assert len(failures.findings) == 3


def test_a_missing_file_is_refused_with_its_path(tmp_path):
    with pytest.raises(ArtifactError) as excinfo:
        load(tmp_path / "nope.json")
    assert "no such artifact" in str(excinfo.value)


def test_a_file_that_is_not_json_is_refused_and_says_what_it_is(tmp_path):
    """A config file is the likely mistake here, so the message names the
    possibility rather than only reporting a JSON error at a line and column."""
    path = tmp_path / "target.toml"
    path.write_text("[targets.local]\nurl = 'http://127.0.0.1'\n", encoding="utf-8")
    with pytest.raises(ArtifactError) as excinfo:
        load(path)
    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "config file" in message


def test_valid_json_that_is_not_an_artifact_is_refused(tmp_path):
    """The case worth being careful about. `[]`, `{}` and a bare number are all
    valid JSON, and a summary of one of them would be confidently reporting a
    run that never happened."""
    for payload in ([], {}, 3, "a string", None):
        path = tmp_path / "thing.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ArtifactError):
            load(path)


def test_json_that_looks_ish_but_names_no_known_tool_is_refused(tmp_path):
    """A partial artifact. It has seed and config and summary, like a real one,
    but nothing identifies the producer."""
    path = tmp_path / "half.json"
    path.write_text(
        json.dumps({"seed": 1, "config": {}, "summary": {}, "records": []}),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError) as excinfo:
        load(path)
    message = str(excinfo.value)
    assert "could not tell which tool" in message
    assert "records" in message, "the message should show what it did find"


def test_a_refusal_does_not_guess_from_one_shared_key(tmp_path):
    """`records` alone is not enough to call something blast. Attributing an
    artifact to the wrong tool would report its checks as HTTP statuses, which
    is worse than refusing."""
    path = tmp_path / "records-only.json"
    path.write_text(
        json.dumps({"records": [{"id": "x"}], "summary": {}}), encoding="utf-8"
    )
    with pytest.raises(ArtifactError):
        load(path)


# ---------------------------------------------------------------------------
# Findings that are about the run rather than the records
# ---------------------------------------------------------------------------


def test_strays_not_searched_is_distinct_from_no_strays():
    """`extra: null` means the adapter could not enumerate, so stray records
    were never looked for. Reporting that as "no strays" would be a green tick
    earned by not looking."""
    not_searched = summarize(ledger_artifact(extra=None))
    searched_none = summarize(ledger_artifact(extra=[]))

    kinds_not = [f.kind for f in not_searched.findings]
    kinds_none = [f.kind for f in searched_none.findings]

    assert "strays-not-searched" in kinds_not
    assert "strays-not-searched" not in kinds_none


def test_a_truncated_regression_list_says_so():
    """compare caps the records it lists at 25 and records the real count. A
    summary that printed 25 without saying so would understate the damage."""
    artifact = compare_artifact(regressed=True)
    artifact["regressions"]["count"] = 41
    artifact["regressions"]["truncated"] = True
    summary = summarize(artifact)
    assert any(f.kind == "truncated" for f in summary.findings)
    assert summary.counts.failed == 41, "the count is the real one, not the listed one"


def test_skipped_checks_are_grouped_by_reason_not_listed_one_by_one():
    """A run where the adapter cannot see the body produces one skip per record
    per check. One finding per reason with a count is the information."""
    artifact = verify_artifact(
        skipped={
            "adapter cannot see field": {"body": 12, "route": 12},
            "nothing to check": {"references": 12},
        }
    )
    summary = summarize(artifact)
    by_kind = {f.kind: f.detail for f in summary.findings}
    assert "skipped:adapter cannot see field" in by_kind
    assert "body" in by_kind["skipped:adapter cannot see field"]
    assert "24" in by_kind["skipped:adapter cannot see field"]
    assert len(summary.findings) == 2, "two reasons, two findings"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_text_form_says_when_a_verdict_was_derived():
    """A derived verdict is a reconstruction. Marking it as recorded would be
    claiming the artifact says something it does not."""
    assert "(derived)" in render(summarize(blast_artifact(flags=[])))
    # ledger records its own verdict, so it is not marked.
    assert "(derived)" not in render(summarize(ledger_artifact(verdict="BALANCED")))


def test_the_text_form_says_the_exit_code_is_derived():
    """Every exit code here is reconstructed, because no artifact records one.

    The number keeps the name the contract calls for, `exit_code`, and this
    line is where a reader learns it is a reconstruction. The first version of
    this file called the field `exit_code_derived` instead, which was honest and
    broke a name the contract specified. The honesty belongs in a separate
    field, exactly as it does for the verdict.
    """
    text = render(summarize(blast_artifact(flags=[])))
    assert "exit code: 0 (derived" in text
    assert "not recorded in the artifact" in text


def test_the_json_keys_are_the_names_the_contract_calls_for():
    """`tool`, `verdict`, `exit_code`, counts, findings.

    These are the names a consumer parses, so they are not this file's to
    rename. The first version of this module emitted `exit_code_derived`,
    because no artifact records an exit code and the field was a
    reconstruction. That reasoning is right and the rename was still wrong: a
    consumer written against the contract would have found nothing there.

    The honesty is kept, in `exit_code_source` and `verdict_source`. A value plus
    where it came from, rather than a value whose name carries a caveat nobody
    asked for.
    """
    payload = summarize(ALL_FIXTURES["verify"]).to_json()
    for key in ("tool", "verdict", "exit_code", "counts", "findings"):
        assert key in payload, f"the contract names {key!r} and it is missing"
    assert "exit_code_derived" not in payload, (
        "exit_code_derived was an earlier name for exit_code. If it is back, the "
        "contract is not being followed and a consumer written against the "
        "documented key will not find it."
    )
    assert payload["exit_code_source"] in {"derived", "recorded"}


@pytest.mark.parametrize("tool", sorted(ALL_FIXTURES))
def test_every_tool_says_where_its_exit_code_came_from(tool):
    """Nothing is recorded in an artifact, so every one of these is `derived`.

    `compare` is the interesting one: its artifact does record a `regressed`
    boolean, but the exit code itself is still computed here, so it is `derived`
    like the rest. A tool whose artifact did carry the code would say `recorded`,
    and nothing else here would need to change.
    """
    summary = summarize(ALL_FIXTURES[tool])
    assert summary.exit_code_source == "derived", (
        f"{tool} reported {summary.exit_code_source!r}; no artifact records an "
        "exit code, so every one of them is reconstructed"
    )


def test_a_derived_exit_code_is_never_a_dry_run():
    """The shared convention, applied to a reconstructed value.

    2 means a run that deliberately sent nothing. If a reconstruction ever
    produced 2, a consumer would read a finding as a dry run, which is the exact
    misreading the convention was introduced to remove. Worth a test because the
    code is computed seven times over and nothing else checks that the values it
    produces are ones with the agreed meaning.
    """
    for tool, artifact in ALL_FIXTURES.items():
        code = summarize(artifact).exit_code
        assert code in (exit_codes.EXIT_OK, exit_codes.EXIT_FINDING), (
            f"{tool} produced exit code {code}, which is not a code this project "
            "uses for a reconstructed answer. A report that was read "
            "successfully is neither a refusal nor a dry run."
        )


def test_the_text_form_is_stable_enough_to_grep():
    """A CI log reader looks for a line per fact, so the layout is part of the
    contract rather than a matter of taste."""
    text = render(summarize(redeliver_artifact(failed=1, findings=["duplicate"])))
    assert text.splitlines()[0] == "tool:    redeliver"
    assert "verdict: DELIVERY-BUGS" in text
    assert "[scenario:duplicate] duplicate" in text


def test_a_dry_run_is_said_to_be_one():
    text = render(summarize(blast_artifact(dry_run=True)))
    assert "nothing was sent" in text


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run(argv, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *argv],
        capture_output=True, text=True, cwd=cwd, timeout=300,
    )


def _write(tmp_path, payload, name="artifact.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_command_prints_a_summary(tmp_path):
    path = _write(tmp_path, verify_artifact())
    result = _run(["report", str(path)])
    assert result.returncode == EXIT_OK, result.stderr
    assert "tool:    verify" in result.stdout
    assert "VERIFIED" in result.stdout


def test_the_command_json_output_is_parseable(tmp_path):
    path = _write(tmp_path, ledger_artifact(balanced=False, verdict="UNACCOUNTED"))
    result = _run(["report", str(path), "--json"])
    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["tool"] == "ledger"
    assert payload["verdict"] == "UNACCOUNTED"
    assert payload["exit_code"] == EXIT_FINDING


def test_the_command_succeeds_even_when_the_run_found_something(tmp_path):
    """Reading a report is not running a test. Exiting non-zero because the
    artifact says something went wrong would make the command useless in a
    pipeline that wants to read the report and decide for itself."""
    path = _write(tmp_path, blast_artifact(flags=["did not 2xx"]))
    result = _run(["report", str(path)])
    assert result.returncode == EXIT_OK, (
        "report exits 0 for a readable artifact; the findings are in the output"
    )
    assert "FLAGGED" in result.stdout


def test_the_command_refuses_an_unreadable_artifact(tmp_path):
    result = _run(["report", str(tmp_path / "gone.json")])
    assert result.returncode == EXIT_REFUSED
    assert "report:" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_command_refuses_something_that_is_not_an_artifact(tmp_path):
    path = _write(tmp_path, {"hello": "world"})
    result = _run(["report", str(path)])
    assert result.returncode == EXIT_REFUSED
    assert "could not tell which tool" in result.stderr


def test_the_command_offers_no_send_or_target_flags():
    """Structurally, by what the parser accepts.

    Checked by trying them rather than by reading the help text, because the
    help text legitimately mentions `--send` when it explains what exit code 2
    means. "Mentions the word" is not "accepts the flag", and only the second
    one is the property that matters.
    """
    for flag in ("--send", "--target", "--config"):
        result = _run(["report", "artifact.json", flag, "x"])
        assert result.returncode == EXIT_REFUSED, (
            f"{flag} was accepted by the report subcommand; it must not be"
        )
        assert "unrecognized" in result.stderr.lower() or "expected" in result.stderr.lower()


def test_the_command_needs_no_config_file(tmp_path):
    """Run from a directory that is not the repository, with no config, no
    target and nothing else on disk. An artifact is a file, and reading it needs
    nothing else to be set up."""
    import os

    path = _write(tmp_path, blast_artifact())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", "report", str(path)],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=300,
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "tool:    blast" in result.stdout
