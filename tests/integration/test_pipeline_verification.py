"""End to end against a real pipeline, through the real transport.

Every other test in this repository that involves a payload injects a fake
client and asserts on what came back. This file is where the pipeline tools
are judged, and the difference is that here the system under test actually
parses what it receives and actually creates records, so the tests can assert
the claim that matters: a pipeline which is quietly wrong gets caught.

The pattern throughout is the same, and it is the pattern that makes these
tests mean anything:

    pipeline = PipelineUnderTest(drop_attachments=True)
    ... run verify against it ...
    assert the exit code is MISMATCH
    assert the report blames the attachment check

Starting from a correct pipeline and breaking exactly one thing proves that one
thing. A test that started from a broken pipeline and looked for any non-zero
exit would pass against a tool that returned 3 at random, and would keep passing
if the tool stopped checking attachments altogether.

Hermetic: the pipeline is an in-process object and the target URL is a
localhost string that is only ever resolved, never connected to. The
`allow_network` exemption this repository uses elsewhere is not needed and not
taken, which `test_the_pipeline_under_test_module_stays_in_process` enforces.
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from pipeline_under_test import PipelineUnderTest

from testinghq.core import report
from testinghq.pipeline import ledger as ledger_tool
from testinghq.pipeline import redeliver as redeliver_tool
from testinghq.pipeline import verify as verify_tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK

SEED = 21
COUNT = 24
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"

#: The module the run helpers resolve their adapter through. Published per test
#: by the `with_pipeline` fixture, and removed afterwards: a leaked registration
#: would let a later test verify against a stale system with nothing to say why.
SPEC_MODULE_NAME = "testinghq_integration_pipeline"
SPEC = f"{SPEC_MODULE_NAME}:build"


@pytest.fixture
def target_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n', encoding="utf-8")
    return str(path)


@pytest.fixture
def with_pipeline():
    """A factory for pipeline-under-test objects, each with its adapter
    published under `SPEC` for the duration of the test."""

    def make(can_enumerate: bool = True, **kwargs) -> PipelineUnderTest:
        pipeline = PipelineUnderTest(**kwargs)
        module = types.ModuleType(SPEC_MODULE_NAME)
        module.build = lambda config: pipeline.adapter(can_enumerate=can_enumerate)
        sys.modules[SPEC_MODULE_NAME] = module
        return pipeline

    yield make
    sys.modules.pop(SPEC_MODULE_NAME, None)


#: A virtual clock and a sleep that advances it, so pacing costs no real time.
#: Both are needed: a no-op `sleep` handed to a rate limiter that still reads
#: the real clock does not make a test fast, it makes it busy-wait out the real
#: rate in real seconds. That is exactly what happened the first time this file
#: ran: seventy-seven seconds, with every test still reporting that nothing was
#: left to do.
class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


_VIRTUAL = _VirtualClock()


def _readback() -> ReadbackConfig:
    """The adapter config every run helper uses. Built through the real
    `module:attribute` spec path rather than handed an object, so the run
    exercises adapter loading as a user would."""
    return ReadbackConfig(kind="python", spec=SPEC)


def _artifact(tmp_path, name: str) -> dict:
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


def _run_verify(pipeline, config_path, tmp_path, name="verify.json", **kwargs):
    lines: list = []
    code = verify_tool.execute(
        SEED,
        COUNT,
        TARGET,
        config_path,
        str(tmp_path / name),
        readback=_readback(),
        client=pipeline,
        readback_client=pipeline,
        sleep=_VIRTUAL.sleep,
        clock=_VIRTUAL,
        printer=lines.append,
        **kwargs,
    )
    return code, _artifact(tmp_path, name), "\n".join(lines)


def _run_ledger(pipeline, config_path, tmp_path, name="ledger.json", **kwargs):
    lines: list = []
    code = ledger_tool.execute(
        SEED,
        COUNT,
        TARGET,
        config_path,
        str(tmp_path / name),
        readback=_readback(),
        client=pipeline,
        readback_client=pipeline,
        sleep=_VIRTUAL.sleep,
        clock=_VIRTUAL,
        printer=lines.append,
        **kwargs,
    )
    return code, _artifact(tmp_path, name), "\n".join(lines)


def _run_redeliver(pipeline, config_path, tmp_path, name="redeliver.json", **kwargs):
    lines: list = []
    code = redeliver_tool.execute(
        SEED,
        3,
        TARGET,
        config_path,
        str(tmp_path / name),
        readback=_readback(),
        client=pipeline,
        readback_client=pipeline,
        sleep=_VIRTUAL.sleep,
        clock=_VIRTUAL,
        printer=lines.append,
        **kwargs,
    )
    return code, _artifact(tmp_path, name), "\n".join(lines)


# ---------------------------------------------------------------------------
# The baseline: a correct pipeline verifies clean
# ---------------------------------------------------------------------------


def test_a_correct_pipeline_verifies_clean(target_config, tmp_path, with_pipeline):
    """Without this, every other test in the file would pass against a tool
    that reports a failure for everything, which is a real failure mode and has
    happened to this repository before."""
    pipeline = with_pipeline()
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_OK, text
    assert artifact["summary"]["failed"] == 0
    assert artifact["summary"]["verified"] == COUNT
    assert artifact["summary"]["found"] == COUNT
    assert "VERIFIED" in text


def test_a_correct_pipeline_checks_every_one_of_the_six_fields(
    target_config, tmp_path, with_pipeline
):
    """Not a smoke test. A tool that quietly stopped checking the sender would
    keep this file green, and this assertion is the thing that notices."""
    pipeline = with_pipeline()
    _code, artifact, _text = _run_verify(pipeline, target_config, tmp_path)

    passed = artifact["summary"]["checks_passed_by_name"]
    for check in ("ticket_created", "sender", "subject", "body", "routing"):
        assert passed.get(check, 0) > 0, f"{check} never ran"
    assert passed.get("attachments", 0) > 0, (
        "no payload in the verified corpus carried an attachment, so the "
        "attachment check never ran"
    )


def test_a_correct_pipeline_skips_nothing_it_could_have_checked(
    target_config, tmp_path, with_pipeline
):
    pipeline = with_pipeline()
    _code, artifact, _text = _run_verify(pipeline, target_config, tmp_path)

    assert set(artifact["summary"]["checks_skipped_by_name"]) <= {"attachments"}
    for record in artifact["records"]:
        by_check = {c["check"]: c["status"] for c in record["verification"]["checks"]}
        for name in ("ticket_created", "sender", "subject", "body", "routing"):
            assert by_check[name] == "passed", f"{name} was {by_check[name]}"
        # Only the attachment check may skip, and only for a payload with no
        # files to lose.
        wanted = "passed" if record["intended"]["attachments"] else "skipped"
        assert by_check["attachments"] == wanted


def test_the_verified_corpus_actually_contains_attachments():
    """The reason `verify` builds its own corpus. The clean generator never
    attaches anything, so without this the attachment check would be a check
    that never runs, and the sixth field would be decorative."""
    corpus = verify_tool.build_clean_corpus(SEED, COUNT)
    with_files = [e for e in corpus if e.attachments]
    assert with_files, "no verified payload carried an attachment"
    assert len(with_files) < len(corpus), (
        "every payload carries an attachment, so 'no attachment to check' is "
        "never exercised and a skipped check cannot be told from a passing one"
    )


def test_the_verified_corpus_is_reproducible():
    first = [report.payload_sha256(e) for e in verify_tool.build_clean_corpus(SEED, COUNT)]
    second = [report.payload_sha256(e) for e in verify_tool.build_clean_corpus(SEED, COUNT)]
    assert first == second


def test_a_single_verified_payload_does_not_depend_on_how_many_were_generated():
    """The per-payload rng is seeded from (seed, index), not drawn from one
    stream, so index 7 is the same whether it was generated alone or as part
    of forty. A shared stream is the kind of coupling that makes a replay stop
    reproducing a run."""
    alone = verify_tool.build_clean_corpus(SEED, 8)[7]
    among = verify_tool.build_clean_corpus(SEED, COUNT)[7]
    assert report.payload_sha256(alone) == report.payload_sha256(among)


# ---------------------------------------------------------------------------
# Parse defects a status code cannot see
# ---------------------------------------------------------------------------


def test_a_pipeline_that_drops_attachments_is_caught(target_config, tmp_path, with_pipeline):
    """The headline case. A 200 for every payload, a ticket for every payload,
    and the invoice silently gone. No status-code tool can see it."""
    pipeline = with_pipeline(drop_attachments=True)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["failed"] > 0
    assert "attachments" in text
    failing = [
        r for r in artifact["records"]
        if any("attachments" in m for m in r["assertion"]["mismatches"])
    ]
    # Every payload the corpus gave a file to is caught. Not a sample: a
    # partial catch is a partial tool.
    expected = [e for e, _t, _r in verify_tool.build_tagged_corpus(SEED, COUNT) if e.attachments]
    assert len(failing) == len(expected)


def test_a_pipeline_that_truncates_the_body_is_caught(target_config, tmp_path, with_pipeline):
    pipeline = with_pipeline(truncate_body_at=40)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["failed"] == COUNT
    assert "body" in text


def test_a_pipeline_that_loses_the_sender_address_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Keeping the display name and dropping the addr-spec is the classic
    sender parse bug, and the ticket ends up filed under a person rather than
    an address."""
    pipeline = with_pipeline(mangle_sender=True)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["failed"] == COUNT
    assert "sender" in text


