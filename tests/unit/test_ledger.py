"""The accounting: what "exactly once" is allowed to mean.

The accounting function is pure, so these tests drive it directly with lists
of what was sent and what the system held. That is the point of keeping it
pure: the interesting cases are the ones a fake pipeline will not reproduce on
demand, like a system that answered for message 3 and not message 4, and they
have to be reachable without arranging a pipeline to misbehave in exactly that
shape.

The three properties that matter, and that a counting tool gets wrong by
default:

  a message with no record is MISSING, not zero and not a pass
  a message with two records is DUPLICATED, and never quietly counted as one
  a message with one record that is wrong is MISPARSE, not exactly-once
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from testinghq.core.transport import ClientResponse, TransportResult
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK, EXIT_REFUSED
from testinghq.pipeline import ledger
from testinghq.pipeline.common import SentMessage
from testinghq.pipeline.messages import build_chain_message_id, make_tag
from testinghq.pipeline.readback import Probe, Readback
from testinghq.pipeline.verify import build_tagged_corpus

SEED = 4
COUNT = 3

#: The poll settings a run recorded. What matters to these tests is
#: that the key exists and is passed through, not the numbers.
_POLL = {"quiet_window": 5.0, "max_wait": 60.0, "poll_interval": 0.5}


def _sent(count=COUNT, seed=SEED):
    corpus = build_tagged_corpus(seed, count)
    sent = []
    for index, (email, tag, record_id) in enumerate(corpus):
        sent.append(
            SentMessage(
                index=index,
                record_id=record_id,
                tag=tag,
                email=email,
                probe=Probe(
                    record_id=record_id,
                    tag=tag,
                    payload_sha256="a" * 64,
                    from_addr=email.ground_truth.from_addr,
                    subject=email.ground_truth.subject,
                    recipient=email.envelope.to[0],
                    message_id=build_chain_message_id(tag),
                ),
                result=TransportResult(status=200, latency_ms=1.0, body_snippet="ok"),
            )
        )
    return sent


def _good(message: SentMessage, ticket_id="T1") -> Readback:
    return Readback(
        exists=True,
        ticket_id=ticket_id,
        from_addr=message.email.ground_truth.from_addr,
        subject=message.email.ground_truth.subject,
        body=message.email.text,
        attachment_names=tuple(a.filename for a in message.email.attachments),
        route=message.email.envelope.to[0],
        tag=message.tag,
        fields=("from_addr", "subject", "body", "attachment_names", "route"),
    )


# ---------------------------------------------------------------------------
# The clean case
# ---------------------------------------------------------------------------


def test_a_perfectly_ordered_pipeline_balances():
    sent = _sent()
    readbacks = {m.tag: [_good(m, f"T{i}")] for i, m in enumerate(sent)}
    accounting = ledger.account(sent, readbacks, strays=[])

    assert accounting["balanced"] is True
    assert ledger.verdict(accounting) == "BALANCED"
    assert accounting["sent"] == COUNT
    assert accounting["produced"] == COUNT
    assert accounting["exactly_once"] == COUNT
    assert accounting["missing"] == []
    assert accounting["duplicated"] == []
    assert accounting["wrong"] == []
    assert accounting["extra"] == []


def test_the_accounting_is_keyed_by_tag_not_by_position():
    """The property that stops a ledger manufacturing confident nonsense. A
    system that answered for message 3 and not message 4 must not shift every
    later result by one."""
    sent = _sent()
    readbacks = {
        m.tag: ([_good(m)] if m.tag != sent[1].tag else []) for m in sent
    }
    accounting = ledger.account(sent, readbacks, strays=[])
    assert accounting["missing"] == [sent[1].tag]
    assert accounting["exactly_once"] == 2


# ---------------------------------------------------------------------------
# Loss
# ---------------------------------------------------------------------------


def test_a_lost_message_is_missing_and_never_a_pass():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    readbacks[sent[2].tag] = []
    accounting = ledger.account(sent, readbacks, strays=[])

    assert accounting["missing"] == [sent[2].tag]
    assert accounting["exactly_once"] == COUNT - 1
    assert accounting["balanced"] is False
    assert ledger.verdict(accounting) == "UNACCOUNTED"


def test_a_message_the_system_never_heard_of_is_missing():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    del readbacks[sent[0].tag]
    accounting = ledger.account(sent, readbacks, strays=[])
    assert accounting["missing"] == [sent[0].tag]


def test_a_whole_lost_run_is_still_reported_as_counts_not_a_crash():
    """The under-load case: everything lost. A tool that raised here would be
    no use at exactly the moment it is needed."""
    sent = _sent()
    accounting = ledger.account(sent, {}, strays=[])
    assert len(accounting["missing"]) == COUNT
    assert accounting["produced"] == 0
    assert accounting["balanced"] is False


# ---------------------------------------------------------------------------
# Duplication
# ---------------------------------------------------------------------------


def test_a_message_with_two_records_is_duplicated_not_counted_once():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    readbacks[sent[1].tag] = [_good(sent[1], "T1"), _good(sent[1], "T2")]
    accounting = ledger.account(sent, readbacks, strays=[])

    entry = accounting["duplicated"][0]
    assert entry["tag"] == sent[1].tag
    assert entry["count"] == 2
    assert entry["tickets"] == ["T1", "T2"]
    assert accounting["exactly_once"] == COUNT - 1
    assert ledger.verdict(accounting) == "UNACCOUNTED"


def test_a_duplicate_is_also_excluded_from_exactly_once():
    """Not "included anyway because one of them was right". A pipeline that
    files every message twice is not a pipeline that filed every message
    once."""
    sent = _sent()
    readbacks = {m.tag: [_good(m, f"T{i}a"), _good(m, f"T{i}b")] for i, m in enumerate(sent)}
    accounting = ledger.account(sent, readbacks, strays=[])
    assert accounting["exactly_once"] == 0
    assert len(accounting["duplicated"]) == COUNT


def test_produced_counts_every_record_not_every_message():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    readbacks[sent[0].tag] = [_good(sent[0], "T1"), _good(sent[0], "T2")]
    accounting = ledger.account(sent, readbacks, strays=[])
    assert accounting["sent"] == COUNT
    assert accounting["produced"] == COUNT + 1


# ---------------------------------------------------------------------------
# Misparse
# ---------------------------------------------------------------------------


def test_one_wrong_record_is_misparse_and_not_exactly_once():
    """Exactly once and wrong are different bugs with different fixes, and
    counting a wrong ticket as exactly-once would tell an operator to stop
    looking."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    mangled = Readback(
        exists=True, ticket_id="T9", subject="not the subject", fields=("subject",)
    )
    readbacks[sent[0].tag] = [mangled]
    accounting = ledger.account(sent, readbacks, strays=[])

    assert len(accounting["wrong"]) == 1
    assert accounting["wrong"][0]["tag"] == sent[0].tag
    assert accounting["wrong"][0]["mismatches"]
    assert accounting["exactly_once"] == COUNT - 1
    assert ledger.verdict(accounting) == "MISPARSED"


