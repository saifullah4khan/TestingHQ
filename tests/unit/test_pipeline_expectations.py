"""The check layer: what a verification verdict is allowed to claim.

Two properties are under test here, and they pull against each other, which
is the point.

The first is that a real parse error is caught. Every check has a test where
the system is genuinely wrong, and the check has to say so.

The second is that a check never claims more than it looked at. A field the
adapter could not see is SKIPPED, never PASSED. The tests for that are the
reason this suite is worth more than a green tick: a check that cannot report
"unknown" is a check that will eventually report "fine" about something it
never read, and that failure is invisible in every other kind of test.

The third group is the deliberate untidiness: shapes a real pipeline produces
that are NOT parse bugs, and which the checks must not fail. Without those, the
normalisation in `readback.py` would be untested leniency rather than tested
judgement.
"""
from __future__ import annotations

import pytest

from testinghq.blast.generate import generate_corpus
from testinghq.blast.payload import Attachment
from testinghq.core import report
from testinghq.pipeline import expectations as exp
from testinghq.pipeline.readback import Readback, ReadbackError

ALL_FIELDS = (
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "message_id",
    "in_reply_to",
    "references",
)


def _email(seed: int = 1, index: int = 0, attachments=()):
    from dataclasses import replace

    return replace(generate_corpus(seed, 1)[index], attachments=tuple(attachments))


def _good(email, route=None) -> Readback:
    """A readback describing a pipeline that got everything right."""
    return Readback(
        exists=True,
        ticket_id="T1",
        from_addr=email.ground_truth.from_addr,
        subject=email.ground_truth.subject,
        body=email.text,
        attachment_names=tuple(a.filename for a in email.attachments),
        route=route if route is not None else (email.envelope.to[0] if email.envelope.to else None),
        message_id="<m@widgets.example>",
        fields=ALL_FIELDS,
    )


def _status(verification, name):
    for check in verification.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in verification.checks]}")


# ---------------------------------------------------------------------------
# The verdict vocabulary
# ---------------------------------------------------------------------------


def test_a_check_result_has_exactly_three_states():
    assert exp.CheckResult("x", True, "ok").status == exp.PASSED
    assert exp.CheckResult("x", False, "no").status == exp.FAILED
    assert exp.CheckResult("x", None, "cannot tell").status == exp.SKIPPED


def test_the_six_named_checks_are_the_documented_six():
    assert exp.CHECKS == (
        "ticket_created",
        "sender",
        "subject",
        "body",
        "attachments",
        "routing",
    )


# ---------------------------------------------------------------------------
# ticket_created: the one check that never skips
# ---------------------------------------------------------------------------


def test_no_record_at_all_fails_ticket_created_and_skips_the_rest():
    email = _email()
    verification = exp.evaluate(email, None, record_id="r", tag="t")
    assert _status(verification, "ticket_created").status == exp.FAILED
    for name in ("sender", "subject", "body", "attachments", "routing"):
        assert _status(verification, name).status == exp.SKIPPED
    assert verification.passed is False
    assert verification.found is False


def test_a_skipped_check_is_never_counted_as_a_pass():
    """The property the whole module is built around. A payload that produced
    no record must not be able to reach a passing verdict through the five
    checks that had nothing to compare."""
    email = _email()
    verification = exp.evaluate(email, None)
    passing = [c for c in verification.checks if c.passed is True]
    assert passing == []


def test_a_known_but_dead_record_fails_ticket_created():
    email = _email()
    verification = exp.evaluate(email, Readback(exists=False, ticket_id="T1"))
    assert _status(verification, "ticket_created").status == exp.FAILED


def test_a_live_record_passes_ticket_created_and_names_it():
    email = _email()
    verification = exp.evaluate(email, _good(email))
    check = _status(verification, "ticket_created")
    assert check.status == exp.PASSED
    assert "T1" in check.detail


