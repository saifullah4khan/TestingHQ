"""loop: the machine-mail corpus and the checks, as pure logic.

No socket and no pipeline here. The corpus is a set of header shapes and the
judging is a function of what the pipeline is said to have done, so both are
tested directly. The integration file drives them through a real pipeline.

The property the tests are built around is the one the spec cares about most: a
tool with no way to see whether an auto-reply was sent must report that check as
SKIPPED, never as passed. Every "no sink" case below asserts `replied is None`,
because `False` would be a claim the tool has no evidence for.
"""
from __future__ import annotations

import pytest

from testinghq.core import guardrails
from testinghq.pipeline import loop as tool
from testinghq.pipeline.readback import Readback


# ---------------------------------------------------------------------------
# What counts as machine mail
# ---------------------------------------------------------------------------


def test_an_ordinary_message_is_not_machine_mail():
    assert (
        tool.machine_mail_marker(
            {"Message-ID": "x@example.com"}, "Your order shipped", "alice@example.com"
        )
        is None
    )


@pytest.mark.parametrize(
    "value", ["auto-replied", "auto-generated", "AUTO-REPLIED", " auto-replied "]
)
def test_auto_submitted_is_the_first_signal(value):
    marker = tool.machine_mail_marker(
        {"Auto-Submitted": value}, "Re: hello", "robot@example.com"
    )
    assert marker == f"Auto-Submitted: {value.strip().lower()}"


def test_an_unknown_auto_submitted_value_is_not_machine_mail():
    """RFC 3834 defines two values. Keying on the header's presence rather than
    its value would flag a pipeline that sets it to something else, so the
    value has to be one of the two."""
    assert (
        tool.machine_mail_marker(
            {"Auto-Submitted": "no"}, "Re: hello", "alice@example.com"
        )
        is None
    )


@pytest.mark.parametrize("value", ["bulk", "list", "junk", "BULK"])
def test_precedence_values_are_machine_mail(value):
    marker = tool.machine_mail_marker(
        {"Precedence": value}, "Bulletin", "newsletter@example.com"
    )
    assert marker == f"Precedence: {value.lower()}"


def test_precedence_first_person_is_not_machine_mail():
    assert (
        tool.machine_mail_marker(
            {"Precedence": "first-class"}, "Hello", "alice@example.com"
        )
        is None
    )


@pytest.mark.parametrize("local", list(tool.NO_REPLY_LOCALS))
def test_a_no_reply_sender_is_machine_mail(local):
    marker = tool.machine_mail_marker(
        {}, "Your receipt", f"{local}@billing.example.com"
    )
    assert marker == f"From: {local}@"


def test_a_no_reply_sender_is_found_through_a_display_name():
    assert (
        tool.machine_mail_marker({}, "Receipt", "No Reply <noreply@billing.example.com>")
        == "From: noreply@"
    )


def test_an_empty_return_path_is_a_bounce():
    marker = tool.machine_mail_marker(
        {"Return-Path": "", "Content-Type": "multipart/report"},
        "Undelivered Mail",
        "someone@example.com",
    )
    assert marker == "Return-Path: <>"


def test_a_populated_return_path_is_not_a_bounce():
    assert (
        tool.machine_mail_marker(
            {"Return-Path": "alice@example.com"}, "Hello", "alice@example.com"
        )
        is None
    )


def test_list_headers_are_machine_mail():
    assert (
        tool.machine_mail_marker(
            {"List-Unsubscribe": "<mailto:u@example.com>"}, "[users] hi", "u@example.com"
        )
        == "List-Unsubscribe present"
    )


@pytest.mark.parametrize(
    "subject",
    ["Automatic reply: your message", "out of office", "Auto-reply: away"],
)
def test_out_of_office_subjects_are_machine_mail(subject):
    marker = tool.machine_mail_marker({}, subject, "colleague@example.com")
    assert marker is not None and marker.startswith("Subject starts")


def test_a_subject_that_merely_mentions_a_reply_is_not_machine_mail():
    """The subject phrase is a signal, and a signal has to be anchored. A
    pipeline that keyed on a substring would treat half its human mail as
    machine-generated."""
    assert (
        tool.machine_mail_marker({}, "Re: automatic reply is broken", "alice@example.com")
        is None
    )