def test_a_misparsed_run_is_not_reported_as_unaccounted():
    """The counts add up. Nothing is lost and nothing is duplicated, so
    "UNACCOUNTED" would send the reader looking for the wrong thing."""
    sent = _sent()
    readbacks = {
        m.tag: [Readback(exists=True, ticket_id="T", subject="wrong", fields=("subject",))]
        for m in sent
    }
    accounting = ledger.account(sent, readbacks, strays=[])
    assert accounting["missing"] == [] and accounting["duplicated"] == []
    assert ledger.verdict(accounting) == "MISPARSED"
    assert accounting["balanced"] is False


# ---------------------------------------------------------------------------
# Strays
# ---------------------------------------------------------------------------


def test_a_record_with_this_runs_prefix_that_was_never_sent_is_extra():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    stray_tag = make_tag("hq", SEED, 99)
    strays = readbacks[sent[0].tag] + [
        Readback(exists=True, ticket_id="STRAY", tag=stray_tag)
    ]
    accounting = ledger.account(sent, readbacks, strays=strays, tag_prefix="hq")
    assert accounting["extra"] == [{"tag": stray_tag, "ticket": "STRAY"}]
    assert accounting["extra_count"] == 1
    assert accounting["balanced"] is False
    assert ledger.verdict(accounting) == "UNACCOUNTED"


