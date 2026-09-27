"""Tagging: the identity that makes a message findable after it is sent.

The load-bearing property here is that a tag is a pure function of (prefix,
seed, index) and reaches the payload in a place the pipeline will not have
destroyed. Both halves are tested, plus the reason the tag is deliberately not
in the subject, which is the one design decision that would silently destroy
the subject check if it went the other way.
"""
from __future__ import annotations

import pytest

from testinghq.blast.generate import generate_corpus
from testinghq.blast.serialize import to_multipart_parts
from testinghq.core import report
from testinghq.pipeline import messages


def _stamped(seed: int = 3, index: int = 0, prefix: str = "hq"):
    corpus = generate_corpus(seed, index + 1)
    tag = messages.make_tag(prefix, seed, index)
    return tag, messages.stamp(corpus[index], tag)


# ---------------------------------------------------------------------------
# Tag identity
# ---------------------------------------------------------------------------


def test_a_tag_is_a_pure_function_of_its_three_inputs():
    assert messages.make_tag("hq", 7, 3) == messages.make_tag("hq", 7, 3)
    assert messages.make_tag("hq", 7, 3) == "hq-7-0003"


def test_tags_differ_by_index():
    assert messages.make_tag("hq", 7, 3) != messages.make_tag("hq", 7, 4)


def test_tags_differ_by_seed():
    """Two runs against one system must not read each other's records, which
    starts with the tags being different."""
    assert messages.make_tag("hq", 7, 3) != messages.make_tag("hq", 8, 3)


def test_tags_differ_by_prefix():
    assert messages.make_tag("hq", 7, 3) != messages.make_tag("spike", 7, 3)


def test_an_empty_prefix_is_refused():
    """A prefix that makes every tag identical would silently turn a ledger
    into a count of one."""
    with pytest.raises(ValueError):
        messages.make_tag("", 7, 3)


def test_a_negative_index_is_refused():
    with pytest.raises(ValueError):
        messages.make_tag("hq", 7, -1)


# ---------------------------------------------------------------------------
# Where the tag lands
# ---------------------------------------------------------------------------


def test_the_tag_reaches_the_payload_as_a_header():
    tag, email = _stamped()
    assert email.headers[messages.TAG_HEADER] == tag


def test_the_tag_reaches_the_payload_as_a_body_marker():
    """The outbound mail sink case: a sink records the body, not the headers,
    so a tag that only travelled in a header would be unfindable there."""
    tag, email = _stamped()
    assert messages.tag_marker(tag) in email.text
    assert messages.tag_marker(tag) in email.html


def test_the_tag_is_recoverable_from_the_body():
    tag, email = _stamped()
    assert messages.find_tag(email.text) == tag
    assert messages.find_tag(email.html) == tag


def test_the_tag_is_recoverable_from_a_stored_header_blob():
    tag, email = _stamped()
    assert messages.find_tag(messages.thread_header_text(email)) == tag


def test_find_tag_returns_none_for_text_carrying_no_marker():
    assert messages.find_tag("Subject: hello") is None
    assert messages.find_tag("") is None
    assert messages.find_tag(None) is None


def test_find_tag_ignores_an_unterminated_marker():
    """A truncated body must not yield a half-token that matches nothing."""
    assert messages.find_tag("[testinghq:hq-7-000") is None


def test_harvest_tag_tries_every_candidate_in_the_documented_order():
    header_tag = messages.make_tag("hq", 1, 0)
    body_tag = messages.make_tag("hq", 1, 1)
    candidates = (None, "nothing", messages.thread_header_text(
        _stamped(1, 0)[1]
    ), _stamped(1, 1)[1].text)
    assert messages.harvest_tag(*candidates) == header_tag
    assert messages.harvest_tag(None, "nothing", _stamped(1, 1)[1].text) == body_tag
    assert messages.harvest_tag(None, None) is None
    assert body_tag != header_tag


