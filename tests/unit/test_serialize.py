import json

from testinghq.blast.payload import Attachment, Envelope, GroundTruth, InboundEmail
from testinghq.blast.serialize import (
    FormField,
    FormFile,
    attachment_info_json,
    charsets_json,
    envelope_json,
    headers_text,
    to_multipart_parts,
)


def _make_email(**overrides):
    defaults = dict(
        to="recipient@example.com",
        from_addr="sender@example.com",
        subject="Hello there",
        text="hello world",
        html="<p>hello world</p>",
        envelope=Envelope(
            to=("recipient@example.com", "cc@example.com"),
            from_addr="sender@example.com",
        ),
        ground_truth=GroundTruth(
            from_addr="sender@example.com",
            subject="Hello there",
            body_core="hello world",
        ),
        headers={"Subject": "Hello there", "X-Custom": "yes"},
        charsets={"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "html": "UTF-8"},
    )
    defaults.update(overrides)
    return InboundEmail(**defaults)


def test_envelope_json_round_trips_to_and_from():
    email = _make_email()
    decoded = json.loads(envelope_json(email))
    assert decoded == {
        "to": ["recipient@example.com", "cc@example.com"],
        "from": "sender@example.com",
    }


def test_charsets_json_round_trips():
    email = _make_email()
    decoded = json.loads(charsets_json(email))
    assert decoded == {"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "html": "UTF-8"}


def test_charsets_json_empty_is_valid_json_object():
    email = _make_email(charsets={})
    assert json.loads(charsets_json(email)) == {}


def test_headers_text_renders_crlf_lines_in_order():
    email = _make_email(headers={"Subject": "Hello there", "X-Custom": "yes"})
    text = headers_text(email.headers)
    assert text == "Subject: Hello there\r\nX-Custom: yes\r\n"


def test_attachment_info_json_only_covers_present_attachments():
    email = _make_email(
        attachments=(
            Attachment(filename="a.txt", content_type="text/plain", content=b"hi"),
            Attachment(filename="b.png", content_type="image/png", content=b"\x89PNG"),
        )
    )
    decoded = json.loads(attachment_info_json(email))
    assert decoded == {
        "attachment1": {"filename": "a.txt", "type": "text/plain"},
        "attachment2": {"filename": "b.png", "type": "image/png"},
    }


def test_to_multipart_parts_field_order_and_values_without_attachments():
    email = _make_email()
    parts = to_multipart_parts(email)

    names = [part.name for part in parts]
    assert names == [
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
    by_name = {part.name: part for part in parts}
    assert all(isinstance(part, FormField) for part in parts)
    assert by_name["to"].value == "recipient@example.com"
    assert by_name["from"].value == "sender@example.com"
    assert by_name["subject"].value == "Hello there"
    assert by_name["text"].value == "hello world"
    assert by_name["html"].value == "<p>hello world</p>"
    assert by_name["attachments"].value == "0"
    assert json.loads(by_name["envelope"].value)["from"] == "sender@example.com"


def test_to_multipart_parts_includes_attachment_info_and_files_when_present():
    attachment = Attachment(filename="a.txt", content_type="text/plain", content=b"hi")
    email = _make_email(attachments=(attachment,))
    parts = to_multipart_parts(email)

    names = [part.name for part in parts]
    assert names == [
        "headers",
        "to",
        "from",
        "subject",
        "text",
        "html",
        "envelope",
        "charsets",
        "attachments",
        "attachment-info",
        "attachment1",
    ]
    by_name = {part.name: part for part in parts}
    assert by_name["attachments"].value == "1"

    file_part = by_name["attachment1"]
    assert isinstance(file_part, FormFile)
    assert file_part.filename == "a.txt"
    assert file_part.content_type == "text/plain"
    assert file_part.content == b"hi"


def test_to_multipart_parts_omits_attachment_info_when_no_attachments():
    email = _make_email()
    names = [part.name for part in to_multipart_parts(email)]
    assert "attachment-info" not in names


def test_to_multipart_parts_orders_attachments_by_position():
    first = Attachment(filename="a.txt", content_type="text/plain", content=b"a")
    second = Attachment(filename="b.txt", content_type="text/plain", content=b"b")
    email = _make_email(attachments=(first, second))
    parts = to_multipart_parts(email)

    file_parts = [part for part in parts if isinstance(part, FormFile)]
    assert [part.name for part in file_parts] == ["attachment1", "attachment2"]
    assert [part.content for part in file_parts] == [b"a", b"b"]


def test_to_multipart_parts_is_deterministic_across_calls():
    email = _make_email(
        attachments=(
            Attachment(filename="a.txt", content_type="text/plain", content=b"hi"),
        )
    )
    first = to_multipart_parts(email)
    second = to_multipart_parts(email)
    assert first == second


def test_to_multipart_parts_is_deterministic_across_equivalent_emails():
    """Same logical content built independently (two separate InboundEmail
    instances) must serialize to byte-identical parts."""
    email_a = _make_email()
    email_b = _make_email()
    assert to_multipart_parts(email_a) == to_multipart_parts(email_b)
