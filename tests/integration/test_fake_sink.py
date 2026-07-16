"""Hermetic tests for the fake in-process sink (fake_sink.py).

No network: every test in this file exercises encode_multipart /
decode_multipart / FakeSink entirely in memory. These prove the sink itself
is correct (round-trips real multipart/form-data bytes without loss) before
tests/integration/test_intake_happy_path.py relies on it to check
serialize.py's output.
"""
import pytest

from fake_sink import (
    DEFAULT_BOUNDARY,
    DecodedAttachment,
    FakeSink,
    MultipartError,
    content_type_for_boundary,
    decode_multipart,
    encode_multipart,
)
from testinghq.blast.serialize import FormField, FormFile


def test_encode_decode_round_trips_plain_fields():
    parts = [
        FormField("to", "recipient@example.com"),
        FormField("subject", "Hello there"),
        FormField("text", "hello world"),
    ]
    body, content_type = encode_multipart(parts)
    fields = decode_multipart(body, content_type)
    assert fields == {
        "to": "recipient@example.com",
        "subject": "Hello there",
        "text": "hello world",
    }


def test_encode_decode_round_trips_file_parts():
    parts = [
        FormField("attachments", "1"),
        FormFile(
            name="attachment1",
            filename="a.txt",
            content_type="text/plain",
            content=b"hi there",
        ),
    ]
    body, content_type = encode_multipart(parts)
    fields = decode_multipart(body, content_type)
    assert fields["attachments"] == "1"
    attachment = fields["attachment1"]
    assert isinstance(attachment, DecodedAttachment)
    assert attachment.filename == "a.txt"
    assert attachment.content_type == "text/plain"
    assert attachment.content == b"hi there"


def test_round_trip_preserves_binary_content_ending_in_crlf_bytes():
    """A naive decoder that strips arbitrary trailing \\r\\n bytes instead of
    exactly the framing it added would corrupt attachment content that
    itself ends in \\r or \\n. Guard against that regression."""
    tricky_content = b"line one\r\nline two\r\n"
    parts = [
        FormField("attachments", "1"),
        FormFile(
            name="attachment1",
            filename="tricky.bin",
            content_type="application/octet-stream",
            content=tricky_content,
        ),
    ]
    body, content_type = encode_multipart(parts)
    fields = decode_multipart(body, content_type)
    assert fields["attachment1"].content == tricky_content


def test_round_trip_preserves_empty_string_field():
    parts = [FormField("subject", "")]
    body, content_type = encode_multipart(parts)
    assert decode_multipart(body, content_type) == {"subject": ""}


def test_round_trip_preserves_unicode_text():
    parts = [FormField("text", "café ☃ 你好")]
    body, content_type = encode_multipart(parts)
    assert decode_multipart(body, content_type) == {"text": "café ☃ 你好"}


def test_round_trip_preserves_empty_attachment_filename():
    """InboundEmail permits degenerate empty filenames (M2 groundwork); the
    sink must round-trip them rather than choke on the empty quoted value."""
    parts = [
        FormFile(name="attachment1", filename="", content_type="", content=b""),
    ]
    body, content_type = encode_multipart(parts)
    fields = decode_multipart(body, content_type)
    attachment = fields["attachment1"]
    assert attachment.filename == ""
    assert attachment.content_type == ""
    assert attachment.content == b""


def test_encode_is_deterministic():
    parts = [FormField("subject", "Hello"), FormField("text", "world")]
    first, first_ct = encode_multipart(parts)
    second, second_ct = encode_multipart(parts)
    assert first == second
    assert first_ct == second_ct


def test_content_type_includes_boundary():
    assert content_type_for_boundary("abc123") == "multipart/form-data; boundary=abc123"


def test_encode_rejects_unsafe_boundary():
    with pytest.raises(MultipartError):
        encode_multipart([FormField("a", "b")], boundary="has\r\nnewline")


def test_decode_rejects_missing_boundary_in_content_type():
    with pytest.raises(MultipartError):
        decode_multipart(b"whatever", "multipart/form-data")


def test_decode_rejects_body_with_no_delimiters():
    with pytest.raises(MultipartError):
        decode_multipart(b"not multipart at all", content_type_for_boundary(DEFAULT_BOUNDARY))


def test_fake_sink_records_received_payloads_in_order():
    sink = FakeSink()
    first_parts = [FormField("subject", "first")]
    second_parts = [FormField("subject", "second")]

    first_response = sink.receive_parts(first_parts)
    second_response = sink.receive_parts(second_parts)

    assert len(sink.received) == 2
    assert sink.received[0] is first_response
    assert sink.received[1] is second_response
    assert sink.received[0].decoded.subject == "first"
    assert sink.received[1].decoded.subject == "second"


def test_fake_sink_response_has_status_200():
    sink = FakeSink()
    response = sink.receive_parts([FormField("subject", "hi")])
    assert response.status_code == 200


def test_fake_sink_reset_clears_received():
    sink = FakeSink()
    sink.receive_parts([FormField("subject", "hi")])
    assert len(sink.received) == 1
    sink.reset()
    assert sink.received == []