# ---------------------------------------------------------------------------
# What the stamp must NOT touch
# ---------------------------------------------------------------------------


def test_the_subject_is_untouched_by_stamping():
    """The reason the tag is not in the subject. A subject carrying a
    synthetic suffix is not the subject that was sent, so verifying it would
    compare a mangled expectation against a mangled result and pass."""
    corpus = generate_corpus(11, 1)
    tag = messages.make_tag("hq", 11, 0)
    stamped = messages.stamp(corpus[0], tag)
    assert stamped.subject == corpus[0].subject
    assert stamped.ground_truth.subject == corpus[0].ground_truth.subject


def test_the_from_address_is_untouched_by_stamping():
    corpus = generate_corpus(11, 1)
    stamped = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0))
    assert stamped.from_addr == corpus[0].from_addr
    assert stamped.envelope == corpus[0].envelope
    assert stamped.ground_truth.from_addr == corpus[0].ground_truth.from_addr


def test_the_ground_truth_body_is_still_present_after_stamping():
    """The body check looks for the generator's substantive sentence inside
    what the system stored. Appending a marker must not push it out."""
    corpus = generate_corpus(11, 1)
    stamped = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0))
    assert corpus[0].ground_truth.body_core in stamped.text


def test_attachments_survive_stamping():
    from dataclasses import replace

    from testinghq.blast.payload import Attachment

    corpus = generate_corpus(11, 1)
    with_file = replace(
        corpus[0],
        attachments=(
            Attachment(filename="a.pdf", content_type="application/pdf", content=b"%PDF-1.4"),
        ),
    )
    stamped = messages.stamp(with_file, messages.make_tag("hq", 11, 0))
    assert [a.filename for a in stamped.attachments] == ["a.pdf"]
    assert stamped.attachments[0].content == b"%PDF-1.4"


def test_stamping_does_not_mutate_the_generated_corpus():
    """`InboundEmail` is frozen but its headers dict is not. Copying the dict
    is the whole reason this is safe, and a corpus shared between the stamping
    path and the firing path would otherwise end up double-tagged."""
    corpus = generate_corpus(11, 1)
    original_headers = dict(corpus[0].headers)
    messages.stamp(corpus[0], messages.make_tag("hq", 11, 0))
    assert corpus[0].headers == original_headers
    assert messages.TAG_HEADER not in corpus[0].headers


def test_two_stamps_of_one_payload_produce_identical_bytes():
    """This is what makes the duplicate redelivery scenario a duplicate. If the
    two stamps differed by so much as a marker count, a pipeline deduplicating
    on payload bytes would be right to keep both."""
    corpus = generate_corpus(11, 1)
    tag = messages.make_tag("hq", 11, 0)
    first = messages.stamp(corpus[0], tag)
    second = messages.stamp(corpus[0], tag)
    assert report.payload_sha256(first) == report.payload_sha256(second)
    assert first.text == second.text


# ---------------------------------------------------------------------------
# Threading headers
# ---------------------------------------------------------------------------


def test_stamping_can_pin_the_threading_headers():
    corpus = generate_corpus(11, 1)
    email = messages.stamp(
        corpus[0],
        messages.make_tag("hq", 11, 0),
        in_reply_to="<root@widgets.example>",
        references="<a@widgets.example> <root@widgets.example>",
    )
    assert email.headers["In-Reply-To"] == "<root@widgets.example>"
    assert email.headers["References"] == "<a@widgets.example> <root@widgets.example>"


def test_stamping_can_replace_the_subject_for_a_reply():
    corpus = generate_corpus(11, 1)
    email = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0), subject="Re: x")
    assert email.subject == "Re: x"


def test_stamping_omits_threading_headers_it_was_not_given():
    corpus = generate_corpus(11, 1)
    email = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0))
    assert "In-Reply-To" not in email.headers
    assert "References" not in email.headers