# ---------------------------------------------------------------------------
# sender
# ---------------------------------------------------------------------------


def test_a_correct_sender_passes():
    email = _email()
    assert _status(exp.evaluate(email, _good(email)), "sender").status == exp.PASSED


def test_a_sender_that_lost_its_address_fails():
    email = _email()
    readback = Readback(
        exists=True,
        ticket_id="T1",
        from_addr="Alice Smith",
        fields=("from_addr",),
    )
    check = _status(exp.evaluate(email, readback), "sender")
    assert check.status == exp.FAILED
    assert email.ground_truth.from_addr in check.detail


def test_a_display_name_around_the_address_is_not_a_mismatch():
    """A ticket system that stores `Alice <a@example.com>` parsed correctly."""
    email = _email()
    readback = Readback(
        exists=True,
        ticket_id="T1",
        from_addr=f"Alice <{email.ground_truth.from_addr}>",
        fields=("from_addr",),
    )
    assert _status(exp.evaluate(email, readback), "sender").status == exp.PASSED


def test_a_sender_the_adapter_cannot_see_is_skipped_not_passed():
    email = _email()
    readback = Readback(exists=True, ticket_id="T1", fields=("subject",))
    check = _status(exp.evaluate(email, readback), "sender")
    assert check.status == exp.SKIPPED
    assert "could not see" in check.detail


# ---------------------------------------------------------------------------
# subject
# ---------------------------------------------------------------------------


def test_a_correct_subject_passes():
    email = _email()
    assert _status(exp.evaluate(email, _good(email)), "subject").status == exp.PASSED


def test_a_truncated_subject_fails():
    email = _email()
    readback = Readback(
        exists=True, ticket_id="T1", subject=email.ground_truth.subject[:5], fields=("subject",)
    )
    check = _status(exp.evaluate(email, readback), "subject")
    assert check.status == exp.FAILED
    assert "expected" in check.detail


def test_a_subject_differing_only_in_case_and_spacing_is_not_a_mismatch():
    """Header folding and case are not what anyone is debugging, and a mismatch
    here would be a false alarm in a report that has to be believed."""
    email = _email()
    untidy = f"  {email.ground_truth.subject.upper()}  "
    readback = Readback(
        exists=True, ticket_id="T1", subject=untidy, fields=("subject",)
    )
    assert _status(exp.evaluate(email, readback), "subject").status == exp.PASSED


def test_a_subject_that_lost_its_reply_prefix_is_a_mismatch():
    """The other side of the same coin. The prefix really was dropped, which is
    a real difference in what the ticket says, and the check should say so
    rather than folding it away."""
    email = _email()
    for prefix in ("Re: ", "FWD: "):
        if email.ground_truth.subject.lower().startswith(prefix.lower()):
            readback = Readback(
                exists=True,
                ticket_id="T1",
                subject=email.ground_truth.subject[len(prefix) :],
                fields=("subject",),
            )
            assert _status(exp.evaluate(email, readback), "subject").status == exp.FAILED
            return
    readback = Readback(exists=True, ticket_id="T1", subject="Re: unrelated", fields=("subject",))
    assert _status(exp.evaluate(email, readback), "subject").status == exp.FAILED


def test_a_subject_with_mojibake_fails():
    email = _email()
    readback = Readback(
        exists=True, ticket_id="T1", subject="âœ” " + email.ground_truth.subject, fields=("subject",)
    )
    assert _status(exp.evaluate(email, readback), "subject").status == exp.FAILED


# ---------------------------------------------------------------------------
# body
# ---------------------------------------------------------------------------


def test_a_correct_body_passes():
    email = _email()
    assert _status(exp.evaluate(email, _good(email)), "body").status == exp.PASSED


def test_a_truncated_body_fails_and_says_the_text_is_missing():
    email = _email()
    readback = Readback(
        exists=True, ticket_id="T1", body=email.ground_truth.body_core[:20], fields=("body",)
    )
    check = _status(exp.evaluate(email, readback), "body")
    assert check.status == exp.FAILED
    assert "not present" in check.detail


