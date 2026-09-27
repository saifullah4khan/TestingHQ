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

import re

from testinghq.core.transport import TransportResult
from testinghq.pipeline import ledger
from testinghq.pipeline.common import SentMessage
from testinghq.pipeline.messages import build_chain_message_id, make_tag
from testinghq.pipeline.readback import Probe, Readback
from testinghq.pipeline.verify import build_tagged_corpus

SEED = 4
COUNT = 3


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


def test_records_this_run_never_sent_are_extra():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    strays = readbacks[sent[0].tag] + [
        Readback(exists=True, ticket_id="STRAY", tag="someone-elses")
    ]
    accounting = ledger.account(sent, readbacks, strays=strays)
    assert accounting["extra"] == [{"tag": "someone-elses", "ticket": "STRAY"}]
    assert accounting["balanced"] is False
    assert ledger.verdict(accounting) == "UNACCOUNTED"


def test_an_untagged_record_counts_as_extra():
    """A ticket with no tag at all cannot be matched to anything this run
    sent, which is exactly what makes it worth looking at."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    strays = readbacks[sent[0].tag] + [Readback(exists=True, ticket_id="STRAY", tag=None)]
    accounting = ledger.account(sent, readbacks, strays=strays)
    assert accounting["extra"] == [{"tag": None, "ticket": "STRAY"}]


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
        ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
        sent,
        accounting,
        readbacks,
    )


def _readback_config():
    from testinghq.pipeline.adapters import ReadbackConfig

    return ReadbackConfig(kind="http", url="http://localhost:8000/tickets")


def test_the_report_prints_every_section_even_when_there_is_nothing_wrong():
    """A report that only lists problems cannot be distinguished from one that
    found none, and "confirm we lost nothing" is as often the question as
    "tell me what we lost"."""
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
        sent, ledger.account(sent, readbacks, strays=[]), readbacks,
    ))

    assert text.splitlines()[0].startswith("ledger: BALANCED")
    for section in ("missing: none", "duplicated: none", "misparsed: none"):
        assert section in text


def test_the_report_says_when_strays_were_not_searched():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
        sent, ledger.account(sent, readbacks, strays=None), readbacks,
    ))
    assert "NOT SEARCHED" in text
    assert "this adapter cannot enumerate" in text


def test_a_balanced_run_with_strays_searched_says_what_it_knows():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
        sent, ledger.account(sent, readbacks, strays=[]), readbacks,
    ))
    assert "nothing else was found" in text


def test_a_run_with_unsearched_strays_says_it_does_not_count_as_balanced():
    sent = _sent()
    readbacks = {m.tag: [_good(m)] for m in sent}
    text = ledger.format_ledger(ledger.build_artifact(
        SEED, ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
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
        SEED, ledger.run_config(SEED, total, "hq", "local", _readback_config(), None, 0.0),
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
        ledger.run_config(SEED, COUNT, "hq", "local", _readback_config(), None, 0.0),
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
        SEED, COUNT, "spike", "local", _readback_config(), "queue:support", 2.5
    )
    assert config["tool"] == "ledger"
    assert config["seed"] == SEED
    assert config["tag_prefix"] == "spike"
    assert config["expect_route"] == "queue:support"
    assert config["settle"] == 2.5
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
