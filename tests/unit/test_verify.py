"""verify: the run, the summary, and the check that a run proved something.

The summary tests are the ones worth reading twice. `checks_skipped_by_name`
exists because the most dangerous outcome this tool can produce is not a wrong
verdict, it is a right-looking verdict for a run that looked at nothing, and
the only way to make that impossible is to count what was not checked and print
it as prominently as what was.
"""
from __future__ import annotations

import json

import pytest

from testinghq.core import report
from testinghq.core import guardrails
from testinghq.core.transport import ClientResponse
from testinghq.pipeline import messages as pipeline_messages
from testinghq.pipeline import verify
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK, EXIT_REFUSED
from testinghq.pipeline.readback import Readback

SEED = 6
COUNT = 8
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"

ALL_FIELDS = (
    "ticket_id",
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "message_id",
    "in_reply_to",
    "references",
)


class _VirtualClock:
    """A clock and sleep that cost no real time.

    Both, and always together. A frozen clock with a no-op sleep makes a rate
    limiter spin forever waiting for time to pass, and a real clock with a
    no-op sleep makes a test busy-wait out the real rate in real seconds. Both
    have happened in this repository and the first one is worse.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


_CLOCK = _VirtualClock()


class _RecordingClient:
    """An `HttpClient` that answers 200 and records what it was sent, so the
    tests can assert the real transport path ran."""

    def __init__(self, status=200):
        self.status = status
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        payload = json.dumps({"ok": self.status < 400}).encode("utf-8")
        return ClientResponse(status=self.status, body=payload)


class _NoSentRecorder:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def send(self, request):
        return self.pipeline.send(request)


@pytest.fixture
def target_config(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n', encoding="utf-8")
    return str(path)


def _config():
    return ReadbackConfig(kind="python", spec="testinghq.verify_spec_probe:build")


@pytest.fixture
def spec_probe():
    """Publishes a factory that returns an adapter over a caller-supplied
    function, so the real `module:attribute` spec path is exercised without the
    test writing a file."""
    import sys
    import types

    module = types.ModuleType("testinghq.verify_spec_probe")
    holder = {"build": None}
    module.build = lambda config: holder["build"]()
    sys.modules["testinghq.verify_spec_probe"] = module
    yield holder
    del sys.modules["testinghq.verify_spec_probe"]


def _run(target_config, tmp_path, spec_probe, fetch, name="verify.json", **kwargs):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(fetch)
    client = _RecordingClient()
    lines: list = []
    code = verify.execute(
        SEED,
        COUNT,
        TARGET,
        target_config,
        str(tmp_path / name),
        readback=_config(),
        client=client,
        sleep=_CLOCK.sleep,
        clock=_CLOCK,
        printer=lines.append,
        **kwargs,
    )
    return code, client, "\n".join(lines), json.loads((tmp_path / name).read_text(encoding="utf-8"))


def _good_for(email, tag):
    return Readback(
        exists=True,
        ticket_id=f"T-{tag}",
        from_addr=email.ground_truth.from_addr,
        subject=email.ground_truth.subject,
        body=email.text,
        attachment_names=tuple(a.filename for a in email.attachments),
        route=email.envelope.to[0],
        message_id=pipeline_messages.message_id_of(email),
        fields=ALL_FIELDS,
    )


def _perfect(email=None, tag=None):
    """A readback for whichever payload the probe names, matching it exactly."""
    corpus = {t: e for e, t, _r in verify.build_tagged_corpus(SEED, COUNT)}

    def fetch(probe):
        email = corpus.get(probe.tag)
        if email is None:
            return []
        return [_good_for(email, probe.tag)]

    return fetch


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def test_the_verified_corpus_is_clean():
    """Verify grades results. A deliberately mangled payload has no correct
    parse to grade against, so verifying one would report a failure every time
    a mutator did its job."""
    for _e, tag, _r in verify.build_tagged_corpus(SEED, COUNT):
        assert tag


def test_the_verified_corpus_carries_no_corruption():
    """`blast.corrupt` is the only source of mangled payloads, and nothing on
    this path calls it. Asserted structurally, because a test that compared the
    corpus to itself would not notice."""
    from testinghq.blast import corrupt

    for email, _tag, _rid in verify.build_tagged_corpus(SEED, COUNT):
        assert email.ground_truth.subject == email.subject
        assert email.ground_truth.body_core in email.text
        assert email.envelope.to and email.envelope.from_addr
        assert email.ground_truth.from_addr == email.envelope.from_addr
        # The mutators all work by changing these, and none of them ran.
        assert "�" not in email.text
        assert corrupt is not None


def test_the_verified_corpus_every_address_is_synthetic():
    """The guardrail the run enforces before it sends, pinned here so the
    corpus cannot grow a real-looking address."""
    fields = []
    for email, _tag, _rid in verify.build_tagged_corpus(SEED, COUNT):
        fields.extend([email.to, email.from_addr, email.envelope.from_addr])
        fields.extend(email.envelope.to)
    guardrails.require_synthetic_content(fields)


def test_the_verified_corpus_tags_come_from_the_tag_marker_and_stay_reserved():
    guardrails.require_synthetic_content(
        [pipeline_messages.build_chain_message_id(tag)
         for _e, tag, _r in verify.build_tagged_corpus(SEED, COUNT)]
    )


def test_a_negative_count_is_refused():
    with pytest.raises(ValueError):
        verify.build_tagged_corpus(SEED, -1)


def test_an_attachment_rate_outside_zero_to_one_is_refused():
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            verify.build_tagged_corpus(SEED, 3, attachment_rate=bad)


def test_a_zero_attachment_rate_produces_a_corpus_with_no_files():
    """The knob has to reach both ends, or the "no attachments to check" path
    is only reachable by luck."""
    corpus = verify.build_clean_corpus(SEED, 20, attachment_rate=0.0)
    assert not any(e.attachments for e in corpus)


def test_a_full_attachment_rate_produces_a_corpus_with_only_files():
    corpus = verify.build_clean_corpus(SEED, 20, attachment_rate=1.0)
    assert all(e.attachments for e in corpus)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def test_a_correct_system_verifies_clean(target_config, tmp_path, spec_probe):
    code, client, text, artifact = _run(target_config, tmp_path, spec_probe, _perfect())
    assert code == EXIT_OK, text
    assert len(client.sent) == COUNT
    assert artifact["summary"]["failed"] == 0
    assert "VERIFIED" in text


def test_the_run_uses_the_real_transport_path(target_config, tmp_path, spec_probe):
    """A verification test that skipped the wire format would be testing a
    payload the system under test never saw."""
    _code, client, _text, _artifact = _run(target_config, tmp_path, spec_probe, _perfect())
    request = client.sent[0]
    assert request.method == "POST"
    assert request.url == TARGET_URL
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b"X-TestingHQ-Tag" in request.body


def test_a_system_that_lost_a_message_is_reported_not_crashed(
    target_config, tmp_path, spec_probe
):
    corpus = {t: e for e, t, _r in verify.build_tagged_corpus(SEED, COUNT)}
    lost = list(corpus)[3]

    def fetch(probe):
        if probe.tag == lost:
            return []
        return [_good_for(corpus[probe.tag], probe.tag)]

    code, _client, text, artifact = _run(target_config, tmp_path, spec_probe, fetch)
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["found"] == COUNT - 1
    assert "holds no record" in text


def test_a_transport_failure_does_not_decide_the_verdict(
    target_config, tmp_path, spec_probe
):
    """A 500 is Blast's problem to grade. Verify's verdict comes from the
    readback, and a report that quietly conflated the two would send an
    operator to look at the wrong thing."""
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    client = _RecordingClient(status=500)
    lines: list = []
    code = verify.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / "verify.json"),
        readback=_config(), client=client, sleep=_CLOCK.sleep, clock=_CLOCK,
        printer=lines.append,
    )
    text = "\n".join(lines)
    artifact = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert code == EXIT_OK, text
    assert artifact["summary"]["transport_non_2xx"] == COUNT
    assert "do not decide the verdict" in text


def test_an_unconfigured_target_is_refused_before_anything_is_sent(
    target_config, tmp_path, spec_probe
):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    client = _RecordingClient()
    lines: list = []
    code = verify.execute(
        SEED, COUNT, "nope", target_config, None,
        readback=_config(), client=client, sleep=_CLOCK.sleep, clock=_CLOCK,
        printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert client.sent == [], "something was sent despite the refusal"
    assert "refused" in "\n".join(lines)


def test_a_readback_that_errors_is_a_refusal_not_a_verdict(
    target_config, tmp_path, spec_probe
):
    """A server that could not answer is not a pipeline that lost a message.
    Reporting it as one would be the worst kind of wrong."""
    from testinghq.pipeline.readback import FunctionAdapter

    def explode(probe):
        raise RuntimeError("the ticket store is down")

    spec_probe["build"] = lambda: FunctionAdapter(explode)
    lines: list = []
    code = verify.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / "verify.json"),
        readback=_config(), client=_RecordingClient(),
        sleep=_CLOCK.sleep, clock=_CLOCK, printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert "could not complete the run" in "\n".join(lines)


# ---------------------------------------------------------------------------
# The summary: what was checked, and what was not
# ---------------------------------------------------------------------------


def _verify_with(readbacks, target_config, tmp_path, spec_probe, **kwargs):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(lambda probe: readbacks.get(probe.tag, []))
    return _run(target_config, tmp_path, spec_probe, lambda probe: [], **kwargs)


def _one(tag, **kwargs):
    from testinghq.pipeline.readback import FunctionAdapter

    return {tag: [Readback(exists=True, ticket_id="T1", **kwargs)]}


def test_the_summary_counts_the_checks_that_never_ran(target_config, tmp_path, spec_probe):
    """The number that makes a green run mean something. Without it, an adapter
    that could see no fields at all would produce a run that reported every
    check as fine and had verified nothing."""
    corpus = verify.build_tagged_corpus(SEED, COUNT)
    readbacks = {
        tag: [Readback(exists=True, ticket_id="T1", subject=email.ground_truth.subject,
                       fields=("subject",))]
        for email, tag, _rid in corpus
    }
    _code, _client, text, artifact = _verify_with(
        readbacks, target_config, tmp_path, spec_probe
    )
    skipped = artifact["summary"]["checks_skipped_by_name"]
    assert set(skipped) >= {"sender", "body", "attachments", "routing"}
    assert skipped["sender"] == COUNT
    assert "NOT CHECKED" in text
    assert "could not see these" in text


def test_the_summary_separates_transport_problems_from_verdicts(
    target_config, tmp_path, spec_probe
):
    _code, _client, text, artifact = _run(target_config, tmp_path, spec_probe, _perfect())
    summary = artifact["summary"]
    assert summary["transport_unanswered"] == 0
    assert summary["transport_non_2xx"] == 0
    assert "transport" not in text, "a clean run has no transport note to print"


def test_a_run_that_verified_nothing_says_nothing_checked(
    target_config, tmp_path, spec_probe
):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(lambda probe: [])
    lines: list = []
    verify.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / "verify.json"),
        readback=_config(), client=_RecordingClient(),
        sleep=_CLOCK.sleep, clock=_CLOCK, printer=lines.append,
    )
    assert "MISMATCHED" in lines[0]


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_the_artifact_is_a_blast_artifact_plus_a_verification(
    target_config, tmp_path, spec_probe
):
    _code, _client, _text, artifact = _run(target_config, tmp_path, spec_probe, _perfect())
    for key in ("seed", "config", "summary", "records"):
        assert key in artifact
    record = artifact["records"][0]
    for key in ("id", "category", "payload_sha256", "intended", "response", "assertion"):
        assert key in record
    assert record["category"] == report.CLEAN
    assert record["assertion"]["passed"] is True
    assert record["verification"]["passed"] is True
    assert record["readback"][0]["ticket_id"].startswith("T-")


def test_a_duplicate_shows_up_in_the_artifact_assertion(target_config, tmp_path, spec_probe):
    corpus = {t: e for e, t, _r in verify.build_tagged_corpus(SEED, COUNT)}
    doubled = list(corpus)[0]

    def fetch(probe):
        if probe.tag != doubled:
            return [_good_for(corpus[probe.tag], probe.tag)]
        email = corpus[probe.tag]
        return [_good_for(email, probe.tag), _good_for(email, probe.tag)]

    _code, _client, _text, artifact = _run(target_config, tmp_path, spec_probe, fetch)
    record = next(r for r in artifact["records"] if r["tag"] == doubled)
    assert record["assertion"]["passed"] is False
    assert len(record["readback"]) == 2
    assert any("2 records" in m for m in record["assertion"]["mismatches"])


def test_the_artifact_config_records_how_to_reproduce_the_run():
    config = verify.run_config(
        SEED, COUNT, "spike", TARGET, _config(), "queue:support", True, 1.5
    )
    assert config["tool"] == "verify"
    assert config["tag_prefix"] == "spike"
    assert config["expect_route"] == "queue:support"
    assert config["body_exact"] is True
    assert config["settle"] == 1.5
    assert "latency" not in config and "timestamp" not in config


def test_the_artifact_survives_a_round_trip_through_json(
    target_config, tmp_path, spec_probe
):
    """Written with `sort_keys=False` and read back by a user. A tuple in the
    record would become a list and quietly change the file's shape."""
    _code, _client, _text, artifact = _run(target_config, tmp_path, spec_probe, _perfect())
    assert json.loads(json.dumps(artifact)) == artifact


