"""loop against a real intake pipeline, through the real transport.

Every test starts from a correct pipeline and breaks exactly one thing, which
is the pattern the rest of the pipeline package uses. A test that started from
a broken pipeline and looked for any non-zero exit would pass against a tool
that returned 3 at random, and would keep passing if the tool stopped checking
anything.

The property under test most often is the one the spec cares about most: a tool
with no way to see whether an auto-reply was sent must report that check as
SKIPPED, never passed. There are three separate tests for it, because it is the
one thing most likely to regress.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from pipeline_under_test import PipelineUnderTest

from testinghq.pipeline import loop as tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK

SEED = 13
COUNT = 6
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"
SPEC_MODULE = "loop_integration_spec"


class _VirtualClock:
    """A clock and sleep that cost no real time, for the same reason every
    other tool's tests have one: the rate limiter busy-waits out real seconds
    otherwise, and the poll phase sits out a real quiet window."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def target_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n',
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def spec_probe():
    module = types.ModuleType(SPEC_MODULE)
    module.build = lambda config: None
    sys.modules[SPEC_MODULE] = module
    yield module
    del sys.modules[SPEC_MODULE]


def _publish(spec_probe, adapter_source):
    spec_probe.build = lambda config: adapter_source


def _run(pipeline, target_config, tmp_path, spec_probe, name="loop.json",
         outbound_sink=None, **kwargs):
    """Run `loop.execute` with the pipeline as both the sending client and the
    readback source, and a virtual clock throughout.

    `outbound_sink` is given to BOTH sides deliberately. The first version
    handed it only to the readback config, so the tool was asked to read a file
    the pipeline was never told to write, every run came back with zero
    auto-replies, and the tool looked correct. The two paths have to be the same
    path, and setting it in one place is what keeps them that way.
    """
    if outbound_sink is not None:
        pipeline.outbound_sink = str(outbound_sink)
    _publish(spec_probe, pipeline)
    readback = ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:build")
    outbound = (
        ReadbackConfig(kind="mailbox", path=str(outbound_sink))
        if outbound_sink is not None
        else None
    )
    clock = _VirtualClock()
    lines: list = []
    # The pipeline is BOTH the sending client and the readback source. Passing
    # only the readback was the first version's mistake, and the suite-wide
    # network block caught it immediately: the run had been about to POST to
    # http://localhost:9 for real. That guard is the reason it is worth having.
    code = tool.execute(
        SEED,
        COUNT,
        TARGET,
        target_config,
        str(tmp_path / name),
        readback=readback,
        outbound=outbound,
        tag_prefix="hq",
        rate=1000.0,
        client=pipeline,
        readback_client=pipeline,
        sleep=clock.sleep,
        clock=clock,
        printer=lines.append,
        quiet_window=0.0,
        poll_interval=0.05,
        **kwargs,
    )
    text = "\n".join(lines)
    path = tmp_path / name
    if not path.is_file():
        raise AssertionError(
            f"no artifact written to {path}. A refusal prints its reason and "
            f"writes nothing, so the run's output is the diagnosis.\n--- output ---\n{text}"
        )
    return code, json.loads(path.read_text(encoding="utf-8")), text, pipeline


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_pipeline_that_ignores_machine_mail_is_clean(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    code, artifact, text, _ = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_OK, text
    assert artifact["summary"]["findings"] == 0
    assert artifact["summary"]["tickets_opened"] == 0
    assert artifact["summary"]["auto_replies_emitted"] == 0
    assert "LOOP-SAFE" in text


def test_the_pipeline_really_saw_every_message(
    target_config, tmp_path, spec_probe
):
    """Without this, a clean run could mean nothing was ever sent."""
    pipeline = PipelineUnderTest()
    _code, artifact, _text, _ = _run(pipeline, target_config, tmp_path, spec_probe)
    assert len(pipeline.received) == COUNT
    assert artifact["summary"]["sent"] == COUNT
    assert len(artifact["messages"]) == COUNT


# ---------------------------------------------------------------------------
# Auto-reply
# ---------------------------------------------------------------------------


def test_a_pipeline_that_auto_replies_is_caught(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(auto_reply_machine_mail=True)
    sink = tmp_path / "outbound.jsonl"
    code, artifact, text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe, outbound_sink=sink
    )

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["auto_replies_emitted"] == COUNT
    assert artifact["summary"]["auto_reply_checked"] == COUNT
    assert "LOOPS-DETECTED" in text
    assert "an outbound message was emitted" in text


def test_the_outbound_sink_is_read_as_json_lines(
    target_config, tmp_path, spec_probe
):
    """The sink is the mailbox adapter over a file the pipeline appends to,
    which is the shape an operator already has for mail sinks."""
    pipeline = PipelineUnderTest(auto_reply_machine_mail=True)
    sink = tmp_path / "outbound.jsonl"
    _run(pipeline, target_config, tmp_path, spec_probe, outbound_sink=sink)

    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == COUNT
    assert all("tag" in line for line in lines)
    assert all(line["triggered_by"] for line in lines), "which header triggered it"


def test_without_a_sink_the_auto_reply_check_is_skipped_not_passed(
    target_config, tmp_path, spec_probe
):
    """The central property, end to end. The pipeline here DOES auto-reply, and
    the run still says the check could not run, because no sink was configured
    to see it. Reporting a pass would be a claim with nothing behind it."""
    pipeline = PipelineUnderTest(auto_reply_machine_mail=True)
    code, artifact, text, _ = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_OK, text
    assert artifact["summary"]["auto_reply_skipped"] == COUNT
    assert artifact["summary"]["auto_reply_checked"] == 0
    assert "adapter cannot see field" in text
    assert "does NOT establish that the pipeline" in text
    for message in artifact["messages"]:
        assert message["result"]["replied"] is None


def test_a_skipped_check_is_prominent_in_the_report(
    target_config, tmp_path, spec_probe
):
    """SKIPPED has to be as loud as FAIL, or a clean-looking run with nothing
    checked reads as a pass."""
    pipeline = PipelineUnderTest()
    _code, _artifact, text, _ = _run(pipeline, target_config, tmp_path, spec_probe)
    assert "NOT CHECKED" in text
    assert "not checked" in text, "each row has to say the reply was not checked"
    headline = text.splitlines()[0]
    assert "LOOP-SAFE" in headline
    assert "NOT CHECKED" in headline, (
        "a reader who sees only the verdict line has to know part of the run did "
        f"not happen. First line was: {headline!r}"
    )


def test_the_same_defect_is_a_finding_with_a_sink_and_invisible_without(
    target_config, tmp_path, spec_probe
):
    """The consequence stated plainly, and the reason the report has to make the
    gap legible on every affected row."""
    with_sink = PipelineUnderTest(auto_reply_machine_mail=True)
    code_a, artifact_a, _t, _p = _run(
        with_sink, target_config, tmp_path, spec_probe,
        name="with.json", outbound_sink=tmp_path / "a.jsonl",
    )
    without = PipelineUnderTest(auto_reply_machine_mail=True)
    code_b, artifact_b, _t2, _p2 = _run(
        without, target_config, tmp_path, spec_probe, name="without.json"
    )

    assert code_a == EXIT_MISMATCH
    assert artifact_a["summary"]["auto_replies_emitted"] == COUNT
    assert code_b == EXIT_OK
    assert artifact_b["summary"]["auto_replies_emitted"] == 0
    assert artifact_b["summary"]["auto_reply_skipped"] == COUNT


# ---------------------------------------------------------------------------
# Ticketing policy
# ---------------------------------------------------------------------------


def test_a_pipeline_that_tickets_machine_mail_is_caught_by_default(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(ticket_machine_mail=True)
    code, artifact, text, _ = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["tickets_opened"] == COUNT
    assert "a ticket was opened for machine mail" in text


def test_ticket_policy_allowed_downgrades_tickets_to_informational(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(ticket_machine_mail=True)
    code, artifact, text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe, ticket_policy="allowed"
    )

    assert code == EXIT_OK, text
    assert artifact["summary"]["tickets_opened"] == COUNT, "the fact is still counted"
    assert artifact["summary"]["findings"] == 0


def test_ticket_policy_allowed_keeps_the_auto_reply_finding(
    target_config, tmp_path, spec_probe
):
    """Allowing a ticket is a policy decision. Allowing a mail loop is not."""
    pipeline = PipelineUnderTest(
        ticket_machine_mail=True, auto_reply_machine_mail=True
    )
    code, _artifact, text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe,
        ticket_policy="allowed", outbound_sink=tmp_path / "out.jsonl",
    )
    assert code == EXIT_MISMATCH
    assert "an outbound message was emitted" in text


# ---------------------------------------------------------------------------
# The loop bait
# ---------------------------------------------------------------------------


def test_a_pipeline_that_answers_the_loop_bait_is_caught(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(answer_loop_bait=True)
    code, artifact, text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe,
        outbound_sink=tmp_path / "out.jsonl",
    )

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["loop_bait_answered"] >= 1
    assert "the loop bait was answered, which is a live mail loop" in text


def test_the_bait_is_caught_even_when_tickets_are_allowed(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(answer_loop_bait=True, ticket_machine_mail=True)
    code, _artifact, text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe,
        ticket_policy="allowed", outbound_sink=tmp_path / "out.jsonl",
    )
    assert code == EXIT_MISMATCH
    assert "the loop bait was answered" in text


def test_untouched_bait_is_clean_under_every_policy(
    target_config, tmp_path, spec_probe
):
    for policy in tool.TICKET_POLICIES:
        pipeline = PipelineUnderTest()
        code, artifact, text, _ = _run(
            pipeline, target_config, tmp_path, spec_probe,
            name=f"{policy}.json", ticket_policy=policy,
            outbound_sink=tmp_path / f"{policy}.jsonl",
        )
        assert code == EXIT_OK, f"{policy}: {text}"
        assert artifact["summary"]["loop_bait_answered"] == 0, policy


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_the_artifact_records_every_message_and_finding(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest(auto_reply_machine_mail=True)
    _code, artifact, _text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe,
        outbound_sink=tmp_path / "out.jsonl",
    )

    for key in ("seed", "config", "summary", "messages"):
        assert key in artifact
    assert len(artifact["messages"]) == COUNT
    for message in artifact["messages"]:
        for key in ("tag", "record_id", "kind", "marker", "result", "is_loop_bait"):
            assert key in message
        assert message["result"]["replied"] is True
        assert message["result"]["findings"]


def test_the_artifact_config_reproduces_the_run(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    _code, artifact, _text, _ = _run(
        pipeline, target_config, tmp_path, spec_probe,
        outbound_sink=tmp_path / "out.jsonl",
    )
    config = artifact["config"]
    assert config["tool"] == "loop"
    assert config["seed"] == SEED and config["count"] == COUNT
    assert config["ticket_policy"] == "none"
    assert config["readback_poll"]["quiet_window"] == 0.0
    assert "latency" not in config and "timestamp" not in config


def test_the_artifact_survives_a_round_trip_through_json(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    _code, artifact, _text, _ = _run(pipeline, target_config, tmp_path, spec_probe)
    assert json.loads(json.dumps(artifact)) == artifact
