"""The InboundEmail model: one generated inbound-email payload, independent of
any wire format.

Every field here is plain data. Nothing in this module knows about HTTP or the
filesystem; testinghq/blast/serialize.py turns an InboundEmail into SendGrid
Inbound Parse form fields, testinghq/core/formats.py into the other wire
formats, and testinghq/core/transport.py posts the result.

Determinism: an InboundEmail is fully determined by its constructor arguments.
The generators build them from a seeded random.Random, never from the clock or
global random state.

The model is permissive about content. Empty subjects, empty recipient lists
and empty attachment filenames are allowed, because Blast generates degenerate
and malformed payloads on purpose. Only Python types are enforced.

Header consistency. `subject`, `to` and `from_addr` are the decoded values of
the `Subject`, `To` and `From` headers, the way SendGrid derives both from the
same message. Use `with_changes` rather than `dataclasses.replace` to change
any of them, so `headers` follows. Non-ASCII values are RFC 2047 encoded in
`headers` and stay decoded in the fields.
"""
from __future__ import annotations

import base64
import dataclasses
import re
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Envelope:
    """The SMTP envelope for one message: who it was actually delivered to
    and who it actually came from, which can differ from the To/From headers.
    """

    to: Tuple[str, ...]
    from_addr: str


@dataclass(frozen=True)
class Attachment:
    """One file attached to a generated email."""

    filename: str
    content_type: str
    content: bytes

    def __post_init__(self):
        if not isinstance(self.content, (bytes, bytearray)):
            raise TypeError(
                f"Attachment.content must be bytes, got {type(self.content).__name__}"
            )


@dataclass(frozen=True)
class GroundTruth:
    """What this payload was built to say, kept alongside it so a run can
    compare what the endpoint parsed against what Blast put in."""

    from_addr: str
    subject: str
    body_core: str


#: InboundEmail attribute name -> SendGrid form field name, for the fields
#: whose names differ. Used to keep `raw_fields` in step with `with_changes`.
_WIRE_NAMES = {"from_addr": "from"}

#: Field -> header kept in sync with it.
_SYNCED_HEADERS = (("subject", "Subject"), ("to", "To"), ("from_addr", "From"))


@dataclass(frozen=True)
class InboundEmail:
    """One generated inbound-email payload plus the ground truth needed to
    grade what the endpoint under test does with it.

    Fields mirror a SendGrid Inbound Parse post in parsed mode. `charsets`
    maps form field names ("to", "from", "subject", "text", "html") to the
    charset declared for that field.

    `raw_fields` maps a form field name to the exact bytes to send for it in
    place of the UTF-8 encoding of the str field. Encoding sabotage uses it to
    put real cp1252, latin-1 or Shift_JIS bytes on the wire, and catalog cases
    use it for fields such as a malformed `envelope`. The str field then holds
    what a receiver that trusted `charsets` would decode.

    `extra_form_fields` are `(name, bytes)` parts appended after the standard
    ones. An empty name produces a part with no name parameter. Formats other
    than SendGrid's ignore both of these.
    """

    to: str
    from_addr: str
    subject: str
    text: str
    html: str
    envelope: Envelope
    ground_truth: GroundTruth
    headers: Dict[str, str] = field(default_factory=dict)
    charsets: Dict[str, str] = field(default_factory=dict)
    attachments: Tuple[Attachment, ...] = field(default_factory=tuple)
    raw_fields: Mapping[str, bytes] = field(default_factory=dict)
    extra_form_fields: Tuple[Tuple[str, bytes], ...] = field(default_factory=tuple)

    def with_changes(self, **changes) -> "InboundEmail":
        """`dataclasses.replace`, keeping derived data consistent.

        A changed `subject`, `to` or `from_addr` rewrites the matching header
        (RFC 2047 encoded when not ASCII) unless `headers` is passed in the
        same call. A changed field drops its stale `raw_fields` entry unless
        `raw_fields` is passed in the same call.
        """
        if "headers" not in changes:
            headers = dict(self.headers)
            touched = False
            for attr, header in _SYNCED_HEADERS:
                if attr in changes:
                    value = changes[attr]
                    encoded = (
                        encode_header_value(value)
                        if attr == "subject"
                        else encode_address_header(value)
                    )
                    set_header(headers, header, encoded)
                    touched = True
            if touched:
                changes["headers"] = headers
        if "raw_fields" not in changes and self.raw_fields:
            stale = {_WIRE_NAMES.get(attr, attr) for attr in changes}
            kept = {k: v for k, v in self.raw_fields.items() if k not in stale}
            if len(kept) != len(self.raw_fields):
                changes["raw_fields"] = kept
        return dataclasses.replace(self, **changes)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

#: Longest raw byte run per encoded word. 45 bytes is 60 base64 characters,
#: which with the `=?UTF-8?B?` and `?=` wrapper stays under the RFC 2047
#: limit of 75 characters per encoded word.
_MAX_WORD_BYTES = 45


def _is_ascii(value: str) -> bool:
    return all(ord(ch) < 128 for ch in value)


def encoded_word(raw: bytes, charset: str) -> str:
    """One or more RFC 2047 B-encoded words carrying `raw` labelled `charset`.

    Splits on byte boundaries without regard to the charset, which is what a
    broken sender does, so it is only used for deliberately mislabelled bytes.
    """
    words = []
    for start in range(0, len(raw), _MAX_WORD_BYTES):
        chunk = raw[start:start + _MAX_WORD_BYTES]
        words.append(f"=?{charset}?B?{base64.b64encode(chunk).decode('ascii')}?=")
    return " ".join(words) if words else f"=?{charset}?B??="


def encode_header_value(value: str) -> str:
    """RFC 2047 encode `value` as UTF-8 B words if it is not pure ASCII.

    ASCII values come back unchanged, including any CR or LF in them, because
    a header-injection case needs those bytes left alone. Words never split a
    character across two words.
    """
    if _is_ascii(value):
        return value
    words = []
    chunk = b""
    for ch in value:
        encoded = ch.encode("utf-8", errors="surrogatepass")
        if len(chunk) + len(encoded) > _MAX_WORD_BYTES:
            words.append(chunk)
            chunk = b""
        chunk += encoded
    if chunk:
        words.append(chunk)
    return " ".join(
        f"=?UTF-8?B?{base64.b64encode(w).decode('ascii')}?=" for w in words
    )


_NAME_ADDR = re.compile(r"^(?P<name>.*?)\s*<(?P<addr>[^<>]*)>$")


def encode_address_header(value: str) -> str:
    """Encode the display names of an address header, leaving addresses as
    they are. A non-ASCII item that is not `Name <addr>` is left raw."""
    if _is_ascii(value):
        return value
    items = []
    for item in value.split(", "):
        match = _NAME_ADDR.match(item)
        if match and not _is_ascii(match.group("name")):
            items.append(
                f"{encode_header_value(match.group('name'))} <{match.group('addr')}>"
            )
        else:
            items.append(item)
    return ", ".join(items)


def find_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """The header key matching `name` case-insensitively, or None."""
    wanted = name.casefold()
    for key in headers:
        if key.casefold() == wanted:
            return key
    return None


def set_header(headers: Dict[str, str], name: str, value: str) -> None:
    """Set `name` in place, keeping the existing key's spelling and position."""
    key = find_header(headers, name)
    headers[key if key is not None else name] = value