def test_another_runs_tag_is_listed_but_does_not_unbalance_the_ledger():
    """Two concurrent runs with different prefixes must not report each
    other's tickets as strays."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    other = make_tag("hq-2610151200-ab12", SEED, 0)
    strays = [r for m in sent for r in readbacks[m.tag]] + [
        Readback(exists=True, ticket_id="OTHER", tag=other)
    ]
    accounting = ledger.account(sent, readbacks, strays=strays, tag_prefix="hq-2610151230-cd34")
    assert accounting["extra"] == []
    assert accounting["other_runs"] == [{"tag": other, "ticket": "OTHER"}]
    assert accounting["other_runs_count"] == 1
    assert accounting["balanced"] is True


def test_an_untagged_record_is_not_a_stray():
    """A store holds tickets TestingHQ never touched. They are not strays, or
    BALANCED would only be reachable on an empty store."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    strays = [r for m in sent for r in readbacks[m.tag]] + [
        Readback(exists=True, ticket_id="CUSTOMER-1", tag=None),
        Readback(exists=True, ticket_id="CUSTOMER-2", tag="billing"),
    ]
    accounting = ledger.account(sent, readbacks, strays=strays)
    assert accounting["extra"] == []
    assert accounting["untagged_count"] == 2
    assert ledger.verdict(accounting) == "BALANCED"


def test_this_runs_own_tickets_are_not_strays_when_the_store_returns_no_tag():
    """Most ticket APIs have no top-level tag field. The run's own tickets are
    recognised by ticket id, so they cannot be counted as extra."""
    sent = _sent()
    readbacks = {m.tag: [_good(m, f"T{i}")] for i, m in enumerate(sent)}
    enumerated = [
        Readback(exists=True, ticket_id=f"T{i}", subject=m.email.subject, tag=None)
        for i, m in enumerate(sent)
    ]
    accounting = ledger.account(sent, readbacks, strays=enumerated)
    assert accounting["extra"] == []
    assert accounting["untagged_count"] == 0
    assert accounting["balanced"] is True


def test_a_stray_tag_is_recovered_from_the_stored_body():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    lost_tag = make_tag("hq", SEED, 42)
    strays = [Readback(
        exists=True, ticket_id="T-BODY", tag=None,
        body=f"hello\n[testinghq:{lost_tag}]\n",
    )]
    accounting = ledger.account(sent, readbacks, strays=strays, tag_prefix="hq")
    assert accounting["extra"] == [{"tag": lost_tag, "ticket": "T-BODY"}]


def test_the_extra_list_is_capped_with_an_exact_count():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    total = ledger.MAX_ARTIFACT_STRAYS + 7
    strays = [
        Readback(exists=True, ticket_id=f"S{i}", tag=make_tag("hq", SEED, 1000 + i))
        for i in range(total)
    ]
    accounting = ledger.account(sent, readbacks, strays=strays)
    assert len(accounting["extra"]) == ledger.MAX_ARTIFACT_STRAYS
    assert accounting["extra_count"] == total


