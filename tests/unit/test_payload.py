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


# ---------------------------------------------------------------------------
# with_changes keeps headers and raw_fields consistent
# ---------------------------------------------------------------------------


def _with_headers(**overrides):
    return _make_email(
        headers={
            "From": "sender@example.com",
            "To": "recipient@example.com",
            "Subject": "Hello",
            "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
        },
        **overrides,
    )


def test_with_changes_rewrites_the_matching_headers():
    email = _with_headers().with_changes(
        subject="New subject", to="Bob <bob@example.com>", from_addr="a@example.org"
    )
    assert email.headers["Subject"] == "New subject"
    assert email.headers["To"] == "Bob <bob@example.com>"
    assert email.headers["From"] == "a@example.org"
    assert list(email.headers) == ["From", "To", "Subject", "Date"]


def test_with_changes_rfc2047_encodes_a_non_ascii_subject_and_keeps_the_field_decoded():
    from email.header import decode_header, make_header

    email = _with_headers().with_changes(subject="Café réservation 订单")
    assert email.subject == "Café réservation 订单"
    encoded = email.headers["Subject"]
    assert encoded.isascii() and encoded.startswith("=?UTF-8?B?")
    assert str(make_header(decode_header(encoded))) == email.subject
    assert all(len(word) <= 75 for word in encoded.split(" "))


def test_with_changes_encodes_only_the_display_name_of_an_address():
    email = _with_headers().with_changes(from_addr="Zoë Müller <zoe@example.com>")
    header = email.headers["From"]
    assert header.endswith(" <zoe@example.com>")
    assert header.startswith("=?UTF-8?B?")


def test_with_changes_matches_header_names_case_insensitively():
    email = _make_email(headers={"subject": "Hello"}).with_changes(subject="Bye")
    assert email.headers == {"subject": "Bye"}


def test_with_changes_leaves_headers_alone_when_they_are_passed_explicitly():
    email = _with_headers().with_changes(subject="x", headers={"Subject": "y"})
    assert email.headers == {"Subject": "y"}


def test_with_changes_drops_a_stale_raw_field_and_keeps_the_others():
    email = _make_email(raw_fields={"subject": b"\xe9", "text": b"\xe9"})
    changed = email.with_changes(subject="plain")
    assert changed.raw_fields == {"text": b"\xe9"}
    from_changed = _make_email(raw_fields={"from": b"x"}).with_changes(from_addr="y@example.com")
    assert from_changed.raw_fields == {}


def test_with_changes_does_not_touch_its_input():
    original = _with_headers()
    original.with_changes(subject="other")
    assert original.headers["Subject"] == "Hello"
