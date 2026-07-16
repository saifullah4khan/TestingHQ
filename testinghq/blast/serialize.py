"""Serialize an InboundEmail into SendGrid Inbound Parse multipart fields.

SendGrid's Inbound Parse webhook posts multipart/form-data to your endpoint.
This module builds that in "parsed mode": the message is split into named
text fields (headers, to, from, subject, text, html, envelope, charsets,
attachment count, attachment-info) plus one file part per attachment
(attachment1, attachment2, ...), matching what SendGrid actually sends. See
https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook

This module only knows how to build that ordered list of fields; it has no
opinion about HTTP. testinghq/core/transport.py (a separate backlog item) is
expected to turn the result into an actual POST body.

Determinism: given the same InboundEmail, to_multipart_parts always returns
the same parts in the same order with the same bytes. JSON fields (envelope,
charsets, attachment-info) are encoded with sorted keys and compact
separators so their exact bytes do not depend on Python's dict ordering
beyond what the caller already fixed when building the InboundEmail.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Union

from .payload import InboundEmail


@dataclass(frozen=True)
class FormField:
    """A plain text multipart/form-data field."""

    name: str
    value: str


@dataclass(frozen=True)
class FormFile:
    """A file multipart/form-data part, one per attachment."""

    name: str
    filename: str
    content_type: str
    content: bytes


FormPart = Union[FormField, FormFile]


def _json(value) -> str:
    """Deterministic, compact JSON: sorted keys, no extra whitespace, so the
    same input always encodes to the same bytes regardless of dict build
    order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def envelope_json(email: InboundEmail) -> str:
    """The `envelope` field: valid JSON, `{"to": [...], "from": "..."}`."""
    return _json({"to": list(email.envelope.to), "from": email.envelope.from_addr})


def charsets_json(email: InboundEmail) -> str:
    """The `charsets` field: valid JSON mapping field name to charset."""
    return _json(dict(email.charsets))


def attachment_info_json(email: InboundEmail) -> str:
    """The `attachment-info` field: valid JSON mapping each attachmentN field
    name to its filename and content type."""
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
    """Build the ordered list of multipart/form-data parts a SendGrid Inbound
    Parse webhook would send for this email, in parsed mode.

    Field order: headers, to, from, subject, text, html, envelope, charsets,
    attachments (count), attachment-info (only present when there is at least
    one attachment), then attachment1..N as file parts in attachment order.
    """
    parts: List[FormPart] = [
        FormField("headers", headers_text(email.headers)),
        FormField("to", email.to),
        FormField("from", email.from_addr),
        FormField("subject", email.subject),
        FormField("text", email.text),
        FormField("html", email.html),
        FormField("envelope", envelope_json(email)),
        FormField("charsets", charsets_json(email)),
        FormField("attachments", str(len(email.attachments))),
    ]
    if email.attachments:
        parts.append(FormField("attachment-info", attachment_info_json(email)))
    for index, attachment in enumerate(email.attachments, start=1):
        parts.append(
            FormFile(
                name=f"attachment{index}",
                filename=attachment.filename,
                content_type=attachment.content_type,
                content=attachment.content,
            )
        )
    return parts