def test_expect_route_is_honoured_by_the_accounting():
    """The routing check runs only when a route is declared, and then it
    decides MISPARSED like any other field."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    assert ledger.account(sent, readbacks, strays=[])["wrong"] == []

    misrouted = ledger.account(sent, readbacks, strays=[], route="queue:support")
    assert len(misrouted["wrong"]) == COUNT
    assert all("routing" in w["mismatches"][0] for w in misrouted["wrong"])

    routed = {
        m.tag: [Readback(
            exists=True, ticket_id="T1", from_addr=m.email.ground_truth.from_addr,
            subject=m.email.ground_truth.subject, body=m.email.text,
            attachment_names=tuple(a.filename for a in m.email.attachments),
            route="queue:support", tag=m.tag,
            fields=("from_addr", "subject", "body", "attachment_names", "route"),
        )]
        for m in sent
    }
    assert ledger.account(sent, routed, strays=[], route="queue:support")["balanced"]


def test_the_subject_match_mode_reaches_the_accounting():
    sent = _sent()
    readbacks = {}
    for m in sent:
        good = _good(m)
        readbacks[m.tag] = [Readback(
            exists=True, ticket_id="T1", from_addr=good.from_addr,
            subject=f"[Ticket #1] {good.subject}", body=good.body,
            attachment_names=good.attachment_names, tag=m.tag,
            fields=("from_addr", "subject", "body", "attachment_names"),
        )]
    assert ledger.account(sent, readbacks, strays=[])["balanced"] is True
    exact = ledger.account(sent, readbacks, strays=[], subject_match="exact")
    assert len(exact["wrong"]) == COUNT


def test_an_unsettled_readback_reports_missing_as_unsettled_not_lost():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    readbacks[sent[0].tag] = []
    accounting = ledger.account(sent, readbacks, strays=[], unsettled=True)
    assert ledger.verdict(accounting) == "UNSETTLED"
    assert accounting["balanced"] is False
    duplicated = dict(readbacks)
    duplicated[sent[1].tag] = [_good(sent[1], "A"), _good(sent[1], "B")]
    assert ledger.verdict(
        ledger.account(sent, duplicated, strays=[], unsettled=True)
    ) == "UNACCOUNTED"


def test_strays_that_are_none_is_not_the_same_as_strays_that_are_empty():
    """The distinction the whole design rests on. `None` means the adapter
    could not ask; `[]` means it asked and found nothing. Reporting zero for a
    question never asked would be the worst kind of wrong."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}

    unsearched = ledger.account(sent, readbacks, strays=None)
    assert unsearched["extra"] is None
    assert unsearched["strays_searched"] is False

    searched = ledger.account(sent, readbacks, strays=[])
    assert searched["extra"] == []
    assert searched["strays_searched"] is True

    assert searched["balanced"] is True
    assert ledger.verdict(searched) == "BALANCED"


def test_a_run_that_could_not_search_for_strays_is_not_reported_as_balanced():
    """The strict rule, and the reason for it. A ledger that exits 0 while
    knowing it could not check for tickets it never sent is a footgun in
    exactly the pipeline it was bought for. The operator who wants a CI gate
    gives the adapter a way to enumerate; everyone else gets a qualified
    report and a non-zero exit."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    unsearched = ledger.account(sent, readbacks, strays=None)

    assert unsearched["missing"] == [] and unsearched["duplicated"] == []
    assert unsearched["wrong"] == []
    assert unsearched["balanced"] is False
    assert ledger.verdict(unsearched) == "UNVERIFIED STRAYS"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _artifact(accounting):
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    return ledger.build_artifact(
        SEED,
        ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent,
        accounting,
        readbacks,
    )


def _readback_config():
    return ReadbackConfig(kind="http", url="http://localhost:8000/tickets")


def test_the_report_prints_every_section_even_when_there_is_nothing_wrong():
    """A report that only lists problems cannot be distinguished from one that
    found none, and "confirm we lost nothing" is as often the question as
    "tell me what we lost"."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent, ledger.account(sent, readbacks, strays=[]), readbacks,
    ))

    assert text.splitlines()[0].startswith("ledger: BALANCED")
    for section in ("missing: none", "duplicated: none", "misparsed: none"):
        assert section in text


def test_the_report_says_when_strays_were_not_searched():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent, ledger.account(sent, readbacks, strays=None), readbacks,
    ))
    assert "NOT SEARCHED" in text
    assert "this adapter cannot enumerate" in text


def test_a_balanced_run_with_strays_searched_says_what_it_knows():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent, ledger.account(sent, readbacks, strays=[]), readbacks,
    ))
    assert "nothing else was found" in text


def test_a_run_with_unsearched_strays_says_it_does_not_count_as_balanced():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent, ledger.account(sent, readbacks, strays=None), readbacks,
    ))
    assert text.splitlines()[0].startswith("ledger: UNVERIFIED STRAYS")
    assert "does not count as balanced" in text


