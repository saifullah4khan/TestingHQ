"""Wire formats: how an InboundEmail becomes request bytes.

A format is one method: take an `InboundEmail`, return bytes plus a content
type. It knows nothing about HTTP, targets, guardrails or the clock.

Every format is deterministic: the same InboundEmail produces the same bytes,
because artifacts record what was sent and a replay has to match. JSON is
encoded with sorted keys and multipart boundaries are fixed.

Formats: `sendgrid` (Inbound Parse, parsed mode, the default), `sendgrid-raw`
(Inbound Parse, raw mode), `mailgun`, `postmark`, `mime`. The field lists and
the synthetic values used for provider-computed fields are in docs/FORMATS.md.
`sendgrid` and `mime` are checked against SendGrid's documented field list and
RFC 5322; `mailgun` and `postmark` against those vendors' published field lists.
None has been verified against a live provider account.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Dict, List, Optional

from ..blast.payload import InboundEmail
from ..blast.serialize import (
    FormField,
    FormFile,
    FormPart,
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
# sendgrid: Inbound Parse, parsed and raw mode
# ---------------------------------------------------------------------------

#: Fixed synthetic values for the fields SendGrid computes on its own side
#: (authentication results, spam scoring, the connecting IP). They have
#: SendGrid's shape and assert nothing about a real domain or host:
#: 192.0.2.1 is TEST-NET-1 (RFC 5737).
SENDGRID_SYNTHETIC = {
    "dkim": "{@example.com : pass}",
    "SPF": "pass",
    "sender_ip": "192.0.2.1",
    "spam_score": "0.0",
    "spam_report": "",
}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _content_id(index: int, attachment) -> Optional[str]:
    """The Content-ID SendGrid reports for an attachment, or None.

    An attachment that carries its own `content_id` keeps it. Otherwise an
    image is treated as inline and given a deterministic Gmail-style id, and
    any other attachment has none, which is how SendGrid's documented example
    looks."""
    explicit = getattr(attachment, "content_id", None)
    if explicit:
        return str(explicit)
    if not attachment.content_type.lower().startswith("image/"):
        return None
    digest = hashlib.sha256(
        f"{index}:{attachment.filename}".encode("utf-8")
    ).hexdigest()
    return f"ii_{digest[:16]}"


def _sendgrid_charsets(payload: InboundEmail, keys=None) -> str:
    charsets = dict(payload.charsets)
    charsets.setdefault("text", "UTF-8")
    if keys is not None:
        charsets = {key: charsets.get(key, "UTF-8") for key in keys}
    return _json(charsets)


class SendgridFormat(WireFormat):
    """SendGrid Inbound Parse, parsed mode: multipart/form-data with one text
    field per part of the message and one file part per attachment.

    The base parts come from `blast.serialize.to_multipart_parts`; this adds
    the fields SendGrid documents that the serializer does not carry (`dkim`,
    `content-ids`, `cc`, `sender_ip`, `spam_report`, `spam_score`, `SPF`),
    adds `text` to `charsets`, and adds `name` and `content-id` to each
    `attachment-info` entry.
    """

    name = "sendgrid"

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def parts(self, payload: InboundEmail) -> List[FormPart]:
        base = {part.name: part for part in to_multipart_parts(payload)}
        content_ids: Dict[str, str] = {}
        info: Dict[str, Dict[str, str]] = {}
        for index, attachment in enumerate(payload.attachments, start=1):
            entry = {
                "filename": attachment.filename,
                "name": attachment.filename,
                "type": attachment.content_type,
            }
            cid = _content_id(index, attachment)
            if cid is not None:
                entry["content-id"] = cid
                content_ids[cid] = f"attachment{index}"
            info[f"attachment{index}"] = entry

        parts: List[FormPart] = [
            base["headers"],
            FormField("dkim", SENDGRID_SYNTHETIC["dkim"]),
            FormField("content-ids", _json(content_ids)),
            base["to"],
            FormField("cc", _first_header(payload.headers, "Cc") or ""),
            base["from"],
            base["subject"],
            base["text"],
            base["html"],
            FormField("sender_ip", SENDGRID_SYNTHETIC["sender_ip"]),
            FormField("spam_report", SENDGRID_SYNTHETIC["spam_report"]),
            FormField("spam_score", SENDGRID_SYNTHETIC["spam_score"]),
            base["envelope"],
            FormField("charsets", _sendgrid_charsets(payload)),
            FormField("SPF", SENDGRID_SYNTHETIC["SPF"]),
            base["attachments"],
        ]
        if payload.attachments:
            parts.append(FormField("attachment-info", _json(info)))
        parts.extend(
            part for part in to_multipart_parts(payload) if isinstance(part, FormFile)
        )
        parts.extend(FormField(name, value) for name, value in payload.extra_form_fields)
        return parts

    def encode(self, payload: InboundEmail) -> EncodedBody:
        from .transport import encode_multipart

        return EncodedBody(
            body=encode_multipart(self.parts(payload), self._boundary),
            content_type=f"multipart/form-data; boundary={self._boundary}",
        )


class SendgridRawFormat(WireFormat):
    """SendGrid Inbound Parse, raw mode ("POST the raw, full MIME message").

    The whole RFC 5322 message, as the `mime` format builds it, goes in the
    `email` field. Alongside it SendGrid sends the envelope, the address and
    subject fields, their charsets, and its own authentication and spam
    results. No `text`, `html`, `headers` or attachment parts: a raw-mode
    receiver parses the message itself.
    """

    name = "sendgrid-raw"

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def parts(self, payload: InboundEmail) -> List[FormPart]:
        # The message's own boundaries must not contain the form boundary, or
        # a receiver splitting the form would split the message too.
        inner = "mime-" + hashlib.sha256(self._boundary.encode("utf-8")).hexdigest()[:24]
        message = MimeFormat(inner).message_bytes(payload)
        return [
            FormField("dkim", SENDGRID_SYNTHETIC["dkim"]),
            FormField("email", message.decode("utf-8", errors="replace")),
            FormField("to", payload.to),
            FormField("from", payload.from_addr),
            FormField("sender_ip", SENDGRID_SYNTHETIC["sender_ip"]),
            FormField("spam_report", SENDGRID_SYNTHETIC["spam_report"]),
            FormField("envelope", envelope_json(payload)),
            FormField("subject", payload.subject),
            FormField("spam_score", SENDGRID_SYNTHETIC["spam_score"]),
            FormField(
                "charsets", _sendgrid_charsets(payload, ("to", "from", "subject"))
            ),
            FormField("SPF", SENDGRID_SYNTHETIC["SPF"]),
        ]

    def encode(self, payload: InboundEmail) -> EncodedBody:
        from .transport import encode_multipart

        return EncodedBody(
            body=encode_multipart(self.parts(payload), self._boundary),
            content_type=f"multipart/form-data; boundary={self._boundary}",
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
    """A whole RFC 5322 message as the body, posted as `message/rfc822`.

    For a receiver that takes a raw message: an SMTP-to-HTTP bridge, a Lambda,
    osTicket's `api/tickets.email`. Built with the stdlib `email` package and
    serialized with CRLF line endings. Every multipart boundary is derived from
    the configured one (`<boundary>`, `<boundary>-1`, ...), never random.
    """

    name = "mime"

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def message(self, payload: InboundEmail) -> EmailMessage:
        message = EmailMessage()
        message["To"] = payload.to
        message["From"] = payload.from_addr
        message["Subject"] = payload.subject
        message.set_content(payload.text or "", charset="utf-8")
        if payload.html:
            message.add_alternative(payload.html, subtype="html", charset="utf-8")
        for attachment in payload.attachments:
            maintype, _, subtype = attachment.content_type.partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        for name, value in payload.headers.items():
            if name.casefold() in _NOT_COPIED_HEADERS:
                continue
            message[name] = value

        multiparts = [part for part in message.walk() if part.is_multipart()]
        for depth, part in enumerate(multiparts):
            part.set_boundary(self._boundary if depth == 0 else f"{self._boundary}-{depth}")
        return message

    def message_bytes(self, payload: InboundEmail) -> bytes:
        return self.message(payload).as_bytes(policy=SMTP)

    def encode(self, payload: InboundEmail) -> EncodedBody:
        return EncodedBody(body=self.message_bytes(payload), content_type="message/rfc822")


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
    SendgridRawFormat.name: SendgridRawFormat,
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