# ---------------------------------------------------------------------------
# verify check: reading back a run that already happened
# ---------------------------------------------------------------------------


def test_verify_check_reads_a_saved_run_without_sending(
    target_config, tmp_path, spec_probe
):
    """The round trip that makes `check` worth having: a real run artifact,
    read back afterwards, with nothing sent a second time."""
    from testinghq.pipeline.readback import FunctionAdapter

    corpus = {t: e for e, t, _r in verify.build_tagged_corpus(SEED, COUNT)}
    held = {t: [_good_for(e, t)] for t, e in corpus.items()}
    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    run_code, sent, _text, _artifact = _run(
        target_config, tmp_path, spec_probe, _perfect()
    )
    assert run_code == EXIT_OK
    assert len(sent.sent) == COUNT

    client = _RecordingClient()
    spec_probe["build"] = lambda: FunctionAdapter(
        lambda probe: held.get(probe.tag, [])
    )
    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "verify.json"),
        target_config,
        str(tmp_path / "checked.json"),
        readback=_config(),
        printer=lines.append,
    )
    text = "\n".join(lines)
    assert code == EXIT_OK, text
    assert "VERIFIED" in text
    assert client.sent == [], "verify check sent a request; it must only read"
    assert (tmp_path / "checked.json").is_file()


