"""Fake in-process sink for Blast integration tests.

This stands in for a real intake endpoint during tests. It round-trips a
generated payload through the same wire format Blast will actually send
(SendGrid Inbound Parse multipart/form-data, per
``testinghq.blast.serialize.to_multipart_parts``), then decodes it back into
plain fields, exactly as a real receiving endpoint would have to. Nothing
here touches a socket: encode -> decode happens entirely in memory, so tests
stay hermetic (no network) while still proving a payload survives the real
wire format intact, not just object identity.

This module has no dependency on ``testinghq.core.transport``, which does not
exist yet (a Lane A backlog item, M1). Once it lands, transport.py is expected
to build a multipart body of this same shape and POST it with an injectable
HTTP client; ``FakeSink.post`` (raw bytes in, decoded fields out) is the
natural target for that client in a later test, without ever making a real
request. Until then, ``FakeSink.receive_parts`` lets tests go straight from
the parts ``to_multipart_parts`` already produces to a decoded, recorded
payload.

Determinism: encoding uses a fixed default boundary and never reads the
clock or any unseeded randomness, so the same parts always produce the same
bytes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from testinghq.blast.serialize import FormField, FormFile, FormPart

DEFAULT_BOUNDARY = "testinghqfakesinkboundary"

_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')
_BOUNDARY_RE = re.compile(r"boundary=([^\s;]+)")


class MultipartError(ValueError):
    """Raised when multipart bytes cannot be encoded or decoded correctly.

    The fake sink fails loud on malformed input rather than silently
    skipping parts: a decode bug here would hide real defects in
    ``to_multipart_parts`` instead of catching them.
    """


@dataclass(frozen=True)
class DecodedAttachment:
    """One file part decoded back out of a multipart body."""

    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class DecodedPayload:
    """Everything the fake sink decoded out of one multipart body, with field
    names mirroring ``testinghq.blast.payload.InboundEmail`` so tests can
    compare the two directly."""

    headers_text: str
    to: str
    from_addr: str
    subject: str
    text: str
    html: str
    envelope: dict
    charsets: dict
    attachment_count: int
    attachments: Tuple[DecodedAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FakeResponse:
    """What the fake sink hands back for one received payload, standing in
    for an HTTP response (status_code) plus what a real assertion pass would
    need (the decoded fields)."""

    status_code: int
    content_type: str
    body: bytes
    decoded: DecodedPayload


def content_type_for_boundary(boundary: str) -> str:
    return f"multipart/form-data; boundary={boundary}"


def _check_boundary(boundary: str) -> None:
    if not boundary or "\r" in boundary or "\n" in boundary:
        raise MultipartError(f"unsafe multipart boundary: {boundary!r}")


def encode_multipart(
    parts: Sequence[FormPart], boundary: str = DEFAULT_BOUNDARY
) -> Tuple[bytes, str]:
    """Encode FormField/FormFile parts (as produced by
    ``testinghq.blast.serialize.to_multipart_parts``) into real
    multipart/form-data bytes, the same shape a POST body from
    ``testinghq.core.transport`` (once built) would send.

    Returns ``(body, content_type)``. Deterministic: the same parts and
    boundary always yield the same bytes.
    """
    _check_boundary(boundary)
    chunks: List[bytes] = []
    for part in parts:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        if isinstance(part, FormField):
            chunks.append(
                f'Content-Disposition: form-data; name="{part.name}"\r\n\r\n'.encode(
                    "utf-8"
                )
            )
            chunks.append(part.value.encode("utf-8"))
        elif isinstance(part, FormFile):
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{part.name}"; '
                    f'filename="{part.filename}"\r\n'
                    f"Content-Type: {part.content_type}\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(part.content)
        else:
            raise MultipartError(f"unknown multipart part type: {type(part).__name__}")
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), content_type_for_boundary(boundary)


def _boundary_from_content_type(content_type: str) -> str:
    match = _BOUNDARY_RE.search(content_type)
    if not match:
        raise MultipartError(f"no boundary found in content type: {content_type!r}")
    return match.group(1).strip('"')


def _param(header_value: str, param_name: str) -> Optional[str]:
    for key, value in _PARAM_RE.findall(header_value):
        if key == param_name:
            return value
    return None


def _parse_part_headers(header_text: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in header_text.split("\r\n"):
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers[name.strip().lower()] = value.strip()
    return headers


def decode_multipart(
    body: bytes, content_type: str
) -> Dict[str, Union[str, DecodedAttachment]]:
    """Decode a real multipart/form-data body back into a mapping of field
    name to value (plain fields as ``str``, file parts as
    ``DecodedAttachment``).

    Raises ``MultipartError`` on malformed input rather than silently
    dropping parts.
    """
    boundary = _boundary_from_content_type(content_type)
    delimiter = b"--" + boundary.encode("utf-8")
    sections = body.split(delimiter)
    if len(sections) < 2:
        raise MultipartError("multipart body has no boundary delimiters")

    fields: Dict[str, Union[str, DecodedAttachment]] = {}
    # sections[0] is the preamble before the first delimiter (empty for a
    # body we built with encode_multipart); sections[-1] is the closing
    # "--\r\n" suffix. Everything in between is one framed part, each
    # exactly "\r\n" + headers + "\r\n\r\n" + payload + "\r\n" by
    # construction.
    for raw in sections[1:-1]:
        if not (raw.startswith(b"\r\n") and raw.endswith(b"\r\n")):
            raise MultipartError(f"malformed multipart part framing: {raw!r}")
        section = raw[2:-2]
        header_blob, sep, payload = section.partition(b"\r\n\r\n")
        if not sep:
            raise MultipartError(
                f"multipart part missing header/body separator: {section!r}"
            )
        headers = _parse_part_headers(header_blob.decode("utf-8", "replace"))
        disposition = headers.get("content-disposition", "")
        name = _param(disposition, "name")
        if name is None:
            raise MultipartError(f"multipart part missing a name: {header_blob!r}")
        filename = _param(disposition, "filename")
        if filename is not None:
            fields[name] = DecodedAttachment(
                filename=filename,
                content_type=headers.get("content-type", ""),
                content=payload,
            )
        else:
            fields[name] = payload.decode("utf-8")
    return fields


def _fields_to_payload(
    fields: Dict[str, Union[str, DecodedAttachment]]
) -> DecodedPayload:
    count = int(fields.get("attachments", "0") or "0")
    attachments = []
    for index in range(1, count + 1):
        key = f"attachment{index}"
        value = fields.get(key)
        if not isinstance(value, DecodedAttachment):
            raise MultipartError(f"expected file part {key!r}, found {value!r}")
        attachments.append(value)
    return DecodedPayload(
        headers_text=fields.get("headers", "") or "",
        to=fields.get("to", "") or "",
        from_addr=fields.get("from", "") or "",
        subject=fields.get("subject", "") or "",
        text=fields.get("text", "") or "",
        html=fields.get("html", "") or "",
        envelope=json.loads(fields["envelope"]) if "envelope" in fields else {},
        charsets=json.loads(fields["charsets"]) if "charsets" in fields else {},
        attachment_count=count,
        attachments=tuple(attachments),
    )


class FakeSink:
    """An in-process stand-in for an intake endpoint. Records every payload
    it receives, intact, in the order received.
    """

    def __init__(self) -> None:
        self.received: List[FakeResponse] = []

    def post(self, body: bytes, content_type: str) -> FakeResponse:
        """Receive a raw multipart body plus its content type, exactly what
        an intake endpoint's request handler would see. Decodes, records,
        and returns a fake response."""
        decoded = _fields_to_payload(decode_multipart(body, content_type))
        response = FakeResponse(
            status_code=200, content_type=content_type, body=body, decoded=decoded
        )
        self.received.append(response)
        return response

    def receive_parts(
        self, parts: Sequence[FormPart], boundary: str = DEFAULT_BOUNDARY
    ) -> FakeResponse:
        """Convenience: encode FormField/FormFile parts to real multipart
        bytes, then post them, in one call. This is the full dry-run path a
        test needs: serialize.to_multipart_parts(email) -> here."""
        body, content_type = encode_multipart(parts, boundary=boundary)
        return self.post(body, content_type)

    def reset(self) -> None:
        self.received = []