def test_the_report_truncates_detail_lists_but_not_counts():
    """A run where twenty-five messages were lost is one finding, not
    twenty-five, and a terminal nobody can read is a terminal nobody reads.
    The count is still exact."""
    total = ledger.MAX_LISTED + 5
    sent = _sent(total)
    readbacks = {m.tag: [] for m in sent}
    accounting = ledger.account(sent, readbacks, strays=[])
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, total, "hq", "local", _readback_config(), None, _POLL),
        sent, accounting, readbacks,
    ))
    assert re.search(rf"missing:\s+{total}$", text, re.MULTILINE), text
    assert f"... and {total - ledger.MAX_LISTED} more, not shown" in text
    # The listed detail is capped, the count is not.
    assert text.count("sent, and the system holds nothing") == ledger.MAX_LISTED


def test_the_artifact_keeps_a_blast_shaped_record_per_message():
    sent = _sent()
    readbacks = {m.tag: [_good(m, "T7")] for m in sent}
    artifact = ledger.build_artifact(
        SEED,
        ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, _POLL),
        sent,
        ledger.account(sent, readbacks, strays=[]),
        readbacks,
    )
    for key in ("seed", "config", "summary", "records"):
        assert key in artifact
    record = artifact["records"][0]
    for key in ("id", "category", "tag", "payload_sha256", "intended", "response"):
        assert key in record
    assert record["tickets"] == ["T7"]
    assert record["ticket_count"] == 1
    assert artifact["summary"]["verdict"] == "BALANCED"


def test_the_artifact_config_records_how_to_reproduce_the_run():
    config = ledger.run_config(
        SEED, COUNT, "spike", "local", _readback_config(), "queue:support", _POLL
    )
    assert config["tool"] == "ledger"
    assert config["seed"] == SEED
    assert config["tag_prefix"] == "spike"
    assert config["expect_route"] == "queue:support"
    assert config["readback_poll"] == _POLL
    # Nothing that varies between two runs of the same command, so the same
    # inputs always rebuild the same corpus.
    assert "latency" not in config and "timestamp" not in config


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_says_the_tag_range_it_would_use():
    text = ledger.format_dry_run(4, 9, "spike", _readback_config())
    assert "spike-9-0000 .. spike-9-0003" in text
    assert "no network calls were made" in text


def test_the_dry_run_does_not_build_an_adapter():
    """A dry run must not construct one, because constructing an http adapter
    connects to something."""
    text = ledger.format_dry_run(2, 1, "hq", _readback_config())
    assert "http" in text


# ---------------------------------------------------------------------------
# The tags themselves
# ---------------------------------------------------------------------------


def test_a_tag_is_unique_within_a_run():
    tags = [tag for _e, tag, _r in build_tagged_corpus(SEED, 40)]
    assert len(set(tags)) == 40


def test_a_prefix_change_isolates_two_concurrent_runs():
    """The failure that makes a ledger untrustworthy rather than merely wrong:
    two runs at once reading each other's records."""
    first = {tag for _e, tag, _r in build_tagged_corpus(SEED, 10, "run-a")}
    second = {tag for _e, tag, _r in build_tagged_corpus(SEED, 10, "run-b")}
    assert not first & second


def test_the_same_seed_and_prefix_rebuild_the_same_tags():
    first = [tag for _e, tag, _r in build_tagged_corpus(SEED, 10)]
    second = [tag for _e, tag, _r in build_tagged_corpus(SEED, 10)]
    assert first == second
    assert first[0] == make_tag("hq", SEED, 0)


# ---------------------------------------------------------------------------
# The run: strays that cannot be searched are reported, not refused
# ---------------------------------------------------------------------------

TARGET = "local"
TARGET_URL = "http://localhost:9/intake"


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Intake:
    def __init__(self):
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return ClientResponse(status=200, body=b"ok")


class _TicketApi:
    """An HTTP ticket API that answers a tag lookup with that tag's ticket,
    a tagless request with every ticket, and carries no tag field at all."""

    def __init__(self, count, seed, prefix="hq"):
        self.tickets = {}
        for index, (email, tag, _rid) in enumerate(build_tagged_corpus(seed, count, prefix)):
            self.tickets[tag] = {
                "id": f"T{index}",
                "from": email.ground_truth.from_addr,
                "subject": email.ground_truth.subject,
                "body": email.text,
                "attachments": [a.filename for a in email.attachments],
            }
        self.requests = []

    def send(self, request):
        self.requests.append(request.url)
        query = parse_qs(urlsplit(request.url).query)
        if "tag" in query:
            found = self.tickets.get(query["tag"][0])
            items = [found] if found else []
        else:
            items = list(self.tickets.values())
        return ClientResponse(status=200, body=json.dumps({"items": items}).encode())