def test_an_empty_body_fails_when_there_was_a_body():
    email = _email()
    readback = Readback(exists=True, ticket_id="T1", body="", fields=("body",))
    assert _status(exp.evaluate(email, readback), "body").status == exp.FAILED


def test_a_body_extracted_from_the_html_part_still_passes():
    """The reason the body check is a substring check. A pipeline that reads
    the HTML part returns an equivalent text, not an identical one, and failing
    it would train the reader to ignore body failures."""
    email = _email()
    readback = Readback(
        exists=True,
        ticket_id="T1",
        body=f"{email.ground_truth.body_core}\n\n[testinghq:hq-1-0000]",
        fields=("body",),
    )
    assert _status(exp.evaluate(email, readback), "body").status == exp.PASSED


def test_body_exact_mode_demands_the_whole_body():
    """`body_exact` compares the whole text body, not the substantive sentence,
    because a system that stores one field verbatim should be held to it."""
    email = _email()
    expectations = exp.Expectations.from_email(email, body_exact=True)
    readback = Readback(
        exists=True, ticket_id="T1", body=email.ground_truth.body_core, fields=("body",)
    )
    verification = exp.evaluate(email, readback, expectations)
    assert _status(verification, "body").status == exp.FAILED

    exact = Readback(exists=True, ticket_id="T1", body=email.text, fields=("body",))
    assert _status(exp.evaluate(email, exact, expectations), "body").status == exp.PASSED


def test_body_exact_falls_back_to_the_core_when_no_full_body_was_supplied():
    """A record-only caller has no payload and so no full text. Comparing
    against an empty string would report every such body as wrong."""
    email = _email()
    from_record = exp.Expectations(
        sender=email.ground_truth.from_addr,
        subject=email.ground_truth.subject,
        body_core=email.ground_truth.body_core,
        body_exact=True,
    )
    readback = Readback(
        exists=True, ticket_id="T1", body=email.ground_truth.body_core, fields=("body",)
    )
    assert _status(exp.evaluate(email, readback, from_record), "body").status == exp.PASSED


def test_a_body_the_adapter_cannot_see_is_skipped():
    email = _email()
    readback = Readback(exists=True, ticket_id="T1", fields=("subject",))
    assert _status(exp.evaluate(email, readback), "body").status == exp.SKIPPED


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


def test_all_attachments_present_passes():
    email = _email(
        attachments=[
            Attachment(filename="invoice.pdf", content_type="application/pdf", content=b"%PDF-1.4"),
            Attachment(filename="notes.txt", content_type="text/plain", content=b"hi"),
        ]
    )
    assert _status(exp.evaluate(email, _good(email)), "attachments").status == exp.PASSED


def test_a_dropped_attachment_fails_and_names_it():
    """The single most common real intake bug, and the one a 200 hides
    completely. This is the case `verify` exists for."""
    email = _email(
        attachments=[
            Attachment(filename="invoice.pdf", content_type="application/pdf", content=b"%PDF-1.4"),
            Attachment(filename="notes.txt", content_type="text/plain", content=b"hi"),
        ]
    )
    readback = Readback(
        exists=True,
        ticket_id="T1",
        attachment_names=("invoice.pdf",),
        fields=("attachment_names",),
    )
    check = _status(exp.evaluate(email, readback), "attachments")
    assert check.status == exp.FAILED
    assert "notes.txt" in check.detail


def test_an_invented_attachment_fails():
    email = _email(
        attachments=[
            Attachment(filename="invoice.pdf", content_type="application/pdf", content=b"%PDF-1.4")
        ]
    )
    readback = Readback(
        exists=True,
        ticket_id="T1",
        attachment_names=("invoice.pdf", "extra.bin"),
        fields=("attachment_names",),
    )
    check = _status(exp.evaluate(email, readback), "attachments")
    assert check.status == exp.FAILED
    assert "extra.bin" in check.detail


