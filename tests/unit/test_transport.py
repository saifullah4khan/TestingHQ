from dataclasses import dataclass, field
from typing import List

import pytest

from testinghq.blast.payload import Envelope, GroundTruth, InboundEmail
from testinghq.core.transport import (
    ClientResponse,
    DEFAULT_BOUNDARY,
    PreparedRequest,
    build_request,
    encode_multipart,
    post,
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


@dataclass
class FakeClient:
    """Records every request; returns a scripted response or raises a
    scripted exception. No sockets, ever."""

    response: ClientResponse = None
    raises: Exception = None
    received: List[PreparedRequest] = field(default_factory=list)

    def send(self, request: PreparedRequest) -> ClientResponse:
        self.received.append(request)
        if self.raises is not None:
            raise self.raises
        return self.response


def _fake_clock(times):
    values = iter(times)

    def clock():
        return next(values)

    return clock


def test_post_records_request_against_fake_client():
    email = _make_email()
    client = FakeClient(response=ClientResponse(status=200, body=b"ok"))

    result = post(email, "https://sink.example.test/inbound", client)

    assert len(client.received) == 1
    request = client.received[0]
    assert request.url == "https://sink.example.test/inbound"
    assert request.method == "POST"
    assert result.status == 200
    assert result.body_snippet == "ok"
    assert result.error is None


def test_post_enforces_default_timeout():
    email = _make_email()
    client = FakeClient(response=ClientResponse(status=200, body=b"ok"))
    post(email, "https://sink.example.test/inbound", client)
    assert client.received[0].timeout == 10.0


def test_post_propagates_custom_timeout():
    email = _make_email()
    client = FakeClient(response=ClientResponse(status=200, body=b"ok"))
    post(email, "https://sink.example.test/inbound", client, timeout=2.5)
    assert client.received[0].timeout == 2.5


def test_post_records_transport_failure_without_raising():
    email = _make_email()
    client = FakeClient(raises=ConnectionRefusedError("connection refused"))

    result = post(email, "https://sink.example.test/inbound", client)

    assert result.status is None
    assert result.body_snippet == ""
    assert "connection refused" in result.error


def test_post_uses_injected_clock_for_latency():
    email = _make_email()
    client = FakeClient(response=ClientResponse(status=200, body=b"ok"))
    clock = _fake_clock([100.0, 100.25])

    result = post(email, "https://sink.example.test/inbound", client, clock=clock)

    assert result.latency_ms == pytest.approx(250.0)


def test_post_truncates_body_snippet():
    email = _make_email()
    long_body = b"x" * 500
    client = FakeClient(response=ClientResponse(status=200, body=long_body))

    result = post(email, "https://sink.example.test/inbound", client)

    assert len(result.body_snippet) == 200


def test_build_request_content_type_has_boundary():
    email = _make_email()
    request = build_request(email, "https://sink.example.test/inbound")
    assert DEFAULT_BOUNDARY in request.headers["Content-Type"]
    assert request.headers["Content-Type"].startswith("multipart/form-data")


def test_encode_multipart_contains_field_values_and_boundary():
    email = _make_email(subject="Unique Subject Marker")
    from testinghq.blast.serialize import to_multipart_parts

    body = encode_multipart(to_multipart_parts(email))
    text = body.decode("utf-8")
    assert f"--{DEFAULT_BOUNDARY}" in text
    assert 'name="subject"' in text
    assert "Unique Subject Marker" in text
    assert text.rstrip().endswith(f"--{DEFAULT_BOUNDARY}--")


def test_encode_multipart_includes_attachment_file_part():
    from testinghq.blast.payload import Attachment
    from testinghq.blast.serialize import to_multipart_parts

    attachment = Attachment(filename="a.txt", content_type="text/plain", content=b"hi")
    email = _make_email(attachments=(attachment,))
    body = encode_multipart(to_multipart_parts(email))
    text = body.decode("utf-8", errors="replace")
    assert 'name="attachment1"; filename="a.txt"' in text
    assert "Content-Type: text/plain" in text


def test_encode_multipart_is_deterministic():
    email = _make_email()
    from testinghq.blast.serialize import to_multipart_parts

    parts = to_multipart_parts(email)
    assert encode_multipart(parts) == encode_multipart(parts)


def test_post_default_client_is_not_used_when_fake_supplied():
    """Guard against accidentally falling back to the real network client
    when a fake is explicitly supplied."""
    email = _make_email()
    client = FakeClient(response=ClientResponse(status=204, body=b""))
    result = post(email, "https://sink.example.test/inbound", client=client)
    assert result.status == 204
    assert len(client.received) == 1