@pytest.fixture
def target_config(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n', encoding="utf-8")
    return str(path)


def _execute(target_config, tmp_path, readback, readback_client, **kwargs):
    clock = _Clock()
    lines = []
    intake = _Intake()
    code = ledger.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / "ledger.json"),
        readback=readback, client=intake, readback_client=readback_client,
        sleep=clock.sleep, clock=clock, printer=lines.append,
        quiet_window=0.0, poll_interval=0.1, **kwargs,
    )
    artifact = None
    if (tmp_path / "ledger.json").is_file():
        artifact = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    return code, "\n".join(lines), artifact, intake


def test_an_http_store_with_enumerate_false_reports_unverified_strays_and_exits_3(
    target_config, tmp_path
):
    """The run was sent and read back; refusing it now would throw that away."""
    readback = ReadbackConfig(
        kind="http", url="http://localhost:8000/tickets", enumerate_records=False
    )
    code, text, artifact, intake = _execute(
        target_config, tmp_path, readback, _TicketApi(COUNT, SEED)
    )
    assert code == EXIT_MISMATCH, text
    assert code != EXIT_REFUSED
    assert len(intake.sent) == COUNT
    summary = artifact["summary"]
    assert summary["verdict"] == "UNVERIFIED STRAYS"
    assert summary["strays_searched"] is False
    assert summary["extra"] is None
    assert "enumerate is false" in summary["strays_not_searched_reason"]
    assert "NOT SEARCHED" in text


def test_an_enumeration_that_fails_after_sending_is_reported_not_refused(
    target_config, tmp_path, monkeypatch
):
    from testinghq.pipeline.readback import FunctionAdapter

    api = _TicketApi(COUNT, SEED)

    class _ListFails(FunctionAdapter):
        def list_all(self):
            raise RuntimeError("list endpoint returned 500")

    def fetch(probe):
        found = api.tickets.get(probe.tag)
        return [Readback(
            exists=True, ticket_id=found["id"], from_addr=found["from"],
            subject=found["subject"], body=found["body"],
            attachment_names=tuple(found["attachments"]), tag=probe.tag,
            fields=("from_addr", "subject", "body", "attachment_names"),
        )] if found else []

    monkeypatch.setattr(ledger, "build_adapter", lambda config, **kw: _ListFails(fetch))
    code, text, artifact, _intake = _execute(
        target_config, tmp_path, ReadbackConfig(kind="mailbox", path="x.jsonl"), None
    )
    assert code == EXIT_MISMATCH, text
    assert artifact["summary"]["verdict"] == "UNVERIFIED STRAYS"
    assert "500" in artifact["summary"]["strays_not_searched_reason"]


def test_an_http_store_with_no_tag_field_balances(target_config, tmp_path):
    """The store returns its tickets with no tag field. The run's own tickets
    are recognised by id, so nothing is counted as extra."""
    readback = ReadbackConfig(kind="http", url="http://localhost:8000/tickets")
    code, text, artifact, _intake = _execute(
        target_config, tmp_path, readback, _TicketApi(COUNT, SEED)
    )
    assert code == EXIT_OK, text
    assert artifact["summary"]["verdict"] == "BALANCED"
    assert artifact["summary"]["extra"] == []


def test_expect_route_reaches_the_ledger_run(target_config, tmp_path):
    readback = ReadbackConfig(kind="http", url="http://localhost:8000/tickets")
    code, text, artifact, _intake = _execute(
        target_config, tmp_path, readback, _TicketApi(COUNT, SEED), route="queue:support"
    )
    assert artifact["config"]["expect_route"] == "queue:support"
    # The API exposes no route field, so routing is not visible, not failed.
    assert code == EXIT_OK, text


