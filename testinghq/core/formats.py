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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from ..blast.payload import InboundEmail
from ..blast.serialize import (
    FormField,
    FormFile,
    attachment_info_json,
    charsets_json,
    envelope_json,
    headers_text,
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
# The registry
# ---------------------------------------------------------------------------

#: name -> constructor taking the multipart boundary. A constructor rather than
#: an instance because the boundary is a module constant in transport.py and
#: importing transport from formats at module scope would be a cycle; passing it
#: in at lookup time breaks that without a lazy import.
_CONSTRUCTORS = {
    SendgridFormat.name: SendgridFormat,
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