def test_attachment_order_is_not_a_property_any_system_preserves():
    email = _email(
        attachments=[
            Attachment(filename="a.txt", content_type="text/plain", content=b"a"),
            Attachment(filename="b.txt", content_type="text/plain", content=b"b"),
        ]
    )
    readback = Readback(
        exists=True, ticket_id="T1", attachment_names=("b.txt", "a.txt"), fields=("attachment_names",)
    )
    assert _status(exp.evaluate(email, readback), "attachments").status == exp.PASSED


def test_a_payload_with_no_attachments_reports_not_checked_not_passed():
    """A check that never ran must not wear the costume of one that did. There
    is no attachment handling to have got right, and saying "attachments: ok"
    would overstate what the run established."""
    email = _email()
    check = _status(exp.evaluate(email, _good(email)), "attachments")
    assert check.status == exp.SKIPPED
    assert "no attachments" in check.detail


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_routing_defaults_to_the_payload_own_recipient():
    """The default is ground truth the generator already produced, not a guess
    about the operator's business."""
    email = _email()
    expectations = exp.Expectations.from_email(email)
    assert expectations.route_for(email) == email.envelope.to[0]


def test_a_declared_route_overrides_the_recipient_default():
    email = _email()
    expectations = exp.Expectations.from_email(email, route="queue:support")
    assert expectations.route_for(email) == "queue:support"


def test_correct_routing_passes():
    email = _email()
    assert _status(exp.evaluate(email, _good(email)), "routing").status == exp.PASSED


def test_misrouted_mail_fails():
    email = _email()
    readback = Readback(
        exists=True,
        ticket_id="T1",
        from_addr=email.ground_truth.from_addr,
        subject=email.ground_truth.subject,
        body=email.text,
        route="queue:unrouted",
        fields=("from_addr", "subject", "body", "route"),
    )
    check = _status(exp.evaluate(email, readback), "routing")
    assert check.status == exp.FAILED
    assert "queue:unrouted" in check.detail


def test_routing_is_skipped_when_nothing_is_expected_and_nothing_seen():
    """A pipeline with no notion of routing declares nothing and the adapter
    sees no route. Reporting a failure here would be inventing a requirement."""
    email = _email()
    readback = Readback(
        exists=True, ticket_id="T1", from_addr=email.ground_truth.from_addr, fields=("from_addr",)
    )
    check = _status(exp.evaluate(email, readback), "routing")
    assert check.status == exp.SKIPPED


def test_an_unseen_route_against_a_declared_expectation_is_skipped_not_passed():
    email = _email()
    expectations = exp.Expectations.from_email(email, route="queue:support")
    readback = Readback(exists=True, ticket_id="T1", fields=("subject",))
    assert _status(exp.evaluate(email, readback, expectations), "routing").status == exp.SKIPPED


# ---------------------------------------------------------------------------
# Sequences: duplicates
# ---------------------------------------------------------------------------


def test_two_records_for_one_message_fail_and_name_both_tickets():
    email = _email()
    readbacks = [
        Readback(exists=True, ticket_id="T1", from_addr=email.ground_truth.from_addr,
                 subject=email.ground_truth.subject, body=email.text,
                 attachment_names=(), route=email.envelope.to[0], fields=ALL_FIELDS),
        Readback(exists=True, ticket_id="T2", from_addr=email.ground_truth.from_addr,
                 subject=email.ground_truth.subject, body=email.text,
                 attachment_names=(), route=email.envelope.to[0], fields=ALL_FIELDS),
    ]
    verification = exp.evaluate_sequence(email, readbacks, record_id="r", tag="t")
    check = _status(verification, "ticket_created")
    assert check.status == exp.FAILED
    assert "T1" in check.detail and "T2" in check.detail
    assert verification.passed is False


