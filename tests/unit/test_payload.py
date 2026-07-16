import dataclasses

import pytest

from testinghq.blast.payload import (
    Attachment,
    Envelope,
    GroundTruth,
    InboundEmail,
)


def _make_email(**overrides):
    defaults = dict(
        to="recipient@example.com",
        from_addr="sender@example.com",
        subject="Hello",
        text="hello world",
        html="<p>hello world</p>",
        envelope=Envelope(to=("recipient@example.com",), from_addr="sender@example.com"),
        ground_truth=GroundTruth(
            from_addr="sender@example.com", subject="Hello", body_core="hello world"
        ),
    )
    defaults.update(overrides)
    return InboundEmail(**defaults)


def test_constructs_with_all_fields():
    attachment = Attachment(filename="a.txt", content_type="text/plain", content=b"hi")
    email = _make_email(
        headers={"X-Test": "1"},
        charsets={"subject": "UTF-8"},
        attachments=(attachment,),
    )
    assert email.to == "recipient@example.com"
    assert email.from_addr == "sender@example.com"
    assert email.subject == "Hello"
    assert email.text == "hello world"
    assert email.html == "<p>hello world</p>"
    assert email.envelope.to == ("recipient@example.com",)
    assert email.envelope.from_addr == "sender@example.com"
    assert email.headers == {"X-Test": "1"}
    assert email.charsets == {"subject": "UTF-8"}
    assert email.attachments == (attachment,)
    assert email.ground_truth.body_core == "hello world"


def test_defaults_are_empty_and_independent_between_instances():
    first = _make_email()
    second = _make_email()
    assert first.headers == {}
    assert first.charsets == {}
    assert first.attachments == ()
    # default_factory dicts/tuples must not be shared mutable state
    assert first.headers is not second.headers
    assert first.charsets is not second.charsets


def test_inbound_email_is_frozen():
    email = _make_email()
    with pytest.raises(dataclasses.FrozenInstanceError):
        email.subject = "changed"


def test_envelope_is_frozen():
    envelope = Envelope(to=("a@example.com",), from_addr="b@example.com")
    with pytest.raises(dataclasses.FrozenInstanceError):
        envelope.from_addr = "c@example.com"


def test_attachment_is_frozen():
    attachment = Attachment(filename="a.txt", content_type="text/plain", content=b"hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        attachment.filename = "b.txt"


def test_attachment_rejects_non_bytes_content():
    with pytest.raises(TypeError):
        Attachment(filename="a.txt", content_type="text/plain", content="not bytes")


def test_attachment_accepts_bytearray_content():
    attachment = Attachment(
        filename="a.txt", content_type="text/plain", content=bytearray(b"hi")
    )
    assert bytes(attachment.content) == b"hi"


def test_model_is_deliberately_permissive_about_degenerate_content():
    """Blast generates the clean-to-garbled spectrum, including degenerate
    payloads (M2). The model must not reject empty subjects, empty envelope
    recipient lists, or empty attachment filenames; those are exactly the
    edge cases Blast is built to produce."""
    email = _make_email(
        subject="",
        to="",
        envelope=Envelope(to=(), from_addr=""),
        attachments=(Attachment(filename="", content_type="", content=b""),),
    )
    assert email.subject == ""
    assert email.to == ""
    assert email.envelope.to == ()
    assert email.attachments[0].filename == ""


def test_ground_truth_is_frozen():
    ground_truth = GroundTruth(
        from_addr="sender@example.com", subject="Hello", body_core="hello world"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ground_truth.subject = "changed"


def test_ground_truth_is_a_required_field():
    """InboundEmail carries ground truth for later assertion; it is not an
    optional afterthought, so constructing without it must fail."""
    with pytest.raises(TypeError):
        InboundEmail(
            to="a@example.com",
            from_addr="b@example.com",
            subject="s",
            text="t",
            html="<p></p>",
            envelope=Envelope(to=("a@example.com",), from_addr="b@example.com"),
        )
