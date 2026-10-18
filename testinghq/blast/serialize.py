"""Serialize an InboundEmail into SendGrid Inbound Parse multipart fields.

SendGrid's Inbound Parse webhook posts multipart/form-data. This module builds
the parsed-mode field list: headers, to, from, subject, text, html, envelope,
charsets, attachments, attachment-info, then one file part per attachment. See
https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook

It has no opinion about HTTP; testinghq/core/transport.py encodes the parts.

A field named in `InboundEmail.raw_fields` is emitted as those exact bytes,
which is how non-UTF-8 content reaches the wire. `extra_form_fields` are
appended last, in order.

Determinism: the same InboundEmail always yields the same parts. JSON fields
use sorted keys and compact separators.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Union

from .payload import InboundEmail


@dataclass(frozen=True)
class FormField:
    """A plain multipart/form-data field. `value` is text (sent as UTF-8) or
    exact bytes. An empty `name` is written with no name parameter."""

    name: str
    value: Union[str, bytes]


@dataclass(frozen=True)
class FormFile:
    """A file multipart/form-data part, one per attachment."""

    name: str
    filename: str
    content_type: str
    content: bytes


FormPart = Union[FormField, FormFile]


def _json(value) -> str:
    """Deterministic, compact JSON: sorted keys, no extra whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def envelope_json(email: InboundEmail) -> str:
    """The `envelope` field: `{"to": [...], "from": "..."}`."""
    return _json({"to": list(email.envelope.to), "from": email.envelope.from_addr})


def charsets_json(email: InboundEmail) -> str:
    """The `charsets` field: JSON mapping field name to charset."""
    return _json(dict(email.charsets))


def attachment_info_json(email: InboundEmail) -> str:
    """The `attachment-info` field: JSON mapping each attachmentN field name
    to its filename and content type."""
    info = {}
    for index, attachment in enumerate(email.attachments, start=1):
        info[f"attachment{index}"] = {
            "filename": attachment.filename,
            "type": attachment.content_type,
        }
    return _json(info)


def headers_text(headers) -> str:
    """Render a header mapping as raw RFC 5322 header text, one "Name: value"
    line per entry (CRLF-terminated), in the mapping's iteration order."""
    return "".join(f"{name}: {value}\r\n" for name, value in headers.items())


def to_multipart_parts(email: InboundEmail) -> List[FormPart]:
    """The ordered multipart/form-data parts SendGrid would post for `email`.

    Order: headers, to, from, subject, text, html, envelope, charsets,
    attachments (count), attachment-info (only with at least one attachment),
    attachment1..N, then any `extra_form_fields`.
    """
    raw = email.raw_fields

    def text_field(name: str, value: str) -> FormField:
        return FormField(name, raw[name] if name in raw else value)

    parts: List[FormPart] = [
        text_field("headers", headers_text(email.headers)),
        text_field("to", email.to),
        text_field("from", email.from_addr),
        text_field("subject", email.subject),
        text_field("text", email.text),
        text_field("html", email.html),
        text_field("envelope", envelope_json(email)),
        text_field("charsets", charsets_json(email)),
        FormField("attachments", str(len(email.attachments))),
    ]
    if email.attachments:
        parts.append(text_field("attachment-info", attachment_info_json(email)))
    for index, attachment in enumerate(email.attachments, start=1):
        parts.append(
            FormFile(
                name=f"attachment{index}",
                filename=attachment.filename,
                content_type=attachment.content_type,
                content=attachment.content,
            )
        )
    for name, value in email.extra_form_fields:
        parts.append(FormField(name, value))
    return parts


def part_value_bytes(part: FormField) -> bytes:
    """The bytes a FormField puts on the wire."""
    if isinstance(part.value, (bytes, bytearray)):
        return bytes(part.value)
    return part.value.encode("utf-8")