def test_a_duplicate_where_the_first_copy_is_perfect_still_fails():
    """The reason a sequence is not collapsed to its first element. Choosing
    the best-looking copy would make a pipeline that files everything twice
    indistinguishable from a correct one, which is most of the time."""
    email = _email()
    perfect = _good(email)
    verification = exp.evaluate_sequence(email, [perfect, perfect])
    assert verification.passed is False


def test_an_empty_sequence_behaves_exactly_like_no_record():
    email = _email()
    nothing = exp.evaluate_sequence(email, [])
    absent = exp.evaluate(email, None)
    assert nothing.passed == absent.passed is False
    assert _status(nothing, "ticket_created").status == exp.FAILED


def test_a_single_record_sequence_is_the_same_as_a_lone_readback():
    email = _email()
    one = exp.evaluate_sequence(email, [_good(email)])
    assert one.passed is True


# ---------------------------------------------------------------------------
# Threading checks
# ---------------------------------------------------------------------------


def _threaded_readback(ticket, in_reply_to="<root@w.example>", references=("<root@w.example>",)):
    return Readback(
        exists=True,
        ticket_id=ticket,
        in_reply_to=in_reply_to,
        references=references,
        fields=("in_reply_to", "references", "ticket_id"),
    )


def test_correct_threading_headers_pass_the_link_check():
    check = exp.check_thread_link(_threaded_readback("T2"), "root@w.example")
    assert check.status == exp.PASSED


def test_a_wrong_in_reply_to_fails_the_link_check():
    check = exp.check_thread_link(
        _threaded_readback("T2", in_reply_to="<other@w.example>"), "root@w.example"
    )
    assert check.status == exp.FAILED
    assert "In-Reply-To" in check.detail


def test_a_truncated_references_chain_fails_the_link_check():
    check = exp.check_thread_link(
        _threaded_readback("T2", references=()), "root@w.example"
    )
    assert check.status == exp.FAILED
    assert "References" in check.detail


def test_a_reply_on_the_same_ticket_passes_the_together_check():
    check = exp.check_thread_together(_threaded_readback("T1"), _threaded_readback("T1"))
    assert check.status == exp.PASSED


def test_a_reply_on_a_different_ticket_fails_the_together_check():
    """The outcome that makes an agent open a duplicate ticket a fortnight
    later, with perfectly correct headers."""
    check = exp.check_thread_together(_threaded_readback("T2"), _threaded_readback("T1"))
    assert check.status == exp.FAILED
    assert "T2" in check.detail and "T1" in check.detail


def test_the_two_threading_checks_are_independent():
    """A pipeline that threads the headers correctly but files the reply
    separately must fail exactly one of them, so the report says which half is
    broken."""
    readback = _threaded_readback("T2")
    root = _threaded_readback("T1")
    assert exp.check_thread_link(readback, "root@w.example").status == exp.PASSED
    assert exp.check_thread_together(readback, root).status == exp.FAILED


def test_threading_checks_skip_rather_than_invent_an_answer():
    assert exp.check_thread_link(None, "root@w.example").status == exp.SKIPPED
    assert exp.check_thread_link(_threaded_readback("T1"), "").status == exp.SKIPPED
    assert exp.check_thread_together(None, _threaded_readback("T1")).status == exp.SKIPPED


# ---------------------------------------------------------------------------
# GroundTruthMatcher: the join with the existing engine
# ---------------------------------------------------------------------------


def _record(email, seed=1):
    return report.build_record(
        email, report.CLEAN, seed=seed, index=0,
        response={"status": 200, "latency_ms": 1.0, "body_snippet": "ok"},
    )


def test_the_ground_truth_matcher_is_a_report_matcher():
    """If it is not, it cannot drop into `build_record`, and the join with the
    existing engine is a claim rather than a fact."""
    assert isinstance(exp.GroundTruthMatcher(), report.Matcher)