def test_a_mailer_daemon_sender_is_machine_mail():
    assert (
        tool.machine_mail_marker(
            {}, "Undelivered Mail", "MAILER-DAEMON@example.com"
        )
        == "From: MAILER-DAEMON@"
    )


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def test_the_default_count_sends_every_shape():
    """The index arithmetic once skipped two shapes at the default count."""
    corpus = tool.build_machine_corpus(3, tool.DEFAULT_COUNT, "hq")
    kinds = {m.kind for m in corpus}
    assert kinds == {shape[0] for shape in tool.MACHINE_MAIL_SHAPES}
    assert not any(m.is_loop_bait for m in corpus)


@pytest.mark.parametrize("extra", [0, 1, 5])
def test_any_count_at_least_the_shape_count_covers_every_shape(extra):
    count = len(tool.MACHINE_MAIL_SHAPES) + extra
    corpus = tool.build_machine_corpus(3, count, "hq")
    assert len(corpus) == count
    assert {m.kind for m in corpus} == {shape[0] for shape in tool.MACHINE_MAIL_SHAPES}


def test_every_message_is_recognised_as_machine_mail():
    for message in tool.build_machine_corpus(3, 12, "hq"):
        assert tool.machine_mail_marker(
            message.email.headers, message.email.subject, message.email.from_addr
        ), message.kind


def test_the_marker_in_the_report_is_the_one_the_detector_finds():
    """Two definitions of the same predicate would drift, and the drift would
    look like a pipeline disagreement."""
    for message in tool.build_machine_corpus(3, 12, "hq"):
        found = tool.machine_mail_marker(
            message.email.headers, message.email.subject, message.email.from_addr
        )
        if not message.is_loop_bait:
            assert message.marker == found, message.kind


def test_a_small_count_still_covers_distinct_signals():
    """A run of four must not be four copies of the same shape, or the tool
    would pass against a pipeline that keys on one header."""
    markers = {
        m.marker for m in tool.build_machine_corpus(3, 5, "hq")
    }
    assert len(markers) >= 4, markers


def test_every_message_is_stamped_and_tagged():
    corpus = tool.build_machine_corpus(9, 6, "spike")
    for index, message in enumerate(corpus):
        assert message.tag == f"spike-9-{index:04d}"
        assert message.record_id == f"loop-9-{index:04d}"
        assert message.email.headers["X-TestingHQ-Tag"] == message.tag


def test_the_tag_never_reaches_the_subject():
    """The reason tagging lives in a header and a body marker: a subject
    carrying a synthetic suffix is not the subject that was sent."""
    for message in tool.build_machine_corpus(9, 6, "spike"):
        assert message.tag not in message.email.subject
        assert message.tag not in message.email.ground_truth.subject


def test_the_corpus_is_deterministic():
    first = tool.build_machine_corpus(4, 8, "hq")
    second = tool.build_machine_corpus(4, 8, "hq")
    assert [m.email.text for m in first] == [m.email.text for m in second]
    assert [m.marker for m in first] == [m.marker for m in second]


def test_a_different_seed_gives_different_bodies():
    first = tool.build_machine_corpus(4, 8, "hq")
    second = tool.build_machine_corpus(5, 8, "hq")
    assert [m.email.text for m in first] != [m.email.text for m in second]


def test_every_address_is_synthetic():
    """The guardrail, run over the corpus the tool would actually send."""
    fields = []
    for message in tool.build_machine_corpus(4, 12, "hq"):
        email = message.email
        fields.extend(
            [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]
        )
    guardrails.require_synthetic_content(fields)


def test_a_negative_count_is_refused():
    with pytest.raises(ValueError):
        tool.build_machine_corpus(4, -1, "hq")


def test_the_loop_bait_is_sent_to_the_configured_reply_address():
    """The pipeline's reply came from its reply address, so an autoresponder
    answers to it."""
    bait = tool.build_loop_bait(4, 3, "hq", "bot@intake.example.com", "ack-1@example.com")
    assert bait.is_loop_bait
    assert bait.email.to == "bot@intake.example.com"