def test_message_id_of_strips_the_angle_brackets():
    corpus = generate_corpus(11, 1)
    email = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0), message_id="<x@y>")
    assert messages.message_id_of(email) == "x@y"


def test_a_pinned_null_message_id_is_ignored_rather_than_written():
    """`None` means "do not pin this header", which is different from writing
    an empty one. A scenario that does not control Message-IDs must not erase
    the one the generator produced."""
    corpus = generate_corpus(11, 1)
    generated = messages.message_id_of(corpus[0])
    email = messages.stamp(corpus[0], messages.make_tag("hq", 11, 0), message_id=None)
    assert messages.message_id_of(email) == generated


def test_a_generated_chain_message_id_is_on_a_reserved_domain():
    """Every address-shaped string this package emits has to satisfy the
    engine-wide synthetic-content contract, or a guardrail would refuse a run
    for a header the operator never asked for."""
    from testinghq.core import guardrails

    tag = messages.make_tag("hq", 11, 0)
    guardrails.require_synthetic_content([messages.build_chain_message_id(tag)])


# ---------------------------------------------------------------------------
# Probes and whole corpora
# ---------------------------------------------------------------------------


def test_a_probe_is_built_from_the_payload_and_the_hash():
    corpus = generate_corpus(4, 1)
    tag = messages.make_tag("hq", 4, 0)
    email = messages.stamp(corpus[0], tag)
    probe = messages.probe_for(email, tag, "clean-4-0000", "b" * 64)
    assert probe.tag == tag
    assert probe.record_id == "clean-4-0000"
    assert probe.payload_sha256 == "b" * 64
    assert probe.from_addr == corpus[0].ground_truth.from_addr
    assert probe.subject == corpus[0].ground_truth.subject
    assert probe.recipient == corpus[0].envelope.to[0]
    assert probe.message_id == messages.message_id_of(email)


def test_a_probe_keeps_the_recipient_for_the_routing_check():
    """The routing expectation falls back to the recipient, so the probe has
    to carry it or a routing check could not be graded at all."""
    corpus = generate_corpus(4, 1)
    email = messages.stamp(corpus[0], messages.make_tag("hq", 4, 0))
    probe = messages.probe_for(email, "t", "r", "c" * 64)
    assert probe.recipient in email.envelope.to


def test_stamp_corpus_gives_every_payload_its_own_tag_and_id():
    stamped = messages.stamp_corpus(generate_corpus(5, 4), 5, "spike")
    assert [tag for _e, tag, _r in stamped] == [
        "spike-5-0000",
        "spike-5-0001",
        "spike-5-0002",
        "spike-5-0003",
    ]
    assert [rid for _e, _t, rid in stamped] == [
        "clean-5-0000",
        "clean-5-0001",
        "clean-5-0002",
        "clean-5-0003",
    ]


def test_stamp_corpus_is_deterministic():
    first = messages.stamp_corpus(generate_corpus(5, 4), 5, "spike")
    second = messages.stamp_corpus(generate_corpus(5, 4), 5, "spike")
    assert [report.payload_sha256(e) for e, _t, _r in first] == [
        report.payload_sha256(e) for e, _t, _r in second
    ]


def test_a_stamped_payload_still_serializes_to_the_real_wire_format():
    """The tag lives in ordinary fields, so the transport path is unchanged. If
    the stamp needed a new part, the whole existing wire contract would have
    moved, and this is where it would show."""
    tag, email = _stamped()
    parts = to_multipart_parts(email)
    assert [p.name for p in parts][:9] == [
        "headers",
        "to",
        "from",
        "subject",
        "text",
        "html",
        "envelope",
        "charsets",
        "attachments",
    ]
    header_field = parts[0]
    assert f"{messages.TAG_HEADER}: {tag}\r\n" in header_field.value
    assert messages.tag_marker(tag) in dict(
        (p.name, p.value) for p in parts if hasattr(p, "value")
    )["text"]
