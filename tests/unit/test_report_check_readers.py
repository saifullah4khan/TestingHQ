"""`testinghq report` on artifacts written by verify, ledger and redeliver.

The artifacts are built by the tools' own `build_artifact`, so a change to
either side that the other does not follow fails here.
"""
from __future__ import annotations

import pytest

from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK
from testinghq.core.transport import TransportResult
from testinghq.pipeline import ledger, redeliver, verify
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import ReadbackOutcome, SentMessage
from testinghq.pipeline.messages import make_tag, probe_for
from testinghq.pipeline.readback import Readback
from testinghq.reporting import ArtifactError, render, summarize

SEED = 3
COUNT = 3
POLL = {"quiet_window": 0.0, "max_wait": 1.0, "poll_interval": 0.1}
READBACK = ReadbackConfig(kind="http", url="http://localhost:8000/tickets")


def _sent():
    out = []
    for index, (email, tag, rid) in enumerate(verify.build_tagged_corpus(SEED, COUNT)):
        out.append(SentMessage(
            index=index, record_id=rid, tag=tag, email=email,
            probe=probe_for(email, tag, rid, ""),
            result=TransportResult(status=200, latency_ms=1.0, body_snippet="ok"),
        ))
    return out


def _good(message, ticket="T1"):
    email = message.email
    return Readback(
        exists=True, ticket_id=ticket, from_addr=email.ground_truth.from_addr,
        subject=email.ground_truth.subject, body=email.text,
        attachment_names=tuple(a.filename for a in email.attachments), tag=message.tag,
        fields=("from_addr", "subject", "body", "attachment_names"),
    )


def _verify_artifact(readbacks, outcome=None):
    sent = _sent()
    verifications = verify.grade(sent, readbacks, None, False)
    return verify.build_artifact(
        SEED,
        verify.run_config(SEED, COUNT, "hq", "local", READBACK, None, False, POLL),
        sent, verifications, readbacks, outcome,
    )


def _ledger_artifact(strays, **account_kwargs):
    sent = _sent()
    readbacks = {m.tag: [_good(m, f"T{i}")] for i, m in enumerate(sent)}
    accounting = ledger.account(sent, readbacks, strays, **account_kwargs)
    return ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", READBACK, None, POLL),
        sent, accounting, readbacks,
    )


def test_a_verify_artifact_carries_its_verdict_and_the_reader_uses_it():
    sent = _sent()
    artifact = _verify_artifact({m.tag: [_good(m)] for m in sent})
    assert artifact["schema_version"] == 1 and artifact["tool"] == "verify"
    result = summarize(artifact)
    assert result.verdict == "VERIFIED"
    assert result.verdict_source == "recorded"
    assert result.exit_code == EXIT_OK


def test_an_unsettled_verify_artifact_reads_as_unsettled_and_exits_3():
    class _Unsettled(ReadbackOutcome):
        unsettled = True

    outcome = _Unsettled(readbacks={}, elapsed=1.0, polls=2, stable=False)
    result = summarize(_verify_artifact({}, outcome))
    assert result.verdict == "UNSETTLED"
    assert result.exit_code == EXIT_FINDING
    assert any(f.kind == "unsettled" for f in result.findings)


def test_a_schema_0_verify_artifact_is_still_read_and_derived():
    sent = _sent()
    artifact = _verify_artifact({m.tag: [_good(m)] for m in sent})
    del artifact["schema_version"]
    del artifact["summary"]["verdict"]
    result = summarize(artifact)
    assert result.verdict == "VERIFIED"
    assert result.verdict_source == "derived"


@pytest.mark.parametrize("mode", ["fire", "check"])
@pytest.mark.parametrize("aborted", [False, True])
def test_the_json_tool_is_the_artifacts_own_tool_for_either_verify_mode(mode, aborted):
    """`tool` is a frozen `report --json` key. It used to read "verify fire"
    or "verify check", so a consumer switching on it saw two names for one
    tool, neither of them the artifact's own `tool`."""
    sent = _sent()
    artifact = _verify_artifact({m.tag: [_good(m)] for m in sent})
    artifact["config"]["mode"] = mode
    artifact["aborted"] = aborted
    result = summarize(artifact)
    assert result.to_json()["tool"] == artifact["tool"] == "verify"
    assert render(result).splitlines()[0] == f"tool:    verify ({mode})"


@pytest.mark.parametrize("tool", ["verify", "ledger", "redeliver"])
def test_a_schema_from_the_future_is_refused(tool):
    if tool == "verify":
        artifact = _verify_artifact({})
    elif tool == "ledger":
        artifact = _ledger_artifact([])
    else:
        scenarios = redeliver.build_scenarios(SEED, 1, "hq", 1.0, ["duplicate"])
        results = redeliver.judge_all(scenarios, {}, [])
        artifact = redeliver.build_artifact(
            SEED, redeliver.run_config(SEED, 1, "hq", "local", READBACK, 1.0, None), results
        )
    assert artifact["tool"] == tool
    artifact["schema_version"] = 2
    with pytest.raises(ArtifactError) as caught:
        summarize(artifact)
    assert "schema_version 2" in str(caught.value)


def test_the_ledger_reader_reports_the_exact_extra_count_and_other_runs():
    strays = [
        Readback(exists=True, ticket_id=f"S{i}", tag=make_tag("hq", SEED, 500 + i))
        for i in range(ledger.MAX_ARTIFACT_STRAYS + 3)
    ] + [Readback(exists=True, ticket_id="O1", tag=make_tag("other", SEED, 0))]
    result = summarize(_ledger_artifact(strays))
    assert result.verdict == "UNACCOUNTED"
    assert result.counts.extra == ledger.MAX_ARTIFACT_STRAYS + 3
    kinds = [f.kind for f in result.findings]
    assert "truncated" in kinds
    assert "other-runs" in kinds


def test_the_ledger_reader_names_why_strays_were_not_searched():
    result = summarize(_ledger_artifact(None, strays_reason="[readback].enumerate is false"))
    assert result.verdict == "UNVERIFIED STRAYS"
    assert result.exit_code == EXIT_FINDING
    detail = next(f.detail for f in result.findings if f.kind == "strays-not-searched")
    assert "enumerate is false" in detail


def test_the_redeliver_reader_lists_unattributed_strays():
    scenarios = redeliver.build_scenarios(SEED, 1, "hq", 1.0, ["duplicate"])
    readbacks = {}
    for delivery in scenarios[0].deliveries:
        email = delivery.email
        readbacks[delivery.tag] = [Readback(
            exists=True, ticket_id="T1", from_addr=email.ground_truth.from_addr,
            subject=email.ground_truth.subject, body=email.text,
            attachment_names=tuple(a.filename for a in email.attachments),
            fields=("from_addr", "subject", "body", "attachment_names"),
        )]
    strays = [Readback(exists=True, ticket_id="ODD", tag=make_tag("hq", SEED, 9))]
    results, stray_summary = redeliver.judge_run(scenarios, readbacks, strays)
    artifact = redeliver.build_artifact(
        SEED, redeliver.run_config(SEED, 1, "hq", "local", READBACK, 1.0, None),
        results, None, stray_summary,
    )
    result = summarize(artifact)
    assert result.verdict == "DELIVERY-BUGS"
    assert result.exit_code == EXIT_FINDING
    assert any(f.kind == "extra" and "ODD" in f.detail for f in result.findings)
