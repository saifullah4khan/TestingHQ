"""The three non-SendGrid wire formats, against their documented shapes.

`sendgrid` and `mime` are checked against a specification: the shape this
project already emitted, and RFC 5322 and RFC 2046. `mailgun` and `postmark` are
written from those vendors' published field lists and have NOT been fired at a
live account of either. So their tests assert that the bytes match the documented
field names and that nothing is dropped or invented. They do not establish that
a real receiver accepts the body, and nothing here should be read as saying they
do. The end-to-end run that would is a separate task, against one real pipeline.

The tests that matter most are the ones about *not fabricating*. A format that
invents a field the vendor never sends, or fills a derived one badly, is worse
than one that omits it, because a receiving pipeline may prefer the invented
value and act on it.
"""
from __future__ import annotations

import base64
import json
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from testinghq.blast.payload import Attachment, Envelope, GroundTruth, InboundEmail
from testinghq.core.formats import get_format
from testinghq.core.transport import DEFAULT_BOUNDARY

URL = "http://127.0.0.1:9/inbound"


def _email(**overrides) -> InboundEmail:
    base = dict(
        to="support@example.com",
        from_addr="Alice <alice@example.com>",
        subject="Your order",
        text="Thanks for your order.",
        html="<p>Thanks for your order.</p>",
        envelope=Envelope(to=("support@example.com",),
                          from_addr="bounces@relay.example.com"),
        ground_truth=GroundTruth(
            from_addr="alice@example.com",
            subject="Your order",
            body_core="Thanks for your order.",
        ),
        headers={"Message-ID": "<m1@example.com>", "X-TestingHQ-Tag": "hq-1-0000"},
        charsets={"to": "UTF-8", "subject": "UTF-8"},
    )
    base.update(overrides)
    return InboundEmail(**base)


def _parts(body: bytes):
    """(name, value-or-bytes) for each multipart part, in order."""
    marker = b"--" + DEFAULT_BOUNDARY.encode()
    # The opening delimiter has no preceding CRLF, so it does not split. Strip
    # it explicitly; the first part lives inside that first chunk, and treating
    # the chunk as a delimiter throws the first field away.
    if body.startswith(marker):
        body = body[len(marker):]
    out = []
    for chunk in body.split(b"\r\n" + marker):
        if chunk.startswith(b"--") or not chunk.strip():
            # The closing delimiter, or padding.
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        name = None
        for line in head.decode().splitlines():
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece[5:].strip('"')
        out.append((name, data.rstrip(b"\r\n")))
    return out


def _fields(body: bytes):
    return {name: value for name, value in _parts(body) if name is not None}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_all_four_formats_are_registered():
    assert get_format_names() == ["mailgun", "mime", "postmark", "sendgrid"]


def get_format_names():
    from testinghq.core.formats import format_names

    return format_names()


# ---------------------------------------------------------------------------
# mailgun
# ---------------------------------------------------------------------------


def test_mailgun_uses_the_documented_field_names():
    """The vocabulary is the point. SendGrid's `text` and `html` become
    `body-plain` and `body-html`."""
    encoded = get_format("mailgun", DEFAULT_BOUNDARY).encode(_email())
    fields = _fields(encoded.body)
    for name in (
        "recipient",
        "sender",
        "from",
        "to",
        "subject",
        "body-plain",
        "body-html",
        "message-headers",
        "attachments",
    ):
        assert name in fields, f"mailgun sends {name!r}; got {sorted(fields)}"


def test_mailgun_keeps_the_envelope_sender_out_of_from():
    """The distinction the SendGrid shape flattens. A message relayed through a
    bounce handler has a real envelope sender that differs from the From header,
    and Mailgun has a field for exactly that."""
    payload = _email()
    fields = _fields(get_format("mailgun", DEFAULT_BOUNDARY).encode(payload).body)
    assert fields["sender"] == b"bounces@relay.example.com"
    assert fields["from"] == b"Alice <alice@example.com>"
    assert fields["sender"] != fields["from"]


def test_mailgun_sends_the_recipient_and_the_to_header_separately():
    fields = _fields(get_format("mailgun", DEFAULT_BOUNDARY).encode(_email()).body)
    assert fields["recipient"] == b"support@example.com"
    assert fields["to"] == b"support@example.com"


def test_mailgun_sends_the_raw_headers_as_one_blob():
    payload = _email()
    fields = _fields(get_format("mailgun", DEFAULT_BOUNDARY).encode(payload).body)
    assert b"Message-ID: <m1@example.com>" in fields["message-headers"]


def test_mailgun_does_not_invent_the_stripped_variants():
    """`stripped-text` and `stripped-html` are derived by Mailgun's own parser.
    Sending our approximation would be fabricating a value that a receiving
    pipeline might reasonably prefer over the real body."""
    fields = _fields(get_format("mailgun", DEFAULT_BOUNDARY).encode(_email()).body)
    assert "stripped-text" not in fields
    assert "stripped-html" not in fields