def test_the_loop_bait_comes_from_a_third_party_autoresponder():
    """Not from the pipeline's own address: a pipeline answering itself is
    not the loop that happens in production."""
    bait = tool.build_loop_bait(4, 3, "hq", "bot@intake.example.com", "ack-1@example.com")
    assert "bot@intake.example.com" not in bait.email.from_addr
    assert bait.email.headers["Auto-Submitted"] == "auto-replied"
    assert tool.machine_mail_marker(
        bait.email.headers, bait.email.subject, bait.email.from_addr
    )


def test_the_loop_bait_replies_to_the_pipelines_real_outbound_message():
    bait = tool.build_loop_bait(4, 3, "hq", "bot@intake.example.com", "ack-1@example.com")
    assert bait.email.headers["In-Reply-To"] == "<ack-1@example.com>"
    assert bait.email.headers["References"] == "<ack-1@example.com>"


def test_without_a_real_parent_the_bait_falls_back_to_a_reserved_domain():
    bait = tool.build_loop_bait(4, 3, "hq", "bot@intake.example.com", None)
    parent = bait.email.headers["In-Reply-To"].strip("<>")
    assert "unseen-parent" in parent
    guardrails.require_synthetic_content([parent])


def test_the_precursor_is_ordinary_mail_from_the_bait_sender():
    precursor = tool.build_precursor(4, 3, "hq")
    email = precursor.email
    assert tool.machine_mail_marker(email.headers, email.subject, email.from_addr) is None
    bait = tool.build_loop_bait(4, 3, "hq", "bot@intake.example.com", None)
    assert bait.email.from_addr == email.from_addr
    assert len({precursor.tag, bait.tag}) == 2


def test_the_dsn_shape_carries_a_delivery_status_part():
    dsn = next(m for m in tool.build_machine_corpus(1, tool.DEFAULT_COUNT, "hq")
               if m.kind == "bounce-dsn")
    assert dsn.email.headers["Content-Type"].startswith("multipart/report")
    assert "report-type=delivery-status" in dsn.email.headers["Content-Type"]
    types = [a.content_type for a in dsn.email.attachments]
    assert "message/delivery-status" in types
    status = next(a for a in dsn.email.attachments
                  if a.content_type == "message/delivery-status")
    assert b"Action: failed" in status.content and b"Status: 5.1.1" in status.content


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _message(kind="precedence-bulk", bait=False, tag="t1"):
    email = tool.build_machine_corpus(1, 1, "hq")[0].email
    return tool.MachineMail(
        kind=kind, marker="Precedence: bulk", email=email, tag=tag,
        record_id="r", is_loop_bait=bait,
    )


def _ticketed(ticket_id="T1"):
    return [Readback(exists=True, ticket_id=ticket_id, fields=("ticket_id",))]


def test_a_correct_pipeline_raises_nothing():
    result = tool.judge_one(_message(), readbacks=[], outbound=[])
    assert result.passed
    assert result.findings == ()
    assert result.ticked is False
    assert result.replied is False


def test_a_ticket_for_machine_mail_is_a_finding_under_the_default_policy():
    result = tool.judge_one(_message(), readbacks=_ticketed(), outbound=[])
    assert not result.passed
    assert any("a ticket was opened" in f for f in result.findings)


def test_a_ticket_is_informational_under_the_allowed_policy():
    result = tool.judge_one(
        _message(), readbacks=_ticketed(), outbound=[], ticket_policy="allowed"
    )
    assert result.passed
    assert any("ticket-policy allowed" in n for n in result.notes)
    assert result.ticked is True, "the fact is still reported, only not as a finding"


def test_an_unknown_ticket_policy_is_refused():
    """A typo in a policy that silently became 'none' or 'allowed' would change
    what counts as a finding."""
    with pytest.raises(ValueError):
        tool.judge_one(_message(), readbacks=[], outbound=[], ticket_policy="allowed-ish")


def test_an_emitted_auto_reply_is_a_finding():
    result = tool.judge_one(_message(), readbacks=[], outbound=[_ticketed("M1")])
    assert not result.passed
    assert any("outbound message was emitted" in f for f in result.findings)


