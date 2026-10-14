"""loop against a pipeline double, through the real transport.

Every test starts from a correct pipeline and breaks one thing. A correct
pipeline here acknowledges ordinary mail, files no ticket for machine mail and
sends no auto-reply to it. Its outbound sink is a JSON Lines file read by the
mailbox adapter, which can enumerate, so a reply that carries no tag is still
seen: `tag_outbound=False` makes the double write its replies with no tag and
no threading header, the "we received your message" reply that quotes nothing.
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from pipeline_under_test import PipelineUnderTest, _bare_message_id, _headers_of, _tag_of

from testinghq.pipeline import loop as tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK
from testinghq.pipeline.readback import Readback

SEED = 13
COUNT = len(tool.MACHINE_MAIL_SHAPES)
#: The machine messages plus the bait.
JUDGED = COUNT + 1
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"
SPEC_MODULE = "loop_integration_spec"
REPLY_ADDRESS = "support@intake.example.com"


class LoopPipeline(PipelineUnderTest):
    """The shared pipeline double, with an outbound side a loop can form on.

    Ordinary mail gets an acknowledgement from `REPLY_ADDRESS`, whose
    Message-ID is mapped to the ticket it acknowledges, so a reply to the
    acknowledgement threads onto that ticket. Machine mail is suppressed unless
    a defect switch says otherwise. The bait is recognised the way a real loop
    starts: machine mail whose In-Reply-To is one of this pipeline's own
    outbound messages.
    """

    def __init__(self, *, acknowledge: bool = True, tag_outbound: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.acknowledge = acknowledge
        self.tag_outbound = tag_outbound
        self.own_message_ids = set()

    def _receive(self, fields):
        record = super()._receive(fields)
        if self.acknowledge and record is not None and self._marker(fields) is None:
            message_id = self._write_outbound(fields, "acknowledgement", "", quote=True)
            self._by_message_id[message_id] = record
        return record

    def _marker(self, fields):
        return tool.machine_mail_marker(
            _headers_of(fields), fields.get("subject") or "", fields.get("from") or ""
        )

    def _act_on_machine_mail(self, fields) -> bool:
        marker = self._marker(fields)
        if marker is None:
            return False
        parent = _bare_message_id(_headers_of(fields).get("In-Reply-To"))
        is_bait = parent in self.own_message_ids
        if self.auto_reply_machine_mail or (self.answer_loop_bait and is_bait):
            self._write_outbound(fields, "auto-reply", marker, quote=False)
        return not self.ticket_machine_mail

    def _write_outbound(self, fields, kind, marker, *, quote: bool) -> str:
        number = len(self.outbound) + 1
        message_id = f"out-{number}@intake.example.test"
        self.own_message_ids.add(message_id)
        line = {
            "id": f"OUT{number:04d}",
            "to": fields.get("from") or "",
            "from": REPLY_ADDRESS,
            "subject": f"Re: {fields.get('subject') or ''}",
            "body": "We received your message.",
            "message_id": message_id,
            "kind": kind,
            "triggered_by": marker,
        }
        if self.tag_outbound:
            line["tag"] = _tag_of(fields)
        if self.tag_outbound or quote:
            line["in_reply_to"] = _headers_of(fields).get("Message-ID", "")
        self.outbound.append(line)
        if self.outbound_sink:
            with open(self.outbound_sink, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
        return message_id


class _VirtualClock:
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


def _execute(pipeline, target_config, tmp_path, spec_probe, name, outbound, **kwargs):
    spec_probe.build = lambda config: pipeline
    clock = _VirtualClock()
    lines: list = []
    settings = {"quiet_window": 0.0, "poll_interval": 0.05, "reply_address": REPLY_ADDRESS}
    settings.update(kwargs)
    code = tool.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / name),
        readback=ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:build"),
        outbound=outbound, tag_prefix="hq", rate=1000.0,
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append, **settings,
    )
    text = "\n".join(lines)
    path = tmp_path / name
    if not path.is_file():
        raise AssertionError(f"no artifact written.\n--- output ---\n{text}")
    return code, json.loads(path.read_text(encoding="utf-8")), text


def _run(pipeline, target_config, tmp_path, spec_probe, name="loop.json",
         sink=True, **kwargs):
    """`loop.execute` with the double as the sending client and the inbound
    readback, and its sink file as the outbound readback. The same path is
    given to both sides, or the tool reads a file nobody writes."""
    outbound = None
    if sink:
        path = tmp_path / f"{name}.outbound.jsonl"
        pipeline.outbound_sink = str(path)
        outbound = ReadbackConfig(kind="mailbox", path=str(path))
    return _execute(pipeline, target_config, tmp_path, spec_probe, name, outbound, **kwargs)


def _results(artifact):
    return [m["result"] for m in artifact["messages"] if m["result"] is not None]


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_correct_pipeline_is_loop_safe(target_config, tmp_path, spec_probe):
    code, artifact, text = _run(LoopPipeline(), target_config, tmp_path, spec_probe)

    assert code == EXIT_OK, text
    summary = artifact["summary"]
    assert summary["verdict"] == "LOOP-SAFE"
    assert summary["findings"] == 0
    assert summary["tickets_opened"] == 0
    assert summary["auto_replies_emitted"] == 0
    assert summary["auto_reply_checked"] == JUDGED
    assert summary["outbound_enumerated"] is True
    assert summary["readback"]["stable"] is True


def test_every_shape_and_the_bait_reach_the_pipeline(target_config, tmp_path, spec_probe):
    pipeline = LoopPipeline()
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    assert len(pipeline.received) == JUDGED + 1
    assert artifact["summary"]["sent"] == JUDGED + 1
    kinds = {m["kind"] for m in artifact["messages"]}
    assert kinds == {s[0] for s in tool.MACHINE_MAIL_SHAPES} | {
        tool.KIND_PRECURSOR, tool.KIND_LOOP_BAIT}


def test_the_artifact_names_its_schema_and_tool(target_config, tmp_path, spec_probe):
    _code, artifact, _text = _run(LoopPipeline(), target_config, tmp_path, spec_probe)
    assert artifact["schema_version"] == 1
    assert artifact["tool"] == "loop"
    assert artifact["config"]["tool"] == "loop"
    assert artifact["aborted"] is False
    assert json.loads(json.dumps(artifact)) == artifact


# ---------------------------------------------------------------------------
# The two-phase bait
# ---------------------------------------------------------------------------


def test_the_bait_replies_to_the_pipelines_real_acknowledgement(
    target_config, tmp_path, spec_probe
):
    pipeline = LoopPipeline()
    _code, artifact, text = _run(pipeline, target_config, tmp_path, spec_probe)

    ack = pipeline.outbound[0]
    assert ack["kind"] == "acknowledgement"
    parent = artifact["summary"]["bait_parent"]
    assert parent["source"] == "pipeline-outbound"
    assert parent["message_id"] == ack["message_id"]
    bait_fields = pipeline.received[-1]
    assert _headers_of(bait_fields)["In-Reply-To"] == f"<{ack['message_id']}>"
    assert bait_fields["to"] == REPLY_ADDRESS
    assert "the pipeline's" in text and "own reply" in text


def test_the_ack_is_found_by_threading_when_it_carries_no_tag(
    target_config, tmp_path, spec_probe
):
    pipeline = LoopPipeline(tag_outbound=False)
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    assert artifact["summary"]["bait_parent"]["source"] == "pipeline-outbound"


def test_with_no_acknowledgement_the_bait_parent_is_fabricated_and_says_so(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        LoopPipeline(acknowledge=False), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_OK, text
    parent = artifact["summary"]["bait_parent"]
    assert parent["source"] == "fabricated"
    assert "no reply to the ordinary message was seen" in parent["reason"]
    assert "In-Reply-To is fabricated" in text


def test_a_pipeline_that_answers_the_bait_is_a_distinct_finding(
    target_config, tmp_path, spec_probe
):
    """Only the bait is answered, so only the bait is a finding."""
    code, artifact, text = _run(
        LoopPipeline(answer_loop_bait=True), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["loop_bait_answered"] == 1
    assert artifact["summary"]["auto_replies_emitted"] == 1
    failing = [r for r in _results(artifact) if r["findings"]]
    assert [r["is_loop_bait"] for r in failing] == [True]
    assert "the loop bait was answered, which is a live mail loop" in text


def test_the_bait_threaded_onto_the_acknowledged_ticket_is_not_a_new_ticket(
    target_config, tmp_path, spec_probe
):
    _code, artifact, _text = _run(
        LoopPipeline(ticket_machine_mail=True), target_config, tmp_path, spec_probe
    )
    assert artifact["summary"]["tickets_opened"] == COUNT
    bait = next(r for r in _results(artifact) if r["is_loop_bait"])
    assert bait["ticked"] is False
    assert any("existing ticket" in n for n in bait["notes"])


def test_the_bait_is_caught_even_when_tickets_are_allowed(
    target_config, tmp_path, spec_probe
):
    pipeline = LoopPipeline(answer_loop_bait=True, ticket_machine_mail=True)
    code, _artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, ticket_policy="allowed"
    )
    assert code == EXIT_MISMATCH
    assert "the loop bait was answered" in text


# ---------------------------------------------------------------------------
# Auto-replies, attributed and not
# ---------------------------------------------------------------------------


def test_a_tagged_auto_reply_is_attributed_to_its_message(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        LoopPipeline(auto_reply_machine_mail=True), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["auto_replies_emitted"] == JUDGED
    assert artifact["summary"]["auto_reply_unattributed"] == 0
    assert "an outbound message was emitted" in text


def test_an_untagged_auto_reply_is_outbound_unattributed_and_a_finding(
    target_config, tmp_path, spec_probe
):
    """The false pass this fixes: replies that quote nothing were invisible to
    a lookup by tag, every row read "reply: no" and the verdict was LOOP-SAFE."""
    pipeline = LoopPipeline(auto_reply_machine_mail=True, tag_outbound=False)
    code, artifact, text = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_MISMATCH, text
    summary = artifact["summary"]
    assert summary["verdict"] == "LOOPS-DETECTED"
    assert summary["auto_reply_unattributed"] == JUDGED
    assert any(f.startswith("outbound, unattributed") for f in summary["run_findings"])
    states = {r["reply_state"] for r in _results(artifact)}
    assert states == {tool.REPLY_UNATTRIBUTED}
    assert len(artifact["outbound_unattributed"]) == JUDGED
    assert "outbound, unattributed" in text


def test_an_untagged_reply_that_threads_is_attributed_by_in_reply_to(
    target_config, tmp_path, spec_probe
):
    class Threading(LoopPipeline):
        def _act_on_machine_mail(self, fields):
            if self._marker(fields) is None:
                return False
            self._write_outbound(fields, "auto-reply", "x", quote=True)
            return True

    code, artifact, _text = _run(
        Threading(tag_outbound=False), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["auto_reply_unattributed"] == 0
    assert artifact["summary"]["auto_replies_emitted"] == JUDGED


def test_outbound_from_before_the_run_is_not_counted(target_config, tmp_path, spec_probe):
    path = tmp_path / "loop.json.outbound.jsonl"
    path.write_text(json.dumps({"id": "OLD1", "subject": "earlier mail"}) + "\n",
                    encoding="utf-8")
    code, artifact, text = _run(LoopPipeline(), target_config, tmp_path, spec_probe)
    assert code == EXIT_OK, text
    assert artifact["summary"]["auto_reply_unattributed"] == 0


def test_without_a_sink_the_auto_reply_check_is_not_checked_not_passed(
    target_config, tmp_path, spec_probe
):
    """The pipeline here DOES auto-reply, and the run still says the check did
    not run, because nothing was configured to see it."""
    code, artifact, text = _run(
        LoopPipeline(auto_reply_machine_mail=True), target_config, tmp_path,
        spec_probe, sink=False,
    )
    assert code == EXIT_OK, text
    assert artifact["summary"]["auto_reply_skipped"] == JUDGED
    assert artifact["summary"]["verdict"] == "LOOP-SAFE (auto-reply NOT CHECKED)"
    assert text.splitlines()[0].startswith("loop: LOOP-SAFE (auto-reply NOT CHECKED)")
    assert "no sink configured" in text
    assert "no [loop.outbound] sink is configured" in text
    for result in _results(artifact):
        assert result["replied"] is None


def test_a_sink_that_cannot_enumerate_qualifies_the_verdict(
    target_config, tmp_path, spec_probe
):
    """An outbound adapter with only `fetch` sees tagged replies and nothing
    else, and the headline says the untagged half was not checked."""
    pipeline = LoopPipeline()

    class TagOnly:
        def fetch(self, probe):
            return [
                Readback(exists=True, ticket_id=line["id"], tag=line.get("tag"),
                         message_id=line["message_id"],
                         fields=("ticket_id", "message_id"))
                for line in pipeline.outbound if line.get("tag") == probe.tag
            ]

    spec_probe.tag_only = TagOnly
    code, artifact, _text = _execute(
        pipeline, target_config, tmp_path, spec_probe, "tagonly.json",
        ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:tag_only"),
    )
    assert code == EXIT_OK
    assert artifact["summary"]["outbound_enumerated"] is False
    assert artifact["summary"]["verdict"] == "LOOP-SAFE (untagged auto-replies NOT CHECKED)"


# ---------------------------------------------------------------------------
# Ticketing policy
# ---------------------------------------------------------------------------


def test_a_pipeline_that_tickets_machine_mail_is_caught_by_default(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        LoopPipeline(ticket_machine_mail=True), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["tickets_opened"] == COUNT
    assert "a ticket was opened for machine mail" in text


def test_ticket_policy_allowed_downgrades_tickets_to_informational(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        LoopPipeline(ticket_machine_mail=True), target_config, tmp_path, spec_probe,
        ticket_policy="allowed",
    )
    assert code == EXIT_OK, text
    assert artifact["summary"]["tickets_opened"] == COUNT
    assert artifact["summary"]["findings"] == 0
    assert artifact["config"]["ticket_policy"] == "allowed"


def test_ticket_policy_allowed_keeps_the_auto_reply_finding(
    target_config, tmp_path, spec_probe
):
    pipeline = LoopPipeline(ticket_machine_mail=True, auto_reply_machine_mail=True)
    code, _artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, ticket_policy="allowed"
    )
    assert code == EXIT_MISMATCH
    assert "an outbound message was emitted" in text


# ---------------------------------------------------------------------------
# Interruption
# ---------------------------------------------------------------------------


def test_ctrl_c_writes_a_partial_artifact_and_exits_3(target_config, tmp_path, spec_probe):
    class Interrupted(LoopPipeline):
        def send(self, request):
            if len(self.received) >= 3:
                raise KeyboardInterrupt
            return super().send(request)

    code, artifact, text = _run(Interrupted(), target_config, tmp_path, spec_probe)
    assert code == EXIT_MISMATCH
    assert artifact["aborted"] is True
    assert artifact["summary"]["verdict"] == "ABORTED"
    assert "ABORTED" in text