def test_the_ledger_artifact_leads_with_schema_version_and_tool(target_config, tmp_path):
    readback = ReadbackConfig(kind="http", url="http://localhost:8000/tickets")
    _code, _text, artifact, _intake = _execute(
        target_config, tmp_path, readback, _TicketApi(COUNT, SEED),
        subject_match="suffix", attachment_match="loose",
    )
    assert list(artifact)[:2] == ["schema_version", "tool"]
    assert artifact["schema_version"] == 1 and artifact["tool"] == "ledger"
    assert artifact["config"]["subject_match"] == "suffix"
    assert artifact["config"]["attachment_match"] == "loose"


def test_the_dry_run_says_strays_will_not_be_searched_when_enumerate_is_false():
    text = ledger.format_dry_run(
        3, 1, "hq",
        ReadbackConfig(kind="http", url="http://localhost:8000/t", enumerate_records=False),
    )
    assert "NOT SEARCHED" in text
    assert "UNVERIFIED STRAYS" in text


def test_the_dry_run_says_what_extra_will_mean():
    text = ledger.format_dry_run(3, 1, "spike", _readback_config())
    assert "spike-*" in text
    assert "untagged records are ignored" in text
    assert "routing skipped" in text


# ---------------------------------------------------------------------------
# The prefix family, with per-run prefixes
# ---------------------------------------------------------------------------


def _stray(tag, ticket):
    return Readback(exists=True, ticket_id=ticket, tag=tag)


def test_a_per_run_prefix_family_is_exact():
    """`hq-<yymmddHHMM>-<4 hex>` per run: the same prefix with an unsent index
    is extra, a different run's prefix is another run, never extra."""
    run = "hq-2610141200-ab12"
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    mine = make_tag(run, SEED, 77)
    earlier = make_tag("hq-2610141100-cd34", SEED, 0)
    sorted_strays = ledger.classify_strays(
        [_stray(mine, "MINE"), _stray(earlier, "EARLIER")],
        [m.tag for m in sent], [], run,
    )
    assert sorted_strays["extra"] == [{"tag": mine, "ticket": "MINE"}]
    assert sorted_strays["other_runs"] == [{"tag": earlier, "ticket": "EARLIER"}]


def test_a_fixed_prefix_does_not_claim_a_per_run_prefix_that_starts_with_it():
    """A run with `--tag-prefix hq` must not count another run's
    `hq-2610141200-ab12-...` tickets as its own strays."""
    other = make_tag("hq-2610141200-ab12", SEED, 1)
    assert not ledger.in_family(other, "hq")
    assert ledger.in_family(make_tag("hq", 9, 3), "hq")
    sorted_strays = ledger.classify_strays([_stray(other, "X")], [], [], "hq")
    assert sorted_strays["extra"] == []
    assert sorted_strays["other_runs"] == [{"tag": other, "ticket": "X"}]


def test_a_user_tag_prefix_with_dashes_is_its_own_family():
    assert ledger.minted_by("acme-ci-7-0002", ["acme-ci"]) == "acme-ci"
    assert ledger.minted_by("acme-ci-7-0002", ["acme"]) is None
    assert ledger.minted_by("acme-ci-dup-7-0002", ["acme-ci", "acme-ci-dup"]) == "acme-ci-dup"


# ---------------------------------------------------------------------------
# Enumeration through the adapter contract
# ---------------------------------------------------------------------------


def _fetch_from(api):
    def fetch(probe):
        found = api.tickets.get(probe.tag)
        return [Readback(
            exists=True, ticket_id=found["id"], from_addr=found["from"],
            subject=found["subject"], body=found["body"],
            attachment_names=tuple(found["attachments"]), tag=probe.tag,
            fields=("from_addr", "subject", "body", "attachment_names"),
        )] if found else []
    return fetch