def test_with_no_outbound_sink_the_reply_check_is_skipped_not_passed():
    """The central property. `None` means the check could not run; `False`
    would claim no reply was sent, which this tool has no evidence for."""
    result = tool.judge_one(_message(), readbacks=[], outbound=None)
    assert result.replied is None
    assert result.passed, "a skipped check must not fail the message"
    assert any("[loop.outbound]" in n for n in result.notes)
    assert not any("outbound message" in f for f in result.findings)


def test_a_pipeline_that_auto_replies_is_caught_only_when_a_sink_is_configured():
    """The consequence worth stating plainly: the same defect is a finding with
    a sink and invisible without one. The report has to make that legible, and
    it does, through the note on every affected row."""
    without = tool.judge_one(_message(), readbacks=[], outbound=None)
    with_sink = tool.judge_one(_message(), readbacks=[], outbound=[_ticketed("M1")])
    assert without.passed is True
    assert with_sink.passed is False
    assert without.replied is None and with_sink.replied is True


def test_the_loop_bait_is_a_finding_even_when_tickets_are_allowed():
    """Answering a message addressed to your own reply address is the loop. No
    policy makes that acceptable, so the allowed policy does not cover it."""
    result = tool.judge_one(
        _message(bait=True),
        readbacks=_ticketed(),
        outbound=[_ticketed("M1")],
        ticket_policy="allowed",
    )
    assert not result.passed
    assert any("the loop bait was answered" in f for f in result.findings)
    assert result.is_loop_bait is True


def test_an_untouched_loop_bait_is_clean_under_every_policy():
    for policy in tool.TICKET_POLICIES:
        result = tool.judge_one(
            _message(bait=True), readbacks=[], outbound=[], ticket_policy=policy
        )
        assert result.passed, policy


def test_judge_covers_every_message_in_order():
    corpus = tool.build_machine_corpus(2, 5, "hq")
    readbacks = {m.tag: _ticketed() for m in corpus[:2]}
    results = tool.judge(corpus, readbacks, None)
    assert [r.tag for r in results] == [m.tag for m in corpus]
    assert sum(1 for r in results if r.ticked) == 2


def test_a_result_serializes_for_the_artifact():
    payload = tool.judge_one(_message(), readbacks=_ticketed(), outbound=[]).to_json()
    assert payload["tag"] == "t1"
    assert payload["ticked"] is True
    assert payload["replied"] is False
    assert payload["findings"]
    assert "is_loop_bait" in payload


def test_unattributed_outbound_never_reads_as_no_reply():
    """An auto-reply that quotes nothing cannot be matched to a message. With
    such mail in the sink, "reply: no" would be a claim with no evidence."""
    result = tool.judge_one(_message(), readbacks=[], outbound=[], unattributed_seen=True)
    assert result.reply_state == tool.REPLY_UNATTRIBUTED
    assert result.replied is None
    plain = tool.judge_one(_message(), readbacks=[], outbound=[])
    assert plain.reply_state == tool.REPLY_NO


def test_a_bait_filed_on_the_precursors_ticket_is_not_a_new_ticket():
    result = tool.judge_one(
        _message(bait=True), readbacks=_ticketed("T7"), outbound=[],
        threaded_ticket_ids=["T7"],
    )
    assert result.ticked is False and result.passed
    assert any("existing ticket" in n for n in result.notes)


def _out(tag=None, in_reply_to=None, references=(), ident="O1"):
    return Readback(exists=True, ticket_id=ident, tag=tag, in_reply_to=in_reply_to,
                    references=tuple(references),
                    fields=("ticket_id", "in_reply_to", "references"))


def test_outbound_is_attributed_by_tag_then_by_threading_headers():
    ids = {"m1@hq.example.test": "t1", "m2@hq.example.test": "t2"}
    assert tool.attribute(_out(tag="t1"), ["t1", "t2"], ids) == "t1"
    assert tool.attribute(_out(in_reply_to="<m2@hq.example.test>"), ["t1", "t2"], ids) == "t2"
    assert tool.attribute(_out(references=["<other@x.example>", "<m1@hq.example.test>"]),
                          ["t1", "t2"], ids) == "t1"
    assert tool.attribute(_out(tag="someone-else"), ["t1", "t2"], ids) is None
    assert tool.attribute(_out(), ["t1", "t2"], ids) is None