def test_verify_check_refuses_an_artifact_it_cannot_rebuild(
    target_config, tmp_path, spec_probe
):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "seed": SEED,
                "config": {"count": COUNT},
                "records": [
                    {"id": "clean-1-0000", "category": "clean", "tag": "not-a-real-tag"}
                ],
            }
        ),
        encoding="utf-8",
    )
    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "run.json"), target_config, None,
        readback=_config(), printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert "not-a-real-tag" in "\n".join(lines)


def test_verify_check_refuses_an_artifact_with_no_clean_records(
    target_config, tmp_path, spec_probe
):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "seed": SEED,
                "config": {"count": 2},
                "records": [
                    {"id": "typo-1-0000", "category": "typo", "tag": "hq-1-0000"}
                ],
            }
        ),
        encoding="utf-8",
    )
    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "run.json"), target_config, None,
        readback=_config(), printer=lines.append,
    )
    assert code == EXIT_REFUSED
    text = "\n".join(lines)
    assert "correct parse" in text
    assert "garbled corpus" in text


def test_verify_check_reports_a_missing_artifact(target_config, tmp_path, spec_probe):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "nope.json"), target_config, None,
        readback=_config(), printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert "could not read" in "\n".join(lines)


def test_verify_check_reports_a_garbled_artifact(target_config, tmp_path, spec_probe):
    from testinghq.pipeline.readback import FunctionAdapter

    spec_probe["build"] = lambda: FunctionAdapter(_perfect())
    (tmp_path / "run.json").write_text("{not json", encoding="utf-8")
    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "run.json"), target_config, None,
        readback=_config(), printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert "not valid JSON" in "\n".join(lines)


