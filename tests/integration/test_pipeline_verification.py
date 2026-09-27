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
from testinghq.pipeline import expectations
from testinghq.pipeline import ledger as ledger_tool
from testinghq.pipeline import verify as verify_tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK
from testinghq.pipeline.readback import Readback

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
    """Publishes a readback factory for the duration of one test.

    `make(**kwargs)` builds a `PipelineUnderTest` and publishes it.
    `publish(obj)` publishes something else that answers `fetch`, for a test
    that wraps or decorates a pipeline rather than configuring one.

    Published under a module object rather than set globally, and removed
    afterwards, because a leaked registration would let a later test verify
    against a stale system with nothing to say why.
    """
    def publish(source, can_enumerate: bool = True) -> None:
        module = types.ModuleType(SPEC_MODULE_NAME)
        if hasattr(source, "adapter"):
            module.build = lambda config: source.adapter(can_enumerate=can_enumerate)
        else:
            module.build = lambda config: source
        sys.modules[SPEC_MODULE_NAME] = module

    def make(can_enumerate: bool = True, **kwargs) -> PipelineUnderTest:
        pipeline = PipelineUnderTest(**kwargs)
        publish(pipeline, can_enumerate=can_enumerate)
        return pipeline

    make.publish = publish
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
    path = tmp_path / name
    if not path.is_file():
        # A tool that refuses writes no artifact. Saying so beats a
        # FileNotFoundError from three frames down, which is what the first
        # version of these helpers did to every refusal.
        raise AssertionError(
            f"no artifact was written to {path}. A refusal prints its reason and "
            f"writes nothing, so the run's own output is the diagnosis."
        )
    return json.loads(path.read_text(encoding="utf-8"))


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
    text = "\n".join(lines)
    try:
        artifact = _artifact(tmp_path, name)
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n--- run output ---\n{text}") from None
    return code, artifact, text


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
    text = "\n".join(lines)
    try:
        artifact = _artifact(tmp_path, name)
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n--- run output ---\n{text}") from None
    return code, artifact, text




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

    # Every reason is always present in the summary, so that a reader can tell a
    # zero from a cause nobody looked for. What matters is which are non-empty.
    grouped = artifact["summary"]["checks_skipped_by_reason"]
    assert set(grouped) == set(expectations.SKIP_REASONS)
    non_empty = {reason for reason, checks in grouped.items() if checks}
    assert non_empty <= {expectations.SKIP_NOTHING_TO_CHECK}, grouped
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
    grouped = artifact["summary"]["checks_skipped_by_reason"]
    not_visible = grouped[expectations.SKIP_NOT_VISIBLE]
    assert set(not_visible) >= {"sender", "body", "attachments", "routing"}
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


class _LateDuplicatePipeline:
    """A pipeline that creates a second ticket, a few polls after the first.

    Wraps the real one rather than reimplementing it, so the multipart parse
    and the ticket model stay the ones under test everywhere else and only the
    timing is new.
    """

    def __init__(self, delay_polls: int = 2) -> None:
        self.inner = PipelineUnderTest(duplicate_on_redelivery=True)
        self.delay_polls = delay_polls
        self._fetches = 0
        self.send = self.inner.send

    def fetch(self, probe):
        self._fetches += 1
        found = self.inner.fetch(probe)
        if self._fetches > self.delay_polls and len(found) == 1:
            # The late second copy. Same Message-ID, so a correct pipeline should
            # never have produced it, which is the whole finding.
            return found + [self._second_copy(found[0])]
        return found

    @staticmethod
    def _second_copy(record: Readback) -> Readback:
        """The late duplicate: the same message, on a second ticket, carrying
        every field the first copy did. A pipeline that produced only this one
        would look perfectly correct to any check that looked once."""
        return Readback(
            exists=True,
            ticket_id=f"{record.ticket_id}-late",
            from_addr=record.from_addr,
            subject=record.subject,
            body=record.body,
            attachment_names=record.attachment_names,
            route=record.route,
            message_id=record.message_id,
            in_reply_to=record.in_reply_to,
            references=record.references,
            tag=record.tag,
            fields=record.fields,
        )

    def list_all(self):
        return self.inner.list_all()

    def close(self):
        return self.inner.close()


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

def test_the_ledger_reports_a_duplicate_that_appeared_late(
    target_config, tmp_path, with_pipeline
):
    """The case a single read after the last send misses.

    Every message is answered with one ticket on the first read, so a readback
    that only asked "did it create anything" would finish there and report a
    balanced ledger. The second ticket lands while the run is still watching.
    The virtual clock advances by the sleep amount, so this costs no real time
    and still exercises the real polling loop.
    """
    pipeline = _LateDuplicatePipeline(delay_polls=2)
    with_pipeline.publish(pipeline)
    code, artifact, text = _run_ledger(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH, text
    duplicated = artifact["summary"]["duplicated"]
    assert len(duplicated) == COUNT, duplicated
    assert all(entry["count"] == 2 for entry in duplicated)
    assert artifact["summary"]["verdict"] == "UNACCOUNTED"
    assert "duplicated" in text
    assert artifact["summary"]["readback"]["stable"] is True
    assert artifact["summary"]["readback"]["polls"] > 2, (
        "the run reported a duplicate without ever looking twice"
    )

def test_a_late_duplicate_is_caught_by_verify_too(
    target_config, tmp_path, with_pipeline
):
    pipeline = _LateDuplicatePipeline(delay_polls=2)
    with_pipeline.publish(pipeline)
    code, artifact, text = _run_verify(pipeline, target_config, tmp_path)

    assert code == EXIT_MISMATCH
    assert any(
        "records for one message" in mismatch
        for record in artifact["records"]
        for mismatch in record["assertion"]["mismatches"]
    )

class _LateDuplicatePipeline:
    """A pipeline that creates a second ticket, a few polls after the first.

    Wraps the real one rather than reimplementing it, so the multipart parse
    and the ticket model stay the ones under test everywhere else and only the
    timing is new.
    """

    def __init__(self, delay_polls: int = 2) -> None:
        self.inner = PipelineUnderTest(duplicate_on_redelivery=True)
        self.delay_polls = delay_polls
        self._fetches = 0
        self.send = self.inner.send

    def fetch(self, probe):
        self._fetches += 1
        found = self.inner.fetch(probe)
        if self._fetches > self.delay_polls and len(found) == 1:
            # The late second copy. Same Message-ID, so a correct pipeline should
            # never have produced it, which is the whole finding.
            return found + [self._second_copy(found[0])]
        return found

    @staticmethod
    def _second_copy(record: Readback) -> Readback:
        """The late duplicate: the same message, on a second ticket, carrying
        every field the first copy did. A pipeline that produced only this one
        would look perfectly correct to any check that looked once."""
        return Readback(
            exists=True,
            ticket_id=f"{record.ticket_id}-late",
            from_addr=record.from_addr,
            subject=record.subject,
            body=record.body,
            attachment_names=record.attachment_names,
            route=record.route,
            message_id=record.message_id,
            in_reply_to=record.in_reply_to,
            references=record.references,
            tag=record.tag,
            fields=record.fields,
        )

    def list_all(self):
        return self.inner.list_all()

    def close(self):
        return self.inner.close()


# ---------------------------------------------------------------------------
# Threading defects, which only the redelivery scenarios can reach
# ---------------------------------------------------------------------------

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