def test_mailgun_sends_each_attachment_as_a_numbered_file_part():
    payload = _email(attachments=(
        Attachment("a.txt", "text/plain", b"one"),
        Attachment("b.csv", "text/csv", b"two"),
    ))
    encoded = get_format("mailgun", DEFAULT_BOUNDARY).encode(payload)
    names = [name for name, _ in _parts(encoded.body)]
    assert names[-2:] == ["attachment1", "attachment2"]
    assert _fields(encoded.body)["attachments"] == b"2"


def test_mailgun_sends_no_attachment_parts_for_a_message_with_none():
    payload = _email()
    names = [name for name, _ in _parts(
        get_format("mailgun", DEFAULT_BOUNDARY).encode(payload).body)]
    assert not any(n and n.startswith("attachment") and n != "attachments" for n in names)
    assert _fields(
        get_format("mailgun", DEFAULT_BOUNDARY).encode(payload).body
    )["attachments"] == b"0"


def test_mailgun_is_multipart_and_announces_its_boundary():
    encoded = get_format("mailgun", DEFAULT_BOUNDARY).encode(_email())
    assert f"boundary={DEFAULT_BOUNDARY}" in encoded.content_type
    assert f"--{DEFAULT_BOUNDARY}".encode() in encoded.body


# ---------------------------------------------------------------------------
# postmark
# ---------------------------------------------------------------------------


def test_postmark_posts_one_json_document():
    encoded = get_format("postmark", DEFAULT_BOUNDARY).encode(_email())
    assert encoded.content_type == "application/json"
    assert isinstance(json.loads(encoded.body.decode("utf-8")), dict)


def test_postmark_uses_the_documented_pascal_case_names():
    """Not `from`, `text`, `html`. The casing is wrong-looking on purpose:
    Postmark's webhook payload is PascalCase and a receiver written against it
    looks the keys up by those names."""
    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(_email()).body.decode("utf-8")
    )
    for name in (
        "From", "To", "Cc", "Bcc", "Subject", "MessageID",
        "Headers", "HtmlBody", "TextBody", "ReplyTo", "Attachments",
    ):
        assert name in document, f"postmark sends {name!r}; got {sorted(document)}"


def test_postmark_headers_are_a_list_of_name_value_objects():
    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(_email()).body.decode("utf-8")
    )
    assert document["Headers"] == [
        {"Name": "Message-ID", "Value": "<m1@example.com>"},
        {"Name": "X-TestingHQ-Tag", "Value": "hq-1-0000"},
    ]


def test_postmark_from_is_the_header_not_the_envelope_sender():
    """Postmark's inbound webhook has no envelope field. So the envelope sender
    is not sent under an invented key, and a receiver cannot mistake a relayed
    bounce address for the message author."""
    payload = _email()
    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(payload).body.decode("utf-8")
    )
    assert document["From"] == "Alice <alice@example.com>"
    assert "bounces@relay.example.com" not in json.dumps(document)


def test_postmark_attachments_are_inline_base64():
    payload = _email(attachments=(Attachment("a.txt", "text/plain", b"one"),))
    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(payload).body.decode("utf-8")
    )
    assert document["Attachments"] == [
        {
            "Name": "a.txt",
            "ContentType": "text/plain",
            "ContentLength": 3,
            "Content": base64.b64encode(b"one").decode("ascii"),
        }
    ]


def test_postmark_sends_an_empty_attachment_list_rather_than_omitting_it():
    """A receiver iterating `body["Attachments"]` gets a KeyError on a
    message with no files. That is a receiver bug, and this format should not
    have to work around it."""
    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(_email()).body.decode("utf-8")
    )
    assert document["Attachments"] == []


def test_postmark_is_byte_stable_across_runs():
    """Determinism, which every other format here guarantees. Sorted keys, so
    the JSON does not depend on dict build order."""
    first = get_format("postmark", DEFAULT_BOUNDARY).encode(_email())
    second = get_format("postmark", DEFAULT_BOUNDARY).encode(_email())
    assert first.body == second.body


# ---------------------------------------------------------------------------
# mime
# ---------------------------------------------------------------------------


def test_mime_produces_a_message_a_parser_can_read():
    """Checked by parsing it back, not by asserting on substrings. The point of
    this format is that a real MIME parser accepts it."""
    encoded = get_format("mime", DEFAULT_BOUNDARY).encode(_email())
    parsed = message_from_bytes(encoded.body, policy=default_policy)
    assert parsed["To"] == "support@example.com"
    assert parsed["From"] == "Alice <alice@example.com>"
    assert parsed["Subject"] == "Your order"