def test_the_ground_truth_matcher_passes_a_correct_readback():
    email = _email()
    result = exp.GroundTruthMatcher().match(_record(email), {"status": 200}, _good(email))
    assert result.passed is True
    assert result.mismatches == []


def test_the_ground_truth_matcher_fails_a_misparsed_readback():
    email = _email()
    readback = Readback(
        exists=True, ticket_id="T1", subject="wrong", fields=("subject", "from_addr", "body")
    )
    result = exp.GroundTruthMatcher().match(_record(email), {"status": 200}, readback)
    assert result.passed is False
    assert any("subject" in m for m in result.mismatches)


def test_the_ground_truth_matcher_fails_when_nothing_was_found():
    email = _email()
    result = exp.GroundTruthMatcher().match(_record(email), {"status": 200}, None)
    assert result.passed is False
    assert result.mismatches


def test_the_ground_truth_matcher_fails_on_two_records():
    email = _email()
    readbacks = [_good(email), _good(email)]
    result = exp.GroundTruthMatcher().match(_record(email), {"status": 200}, readbacks)
    assert result.passed is False
    assert "2 records" in result.mismatches[0]


def test_the_ground_truth_matcher_refuses_a_readback_of_the_wrong_shape():
    email = _email()
    with pytest.raises(ReadbackError):
        exp.GroundTruthMatcher().match(_record(email), {"status": 200}, {"id": "T1"})


def test_the_ground_truth_matcher_does_not_second_guess_the_transport():
    """Status grading is `report`'s job. Duplicating it here would be the
    third copy of the expectation rules that tests/test_lane_hygiene.py exists
    to prevent, and this matcher is the most likely place for one to appear."""
    source = exp.GroundTruthMatcher.__doc__ or ""
    assert "StatusOnlyMatcher" in source
    result = exp.GroundTruthMatcher().match(_record(_email()), {"status": 500}, _good(_email()))
    assert result.passed is True, (
        "a 500 with a perfectly parsed readback is a transport failure, which "
        "core/report already grades. This matcher must only speak about content."
    )


def test_a_record_with_no_ground_truth_block_is_refused():
    with pytest.raises(exp.ExpectationError):
        exp.GroundTruthMatcher().match(
            {"id": "x", "intended": {}}, {"status": 200}, _good(_email())
        )


def test_a_record_only_verification_skips_the_attachment_check():
    """A saved record stores a count, not filenames, so this path says so
    rather than guessing names and comparing against its own invention."""
    verification = exp.evaluate_from_record(
        _record(_email()), _good(_email()), exp.Expectations.from_email(_email())
    )
    check = _status(verification, "attachments")
    assert check.status == exp.SKIPPED
    assert "count" in check.detail


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_a_verification_serializes_with_its_three_states_intact():
    email = _email()
    verification = exp.evaluate(
        email,
        Readback(exists=True, ticket_id="T1", subject=email.ground_truth.subject, fields=("subject",)),
        record_id="r",
        tag="t",
    )
    payload = verification.to_json()
    assert payload["record_id"] == "r"
    assert payload["tag"] == "t"
    statuses = {c["check"]: c["status"] for c in payload["checks"]}
    assert statuses["ticket_created"] == exp.PASSED
    assert statuses["sender"] == exp.SKIPPED
    assert statuses["subject"] == exp.PASSED
    # Passed overall, with three of the six checks never run. That is the
    # documented verdict: the operator chose which fields the adapter exposes,
    # and a run is not called a failure for the fields nobody could see. What
    # the tool owes is that the skips are impossible to miss, which is what
    # the summary's checks_skipped_by_name and the printed report are for.
    assert payload["passed"] is True


def test_expectations_serialize_with_the_route_they_were_built_with():
    email = _email()
    assert exp.Expectations.from_email(email, route="q").to_json()["route"] == "q"
    assert exp.Expectations.from_email(email).to_json()["attachments"] == []
