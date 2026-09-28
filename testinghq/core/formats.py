"""Wire formats: how an InboundEmail becomes request bytes.

Until this module existed there was exactly one wire format, multipart/form-data
in the SendGrid Inbound Parse shape, hardcoded into `build_request`. That is the
right format for a SendGrid webhook and the wrong one for everything else, so
most users could not point Blast at their own pipeline without proxying through
something that translated for them.

A format is one method: take an `InboundEmail`, return bytes plus a content type.
Nothing else. A format does not know about HTTP, about targets, about the
guardrails, or about the clock, which is what keeps adding one from being a
change to the sending machinery.

Determinism is a property of every format here, not just the original. The same
InboundEmail must produce the same bytes every time, because run artifacts
record what was sent and a replay has to match. That rules out anything whose
output depends on dict iteration order or on the wall clock, and it is why the
JSON formats sort their keys.

`sendgrid` is the default and is byte-for-byte what this project emitted before
the format layer existed. That is the compatibility promise: an existing config
with no `format` key keeps producing the identical request, and the test that
says so compares against bytes captured before this module did.

PROVENANCE, because it decides how much a passing test is worth. `sendgrid` and
`mime` are the shapes this project already emitted or that RFC 5322 and RFC 2046
define, so their tests are checks against a specification. `mailgun` and
`postmark` are written from those vendors' published field lists and have not
been fired at a live account of either, so their tests check that the bytes
match the documented shape and nothing more. A real end-to-end run is task 10,
and claiming these are verified against a live provider before that has happened
is the specific dishonesty the rest of this project is careful about.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple

from ..blast.payload import InboundEmail
from ..blast.serialize import (
    FormField,
    FormFile,
    attachment_info_json,
    charsets_json,
    envelope_json,
    to_multipart_parts,
)


@dataclass(frozen=True)
class EncodedBody:
    """Bytes to send, and the content type that describes them.

    Content type is produced by the format rather than assembled by the caller,
    because the boundary in a multipart content type is only correct if it came
    from the same encoder that wrote the body. Splitting those two is how a
    request ends up announcing one boundary and carrying another.
    """

    body: bytes
    content_type: str


class WireFormat:
    """The format interface. One method, by design.

    Subclassing rather than a Protocol because every implementation here shares
    the name registry, and a Protocol plus a separate registry is two places to
    forget to update.
    """

    #: The value a `[targets.<name>]` table uses to select this format.
    name: str = ""

    def encode(self, payload: InboundEmail) -> EncodedBody:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# sendgrid: the current behaviour, preserved exactly
# ---------------------------------------------------------------------------


class SendgridFormat(WireFormat):
    """SendGrid Inbound Parse, in parsed mode.

    The field order, names and JSON encodings come from
    `blast.serialize.to_multipart_parts`, which is the single place that knows
    the SendGrid shape. This class adds nothing but the boundary-aware content
    type, because guessing that here would mean two sources of truth for a
    wire format that operators debug by reading raw bytes.
    """

    name = "sendgrid"

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def encode(self, payload: InboundEmail) -> EncodedBody:
        from .transport import encode_multipart

        body = encode_multipart(to_multipart_parts(payload), self._boundary)
        return EncodedBody(
            body=body, content_type=f"multipart/form-data; boundary={self._boundary}"
        )


# ---------------------------------------------------------------------------
# mailgun: Mailgun inbound routing
# ---------------------------------------------------------------------------


class MailgunFormat(WireFormat):
    """Mailgun inbound routing: multipart/form-data with Mailgun's field names.

    Field list taken from Mailgun's "receive and forward over HTTP" docs:
    https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http.md

    - `recipient` is the envelope recipient, `sender` the envelope sender, and
      `from` the From header. They differ for a relayed or Bcc'd message.
    - The body parts are `body-plain` and `body-html`.
    - `message-headers` is the full header list as a JSON-encoded string of
      `[name, value]` pairs, in order. It is not raw header text.
    - Attachments are sent as `attachment-1` .. `attachment-N`, numbered from 1,
      with `attachment-count` alongside when there are any.
    - `stripped-text`, `stripped-html` and `stripped-signature` are produced by
      Mailgun's own parser, so they are not sent here.
    - `timestamp`, `token` and `signature` authenticate the request. They are
      only sent when `signature_fields` is given, because a value computed
      without the operator's signing key would be a fabricated signature.
      `sigcheck` supplies them.

    Not fired at a live Mailgun account; checked against the field list above.
    """

    name = "mailgun"

    def __init__(
        self, boundary: str, signature_fields: Optional[Dict[str, str]] = None
    ) -> None:
        self._boundary = boundary
        if signature_fields is not None:
            missing = {"timestamp", "token", "signature"} - set(signature_fields)
            if missing:
                raise ValueError(
                    f"mailgun signature_fields is missing {sorted(missing)}"
                )
        self._signature_fields = signature_fields

    def encode(self, payload: InboundEmail) -> EncodedBody:
        from .transport import encode_multipart

        header_pairs = [[name, value] for name, value in payload.headers.items()]
        parts: List[FormPart] = [
            FormField("recipient", payload.to),
            FormField("sender", payload.envelope.from_addr),
            FormField("from", payload.from_addr),
            FormField("subject", payload.subject),
            FormField("body-plain", payload.text),
            FormField("body-html", payload.html),
            FormField("message-headers", json.dumps(header_pairs, ensure_ascii=False)),
        ]
        if payload.attachments:
            parts.append(FormField("attachment-count", str(len(payload.attachments))))
        if self._signature_fields is not None:
            for key in ("timestamp", "token", "signature"):
                parts.append(FormField(key, self._signature_fields[key]))
        for index, attachment in enumerate(payload.attachments, start=1):
            parts.append(
                FormFile(
                    name=f"attachment-{index}",
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    content=attachment.content,
                )
            )

        return EncodedBody(
            body=encode_multipart(parts, self._boundary),
            content_type=f"multipart/form-data; boundary={self._boundary}",
        )


def _first_header(headers, name: str) -> Optional[str]:
    """The first value for a header name, case-insensitively.

    HTTP header names are case-insensitive, and a generator that wrote
    `Message-ID` against one that expects `Message-Id` should not silently lose
    the value. A missing header returns None, which the caller turns into an
    empty field.
    """
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


# ---------------------------------------------------------------------------
# postmark: Postmark inbound webhook, JSON
# ---------------------------------------------------------------------------


class PostmarkFormat(WireFormat):
    """Postmark's inbound webhook, which posts a single JSON object rather than
    multipart form fields.

    Written from Postmark's published inbound webhook payload. Two shapes differ
    from everything else here:

    - Field names are PascalCase (`From`, `TextBody`, `MessageID`), and `From`
      is the header rather than the envelope sender. Postmark's inbound webhook
      has no envelope field, so `sender` is not invented: `headers` carries the
      raw ones and `envelope` is left to the receiver to infer.
    - Attachments are inline as base64 `Content` alongside their name and
      content type, not as separate file parts. Base64 rather than a
      multipart file, because Postmark's payload is one JSON document and a
      nested binary would have no standard encoding.
    """

    name = "postmark"

    def __init__(self, boundary: str) -> None:
        """Accepts the boundary and ignores it, because the registry hands the
        same arguments to every format. A JSON document has no boundary, and the
        alternative is a special case in the lookup that a fourth format would
        have to know about. Kept as a named unused argument rather than `*_` so
        that the next reader can see it was a decision."""

    def encode(self, payload: InboundEmail) -> EncodedBody:
        return EncodedBody(
            body=json.dumps(
                self._document(payload), sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            content_type="application/json",
        )

    def _document(self, payload: InboundEmail) -> Dict[str, object]:
        document: Dict[str, object] = {
            "From": payload.from_addr,
            "To": payload.to,
            "Cc": "",
            "Bcc": "",
            "Subject": payload.subject,
            "MessageID": _first_header(payload.headers, "Message-Id") or "",
            "Headers": [
                {"Name": name, "Value": value}
                for name, value in payload.headers.items()
            ],
            "HtmlBody": payload.html,
            "TextBody": payload.text,
            "ReplyTo": "",
            "StrippedTextReply": "",
        }
        if payload.attachments:
            document["Attachments"] = [
                {
                    "Name": attachment.filename,
                    "ContentType": attachment.content_type,
                    "ContentLength": len(attachment.content),
                    "Content": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in payload.attachments
            ]
        else:
            # An empty list rather than an absent key. Postmark's schema has
            # the field, and a receiver doing `for a in body["Attachments"]`
            # gets a KeyError on a message with no files, which is a receiver
            # bug this format should not have to work around.
            document["Attachments"] = []
        return document


# ---------------------------------------------------------------------------
# mime: raw RFC 5322
# ---------------------------------------------------------------------------


class MimeFormat(WireFormat):
    """A whole RFC 5322 message as the body, rather than a form encoding of one.

    This is the format for a receiver that expects to be handed a message
    instead of a submission about one: an SMTP-to-HTTP bridge, a Lambda that
    takes a raw message, or anything that re-injects the mail later and needs it
    to still be a valid message.

    Built with the stdlib `email` package, because hand-rolling MIME headers and
    boundary quoting is exactly the kind of code that is wrong in a way no
    hermetic test will notice until a real receiver parses it.

    The multipart boundary is fixed, not random. `email` would otherwise pick
    one per message, which would make the same payload produce different bytes
    on every run and break the determinism every other format here guarantees.
    """

    name = "mime"

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def encode(self, payload: InboundEmail) -> EncodedBody:
        message = EmailMessage()
        message["To"] = payload.to
        message["From"] = payload.from_addr
        message["Subject"] = payload.subject
        message.set_content(payload.text or "", charset="utf-8")
        if payload.html:
            message.add_alternative(payload.html, subtype="html", charset="utf-8")
            message.set_boundary(self._boundary)
        for attachment in payload.attachments:
            message.add_attachment(
                attachment.content,
                maintype=attachment.content_type.split("/", 1)[0],
                subtype=attachment.content_type.split("/", 1)[-1],
                filename=attachment.filename,
            )
        for name, value in payload.headers.items():
            if name.casefold() in _NOT_COPIED_HEADERS:
                continue
            message[name] = value

        return EncodedBody(body=message.as_bytes(), content_type=message.get_content_type())


#: Headers the MIME format sets from the payload's own fields, plus the ones
#: `email` owns. Copying these from `payload.headers` as well would produce a
#: message with two of each, and the wrong one wins depending on the parser.
#: The address headers are excluded for the same reason: `to`/`from` live on
#: the payload as fields, and the envelope sender is deliberately not written
#: as a `Return-Path` here, because inventing one would put a fabricated address
#: in a header a real MTA route reads.
_NOT_COPIED_HEADERS = frozenset(
    {
        "content-type",
        "content-transfer-encoding",
        "mime-version",
        "content-length",
        "to",
        "from",
        "subject",
    }
)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: name -> constructor taking the multipart boundary. A constructor rather than
#: an instance because the boundary is a module constant in transport.py and
#: importing transport from formats at module scope would be a cycle; passing it
#: in at lookup time breaks that without a lazy import.
_CONSTRUCTORS = {
    SendgridFormat.name: SendgridFormat,
    MailgunFormat.name: MailgunFormat,
    PostmarkFormat.name: PostmarkFormat,
    MimeFormat.name: MimeFormat,
}


def format_names() -> List[str]:
    """Every selectable format name, sorted. Used in error messages, so a user
    who typos `mailgrun` is told what they could have written."""
    return sorted(_CONSTRUCTORS)


def get_format(name: str, boundary: str) -> WireFormat:
    """Look up a format by its config name.

    Refuses an unknown name rather than falling back to the default. A silent
    fallback would send a SendGrid-shaped body to a Mailgun endpoint and report
    success if the endpoint's status code was 2xx, which is the exact failure
    this module exists to remove.
    """
    try:
        constructor = _CONSTRUCTORS[name]
    except KeyError:
        raise ValueError(
            f"unknown wire format {name!r}; known formats are {format_names()}"
        ) from None
    return constructor(boundary)