def test_mime_carries_both_bodies_as_alternatives():
    parsed = message_from_bytes(
        get_format("mime", DEFAULT_BOUNDARY).encode(_email()).body,
        policy=default_policy,
    )
    assert parsed.is_multipart()
    types = [part.get_content_type() for part in parsed.iter_parts()]
    assert "text/plain" in types
    assert "text/html" in types


def test_mime_does_not_duplicate_a_structural_header_the_payload_carried():
    """A payload with its own Content-Type would otherwise produce a message
    with two, and the wrong one wins depending on the parser."""
    payload = _email(headers={
        "Message-ID": "<m1@example.com>",
        "Content-Type": "text/plain",
        "MIME-Version": "1.0",
    })
    parsed = message_from_bytes(
        get_format("mime", DEFAULT_BOUNDARY).encode(payload).body,
        policy=default_policy,
    )
    assert len(parsed.get_all("Content-Type", [])) == 1
    assert len(parsed.get_all("MIME-Version", [])) == 1


def test_mime_keeps_the_testing_tag_header():
    """The tag is how a pipeline matches a delivered message back to a payload.
    A raw-message format that dropped it would break readback entirely."""
    parsed = message_from_bytes(
        get_format("mime", DEFAULT_BOUNDARY).encode(_email()).body,
        policy=default_policy,
    )
    assert parsed["X-TestingHQ-Tag"] == "hq-1-0000"


def test_mime_uses_a_fixed_boundary_so_the_bytes_are_reproducible():
    """`email` picks a random boundary per message by default, which would make
    the same payload produce different bytes every run and break the
    determinism every other format here guarantees."""
    first = get_format("mime", DEFAULT_BOUNDARY).encode(_email())
    second = get_format("mime", DEFAULT_BOUNDARY).encode(_email())
    assert first.body == second.body
    assert DEFAULT_BOUNDARY.encode() in first.body


def test_mime_handles_a_message_with_no_html():
    """Not every payload has both parts, and a format that assumed otherwise
    would fail on a plain-text corpus."""
    parsed = message_from_bytes(
        get_format("mime", DEFAULT_BOUNDARY)
        .encode(_email(html=""))
        .body,
        policy=default_policy,
    )
    assert parsed.get_content_type() == "text/plain"
    assert "Thanks for your order." in parsed.get_content()


def test_mime_attaches_files_as_real_parts():
    payload = _email(attachments=(
        Attachment("report.csv", "text/csv", b"a,b\n1,2\n"),
    ))
    encoded = get_format("mime", DEFAULT_BOUNDARY).encode(payload)
    parsed = message_from_bytes(encoded.body, policy=default_policy)
    names = [
        part.get_filename()
        for part in parsed.walk()
        if part.get_filename()
    ]
    assert names == ["report.csv"]


def test_mime_encodes_non_ascii_rather_than_sending_raw_bytes():
    """A raw-8bit header is a parser-dependent guess. The stdlib emits an
    RFC 2047 encoded word instead, and a corpus with real accented characters
    is the normal case, not an edge case."""
    payload = _email(subject="Facturación número 7", text="Merci pour votre commande.")
    encoded = get_format("mime", DEFAULT_BOUNDARY).encode(payload)
    raw = payload.subject.encode("utf-8")
    assert raw not in encoded.body, "an unencoded utf-8 header is a parser guess"
    assert b"=?utf-8?" in encoded.body.lower()
    parsed = message_from_bytes(encoded.body, policy=default_policy)
    assert parsed["Subject"] == "Facturación número 7"


# ---------------------------------------------------------------------------
# Cross-format
# ---------------------------------------------------------------------------


def test_no_format_invents_a_field_the_payload_does_not_have():
    """Each format sends what the payload carries. A format that emitted a
    fixed set of fields regardless would let a receiver act on a value
    TestingHQ never generated."""
    payload = _email(subject="", text="", html="")
    for name in ("sendgrid", "mailgun"):
        fields = _fields(get_format(name, DEFAULT_BOUNDARY).encode(payload).body)
        assert fields["subject"] == b""
        assert fields.get("body-plain", fields.get("text")) == b""

    document = json.loads(
        get_format("postmark", DEFAULT_BOUNDARY).encode(payload).body.decode("utf-8")
    )
    assert document["Subject"] == ""
    assert document["TextBody"] == ""


def test_every_format_puts_the_tag_somewhere_a_pipeline_can_find_it():
    """The property the readback side depends on, checked across all four."""
    for name in ("sendgrid", "mailgun", "postmark", "mime"):
        body = get_format(name, DEFAULT_BOUNDARY).encode(_email()).body
        assert b"hq-1-0000" in body, f"{name} dropped the TestingHQ tag"