def test_merge_keeps_tag_lookups_and_returns_what_nothing_claims():
    tagged = _out(tag="t1", ident="O1")
    threaded = _out(in_reply_to="<m2@hq.example.test>", ident="O2")
    stranger = _out(ident="O3")
    merged, unattributed = tool.merge_outbound(
        {"t1": [tagged]}, [tagged, threaded, stranger], ["t1", "t2"],
        {"m2@hq.example.test": "t2"},
    )
    assert merged == {"t1": [tagged], "t2": [threaded]}
    assert unattributed == [stranger]


def test_new_records_is_a_multiset_difference():
    a, b = _out(ident="A"), _out(ident="B")
    assert tool.new_records([a, a, b], [a]) == [a, b]
    assert tool.new_records([a], [a]) == []


def test_unattributed_outbound_makes_the_run_a_finding():
    corpus = tool.build_machine_corpus(1, 3, "hq")
    results = tool.judge(corpus, {}, {m.tag: [] for m in corpus}, unattributed_seen=True)
    summary = tool.summarize(results, corpus, "none", unattributed=[_out()],
                             outbound_enumerated=True)
    assert summary["findings"] == 1
    assert summary["auto_reply_unattributed"] == 1
    assert summary["verdict"] == "LOOPS-DETECTED"
    assert any(f.startswith("outbound, unattributed") for f in summary["run_findings"])
    text = tool.format_results(results, summary)
    assert "outbound, unattributed" in text
    assert all(r.reply_state == tool.REPLY_UNATTRIBUTED for r in results)


def test_loop_bait_answered_counts_only_the_bait():
    corpus = tool.build_machine_corpus(1, 2, "hq")
    bait = tool.build_loop_bait(1, 2, "hq", tool.DEFAULT_REPLY_ADDRESS, None)
    messages = [*corpus, bait]
    replies = {corpus[0].tag: [_out(tag=corpus[0].tag)], corpus[1].tag: [], bait.tag: []}
    summary = tool.summarize(tool.judge(messages, {}, replies), messages, "none")
    assert summary["auto_replies_emitted"] == 1
    assert summary["loop_bait_answered"] == 0
    replies[bait.tag] = [_out(tag=bait.tag, ident="O9")]
    results = tool.judge(messages, {}, replies)
    summary = tool.summarize(results, messages, "none")
    assert summary["loop_bait_answered"] == 1
    bait_result = next(r for r in results if r.is_loop_bait)
    assert bait_result.findings == ("the loop bait was answered, which is a live mail loop",)


def test_the_flag_wins_over_the_config_table_for_both_settings():
    section = {"ticket_policy": "allowed", "reply_address": "cfg@example.com"}
    assert tool.effective_settings(section, flag_policy=None, flag_reply=None) == (
        "allowed", "cfg@example.com")
    assert tool.effective_settings(section, flag_policy="none", flag_reply="flag@example.com") == (
        "none", "flag@example.com")
    assert tool.effective_settings({}, flag_policy=None, flag_reply=None) == (
        tool.TICKET_POLICY_NONE, tool.DEFAULT_REPLY_ADDRESS)


def test_the_headline_says_what_was_not_checked():
    base = {"findings": 0}
    assert tool.headline(base) == "LOOP-SAFE"
    assert tool.headline({**base, "auto_reply_skipped": 3}) == "LOOP-SAFE (auto-reply NOT CHECKED)"
    assert tool.headline({**base, "outbound_enumerated": False}) == (
        "LOOP-SAFE (untagged auto-replies NOT CHECKED)")
    assert tool.headline({**base, "unsettled": True}) == "UNSETTLED"
    assert tool.headline({"aborted": True}) == "ABORTED"


def test_the_report_says_no_sink_configured_not_a_field_problem():
    corpus = tool.build_machine_corpus(1, 2, "hq")
    results = tool.judge(corpus, {}, None)
    text = tool.format_results(results, tool.summarize(results, corpus, "none"))
    assert "no sink configured" in text
    assert "adapter cannot see field" not in text
    assert "by why" not in text