def test_a_can_enumerate_method_returning_false_is_unverified_strays(
    target_config, tmp_path, monkeypatch
):
    """The http adapter keeps `list_all` and answers `can_enumerate()` False
    when `enumerate = false`. The ledger must ask, and never call a listing
    that would raise after the messages are sent."""
    api = _TicketApi(COUNT, SEED)
    calls = []

    class _NoEnumerate:
        def fetch(self, probe):
            return _fetch_from(api)(probe)

        def can_enumerate(self):
            return False

        def list_all(self):
            calls.append("list_all")
            raise AssertionError("list_all called although can_enumerate() is False")

    monkeypatch.setattr(ledger, "build_adapter", lambda config, **kw: _NoEnumerate())
    code, text, artifact, intake = _execute(
        target_config, tmp_path, ReadbackConfig(kind="mailbox", path="x.jsonl"), None
    )
    assert code == EXIT_MISMATCH, text
    assert len(intake.sent) == COUNT
    assert calls == []
    assert artifact["summary"]["verdict"] == "UNVERIFIED STRAYS"
    assert artifact["summary"]["extra"] is None


def test_a_truncated_enumeration_is_not_a_searched_store(
    target_config, tmp_path, monkeypatch
):
    """A listing that says pagination stopped short still reports the strays
    it saw, but cannot balance the ledger."""
    api = _TicketApi(COUNT, SEED)

    class _Listing(list):
        partial = True

    class _Truncated:
        def fetch(self, probe):
            return _fetch_from(api)(probe)

        def list_all(self):
            return _Listing([_stray(make_tag("hq", SEED, 50), "STRAY")])

    monkeypatch.setattr(ledger, "build_adapter", lambda config, **kw: _Truncated())
    code, text, artifact, _intake = _execute(
        target_config, tmp_path, ReadbackConfig(kind="mailbox", path="x.jsonl"), None
    )
    summary = artifact["summary"]
    assert code == EXIT_MISMATCH, text
    assert summary["strays_searched"] is False
    assert summary["strays_partial"] is True
    assert summary["extra_count"] == 1
    assert summary["verdict"] == "UNACCOUNTED"
    assert "PARTIAL" in text


def test_a_truncated_enumeration_with_nothing_extra_is_unverified(
    target_config, tmp_path, monkeypatch
):
    api = _TicketApi(COUNT, SEED)

    class _Listing(list):
        partial = True

    class _Truncated:
        def fetch(self, probe):
            return _fetch_from(api)(probe)

        def list_all(self):
            return _Listing()

    monkeypatch.setattr(ledger, "build_adapter", lambda config, **kw: _Truncated())
    code, _text, artifact, _intake = _execute(
        target_config, tmp_path, ReadbackConfig(kind="mailbox", path="x.jsonl"), None
    )
    assert code == EXIT_MISMATCH
    assert artifact["summary"]["verdict"] == "UNVERIFIED STRAYS"
    assert "partial" in artifact["summary"]["strays_not_searched_reason"]


def test_the_artifact_records_the_target_format_and_public_flag(target_config, tmp_path):
    readback = ReadbackConfig(kind="http", url="http://localhost:8000/tickets")
    _code, _text, artifact, _intake = _execute(
        target_config, tmp_path, readback, _TicketApi(COUNT, SEED)
    )
    assert artifact["config"]["format"] == "sendgrid"
    assert artifact["config"]["allow_public_target"] is False


# ---------------------------------------------------------------------------
# Ctrl-C
# ---------------------------------------------------------------------------


def test_ctrl_c_during_the_readback_writes_what_it_has_and_exits_3(
    target_config, tmp_path, monkeypatch
):
    class _Interrupted:
        def fetch(self, probe):
            raise KeyboardInterrupt

    monkeypatch.setattr(ledger, "build_adapter", lambda config, **kw: _Interrupted())
    code, text, artifact, intake = _execute(
        target_config, tmp_path, ReadbackConfig(kind="mailbox", path="x.jsonl"), None
    )
    assert code == EXIT_MISMATCH, text
    assert artifact["aborted"] is True
    assert artifact["tool"] == "ledger"
    assert artifact["summary"]["verdict"] == "ABORTED"
    assert artifact["summary"]["aborted_during"] == "readback"
    assert artifact["summary"]["sent"] == COUNT == len(intake.sent)
    assert [r["tag"] for r in artifact["records"]] == [
        tag for _e, tag, _r in build_tagged_corpus(SEED, COUNT)
    ]
    assert "ABORTED" in text