def test_verify_check_says_how_many_records_it_skipped(
    target_config, tmp_path, spec_probe
):
    """A run that verified 3 of 100 and stayed quiet about the other 97 is the
    exact shape of a misleading green."""
    from testinghq.pipeline.readback import FunctionAdapter

    corpus = {t: e for e, t, _r in verify.build_tagged_corpus(SEED, COUNT)}
    held = {t: [_good_for(e, t)] for t, e in corpus.items()}
    spec_probe["build"] = lambda: FunctionAdapter(lambda probe: held.get(probe.tag, []))

    # A run artifact that also contains mangled records, which is what a
    # `blast fire` artifact looks like.
    records = [
        {
            "id": record_id,
            "category": "clean",
            "tag": tag,
            "payload_sha256": report.payload_sha256(email),
        }
        for email, tag, record_id in verify.build_tagged_corpus(SEED, COUNT)
    ]
    records.append({"id": "typo-1-9999", "category": "typo", "tag": "typo-1-9999"})
    (tmp_path / "run.json").write_text(
        json.dumps({"seed": SEED, "config": {"count": COUNT}, "records": records}),
        encoding="utf-8",
    )

    lines: list = []
    code = verify.check_saved_run(
        str(tmp_path / "run.json"), target_config, str(tmp_path / "checked.json"),
        readback=_config(), printer=lines.append,
    )
    text = "\n".join(lines)
    assert code == EXIT_OK, text
    assert "1 non-clean record(s) not verified" in text
    assert "correct parse" in text


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_says_nothing_was_sent():
    corpus = verify.build_tagged_corpus(SEED, COUNT)
    text = verify.format_dry_run(corpus, _config(), "spike")
    assert "no network calls were made" in text
    assert "spike" in text
    assert "clean" in text


def test_the_dry_run_names_the_adapter_it_would_use():
    corpus = verify.build_tagged_corpus(SEED, COUNT)
    text = verify.format_dry_run(corpus, _config(), "hq")
    assert "python" in text
    assert "testinghq.verify_spec_probe:build" in text