def test_a_pipeline_that_mangles_the_subject_is_caught(target_config, tmp_path, with_pipeline):
    pipeline = with_pipeline(mangle_subject=True)
    code, _artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert "subject" in text


def test_a_pipeline_that_files_to_the_wrong_queue_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Routing is the one field only an adapter can see, and a message that
    arrived but went to the wrong place is a lost message as far as the person
    waiting for an answer is concerned."""
    pipeline = with_pipeline(misroute=True)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["failed"] == COUNT
    assert "routing" in text
    assert "queue:unrouted" in text


def test_a_declared_expected_route_overrides_the_recipient_default(
    target_config, tmp_path, with_pipeline
):
    """A pipeline with its own routing taxonomy declares its routes, and the
    check must use the declaration rather than the message's recipient."""
    pipeline = with_pipeline(misroute=True)
    code, _artifact, text = _run_verify(
        pipeline, target_config, tmp_path, route="queue:unrouted"
    )
    assert code == EXIT_OK, text


# ---------------------------------------------------------------------------
# Loss
# ---------------------------------------------------------------------------


def test_a_pipeline_that_silently_drops_messages_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Answers 200 and creates nothing. The most consequential thing any of
    these tools can report, and the one a status code cannot."""
    pipeline = with_pipeline(drop_every_nth=3)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["found"] < COUNT
    assert "no record of this message" in text


def test_a_pipeline_that_loses_a_specific_message_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Not every third: one specific message, by Message-ID, which is what a
    targeted failure looks like rather than a load-related one."""
    from testinghq.pipeline import messages as pipeline_messages

    _email, _tag, record_id = verify_tool.build_tagged_corpus(SEED, COUNT)[4]
    doomed = pipeline_messages.message_id_of(_email)

    pipeline = with_pipeline(drop_message_ids=(doomed,))
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["found"] == COUNT - 1
    assert record_id in text


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_a_pipeline_that_duplicates_a_redelivery_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Two tickets for one message, each announced with a 200. Verify's
    sequence handling is what catches it: a checker that looked only at the
    first record would call a pipeline that files everything twice correct.

    The duplicate arrives because the same corpus is fired twice into the same
    pipeline, which is exactly what a webhook retry looks like from here.
    """
    pipeline = with_pipeline(duplicate_on_redelivery=True)

    first_code, _first, first_text = _run_verify(pipeline, target_config, tmp_path)
    assert first_code == EXIT_OK, first_text

    second_code, _second, second_text = _run_verify(
        pipeline, target_config, tmp_path, name="verify2.json"
    )
    assert second_code == EXIT_MISMATCH
    assert "records for one message" in second_text


# ---------------------------------------------------------------------------
# An adapter that cannot see everything
# ---------------------------------------------------------------------------


def test_a_readback_that_cannot_enumerate_reports_strays_as_not_searched(
    target_config, tmp_path, with_pipeline
):
    """The honest null, not a zero. A ledger that reported 'extra: 0' for a
    question it never asked would be worse than one that says it did not ask.

    The exit code is non-zero too, which is the strict half of the same rule: a
    ledger that exits 0 while knowing it could not check for tickets it never
    sent is a footgun in exactly the pipeline it was bought for."""
    pipeline = with_pipeline(can_enumerate=False)
    code, artifact, text = _run_ledger(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["strays_searched"] is False
    assert artifact["summary"]["extra"] is None
    assert artifact["summary"]["verdict"] == "UNVERIFIED STRAYS"
    assert "NOT SEARCHED" in text
    assert "does not count as balanced" in text


def test_a_readback_that_can_enumerate_searches_for_strays(
    target_config, tmp_path, with_pipeline
):
    pipeline = with_pipeline()
    _code, artifact, _text = _run_ledger(pipeline, target_config, tmp_path)
    assert artifact["summary"]["strays_searched"] is True
    assert artifact["summary"]["extra"] == []


def test_a_correct_pipeline_ledgers_balanced(target_config, tmp_path, with_pipeline):
    """The baseline for the ledger, and the question it exists to answer."""
    pipeline = with_pipeline()
    code, artifact, text = _run_ledger(pipeline, target_config, tmp_path)

    assert code == EXIT_OK, text
    summary = artifact["summary"]
    assert summary["verdict"] == "BALANCED"
    assert summary["sent"] == COUNT
    assert summary["produced"] == COUNT
    assert summary["exactly_once"] == COUNT
    assert summary["missing"] == []
    assert summary["duplicated"] == []


def test_a_correct_pipeline_is_delivery_safe(target_config, tmp_path, with_pipeline):
    """The baseline for the redelivery scenarios. Without it, a tool that
    reported DELIVERY-BUGS for everything would keep the rest of the file
    green."""
    pipeline = with_pipeline()
    code, artifact, text = _run_redeliver(pipeline, target_config, tmp_path)

    assert code == EXIT_OK, text
    assert artifact["summary"]["verdict"] == "DELIVERY-SAFE"
    assert artifact["summary"]["failed"] == 0
    assert [s["scenario"] for s in artifact["scenarios"]] == list(
        redeliver_tool.SCENARIOS
    )


# ---------------------------------------------------------------------------
# An adapter that can only see part of the picture
# ---------------------------------------------------------------------------


def test_a_readback_that_cannot_see_a_field_reports_it_as_not_checked(
    target_config, tmp_path, with_pipeline
):
    """The property that stops a partial integration looking like a clean one.
    An API that returns a ticket list and no bodies is a realistic shape, and
    the report has to say the body was never looked at rather than imply it
    was fine."""
    from testinghq.pipeline import verify as module_under_test
    from testinghq.pipeline.readback import Readback

    pipeline = with_pipeline()
    real_build = module_under_test.build_adapter

    class _SubjectOnly:
        def __init__(self, inner):
            self._inner = inner

        def fetch(self, probe):
            return [
                Readback(
                    exists=True, ticket_id=r.ticket_id, subject=r.subject,
                    fields=("subject",),
                )
                for r in self._inner.fetch(probe)
            ]

        def close(self):
            return None

    module_under_test.build_adapter = lambda config, **kwargs: _SubjectOnly(
        real_build(config, **kwargs)
    )
    try:
        code, artifact, text = _run_verify(pipeline, target_config, tmp_path)
    finally:
        module_under_test.build_adapter = real_build

    assert code == EXIT_OK, text
    assert set(artifact["summary"]["checks_skipped_by_name"]) >= {
        "sender",
        "body",
        "attachments",
        "routing",
    }
    assert "NOT CHECKED" in text


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_a_verify_artifact_is_a_blast_artifact_with_more_on_it(
    target_config, tmp_path, with_pipeline
):
    """So `testinghq compare` reads it, and so anyone already reading a run
    artifact knows the shape. The additions are additive, which is why
    core/report.py needed no change and the shipped blast fixtures are
    untouched."""
    pipeline = with_pipeline(drop_attachments=True)
    _code, artifact, _text = _run_verify(pipeline, target_config, tmp_path)

    for key in ("seed", "config", "summary", "records"):
        assert key in artifact
    record = artifact["records"][0]
    for key in ("id", "category", "payload_sha256", "intended", "response", "assertion"):
        assert key in record
    assert record["category"] == report.CLEAN
    assert record["id"].startswith("clean-")
    assert "verification" in record
    assert "readback" in record
    assert "tag" in record


def test_a_verify_artifact_keeps_its_payload_hashes_stable(
    target_config, tmp_path, with_pipeline
):
    """Replay only works if the hash in the artifact is the hash of the bytes
    that were actually built, so it is recomputed from the payload rather than
    copied from the probe."""
    pipeline = with_pipeline()
    _code, artifact, _text = _run_verify(pipeline, target_config, tmp_path)

    corpus = {tag: email for email, tag, _rid in
              verify_tool.build_tagged_corpus(SEED, COUNT)}
    for record in artifact["records"]:
        assert record["payload_sha256"] == report.payload_sha256(corpus[record["tag"]])


def test_the_report_leads_with_the_answer_and_the_numbers(
    target_config, tmp_path, with_pipeline
):
    pipeline = with_pipeline(misroute=True)
    _code, _artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert text.splitlines()[0].startswith("verify: MISMATCHED")
    assert "found in system:" in text
    assert "mismatched:" in text


def test_the_report_leads_with_not_nothing_checked_when_nothing_ran(
    target_config, tmp_path, with_pipeline
):
    """A run whose adapter could see no fields at all must not read as a
    success. It exits 0, because the operator chose the adapter, but the
    headline has to say so."""
    from testinghq.pipeline import verify as module_under_test
    from testinghq.pipeline.readback import Readback

    pipeline = with_pipeline()
    real_build = module_under_test.build_adapter

    class _Blind:
        def fetch(self, probe):
            return []

        def close(self):
            return None

    module_under_test.build_adapter = lambda config, **kwargs: _Blind()
    try:
        _code, _artifact, text = _run_verify(pipeline, target_config, tmp_path)
    finally:
        module_under_test.build_adapter = real_build

    assert text.splitlines()[0].startswith("verify: MISMATCHED")
    assert "holds no record" in text


# ---------------------------------------------------------------------------
# Threading defects, which only the redelivery scenarios can reach
# ---------------------------------------------------------------------------


def test_a_pipeline_that_splits_a_thread_onto_separate_tickets_is_caught(
    target_config, tmp_path, with_pipeline
):
    """Correct In-Reply-To and References, but the reply is filed as its own
    conversation. It is the outcome that makes an agent open a duplicate ticket
    a fortnight later, and the only reason to check the headers separately is
    that a pipeline can get them right and still do this."""
    pipeline = with_pipeline(split_thread=True)
    code, artifact, text = _run_redeliver(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert artifact["summary"]["verdict"] == "DELIVERY-BUGS"
    assert "its original is on" in text
    # The headers were right, so the link check passed and only the together
    # check failed. That is the distinction that makes the report actionable.
    assert "In-Reply-To is" not in text


def test_a_pipeline_that_drops_threading_headers_is_caught(
    target_config, tmp_path, with_pipeline
):
    pipeline = with_pipeline(drop_threading_headers=True)
    code, _artifact, text = _run_redeliver(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert "In-Reply-To is None" in text


def test_a_correct_pipeline_threads_a_reply_that_arrived_first(
    target_config, tmp_path, with_pipeline
):
    """The positive control for the above two, and the reason reply-first is
    worth a scenario. A pipeline that opens a new conversation for a reply it
    has not yet seen a parent for is the common failure; a pipeline that
    re-files the reply when the parent lands is the correct behaviour, and this
    asserts the tool sees the difference.

    All three messages on one ticket, which is what "threaded" means. Asserted
    on the ticket ids rather than on the verdict, because the verdict is
    already asserted by test_a_correct_pipeline_is_delivery_safe and a
    duplicated assertion would catch nothing extra."""
    pipeline = with_pipeline()
    _code, artifact, _text = _run_redeliver(pipeline, target_config, tmp_path)

    threaded = next(
        s for s in artifact["scenarios"]
        if s["scenario"] == redeliver_tool.SCENARIO_REPLY_FIRST
    )
    tickets_by_tag = {m["tag"]: m["tickets"] for m in threaded["messages"]}
    assert len(tickets_by_tag) == 3
    distinct = {t for tickets in tickets_by_tag.values() for t in tickets}
    assert len(distinct) == 1, (
        f"a correct pipeline put one three-message thread on {len(distinct)} "
        f"tickets: {tickets_by_tag}"
    )


# ---------------------------------------------------------------------------
# The helpers themselves stay honest
# ---------------------------------------------------------------------------


def test_the_pipeline_under_test_module_stays_in_process():
    """Readable guarantee, and the same shape as the existing one in
    test_intake_happy_path.py. If someone later adds a real socket or HTTP
    import here, this fails loudly instead of the integration tests quietly
    becoming the thing this suite has spent its life making impossible."""
    import pipeline_under_test

    with open(pipeline_under_test.__file__, encoding="utf-8") as handle:
        lines = handle.readlines()
    forbidden = ("socket", "urllib", "http.client", "http.server", "requests")
    for line in lines:
        if line.startswith(("import ", "from ")):
            for name in forbidden:
                assert name not in line, f"networking import found: {line!r}"


def test_the_pipeline_under_test_answers_200_for_a_defective_parse():
    """The premise the whole file rests on. If a broken pipeline started
    returning 5xx, every test here would be passing for the wrong reason: a
    status-code tool would have caught the bug, and these tools would look
    redundant rather than necessary."""
    from testinghq.core.transport import post

    pipeline = PipelineUnderTest(drop_attachments=True, mangle_sender=True)
    email, _tag, _rid = verify_tool.build_tagged_corpus(SEED, 1)[0]

    result = post(email, TARGET_URL, client=pipeline)
    assert result.status == 200
    assert pipeline.tickets, "the pipeline created no ticket at all"
    assert pipeline.tickets[0].from_addr != email.ground_truth.from_addr


def test_the_pipeline_under_test_deduplicates_by_message_id_by_default():
    """The other half of the premise. A correct pipeline recognises a
    redelivery by Message-ID, and the duplicate scenarios are worthless if the
    double is not a bug to begin with."""
    from testinghq.core.transport import post

    pipeline = PipelineUnderTest()
    email, _tag, _rid = verify_tool.build_tagged_corpus(SEED, 1)[0]

    post(email, TARGET_URL, client=pipeline)
    post(email, TARGET_URL, client=pipeline)

    assert len(pipeline.tickets) == 1
    assert pipeline.tickets[0].deliveries == 2
