"""The InboundEmail model: Blast's in-memory representation of one generated
inbound-email payload, independent of any wire format.

Every field here is plain data. Nothing in this module knows about SendGrid,
HTTP, or the filesystem; testinghq/blast/serialize.py turns an InboundEmail
into SendGrid Inbound Parse wire fields, and testinghq/core/transport.py (a
separate backlog item) turns those into an actual POST.

Determinism is load-bearing for Blast: the same seed plus the same config must
yield byte-identical output. That means every InboundEmail the generator
builds must be fully determined by its constructor arguments, with no
wall-clock reads, no unseeded randomness, and no reliance on dict/set
iteration order beyond what Python guarantees (dict insertion order, which is
deterministic given deterministic insertion). This module enforces none of
that itself; it is on the generator (a separate backlog item) to build these
objects deterministically from a seed.

This model is deliberately permissive about *content*: it does not reject
empty subjects, empty recipient lists, or empty attachment filenames. Blast's
whole job is to generate the clean-to-garbled spectrum, including degenerate
and malformed payloads (M2), so a valid InboundEmail is allowed to describe an
invalid email. The only invariants enforced here are structural (Python
types), never business rules about what a "real" email looks like.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple


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
    """What this payload was actually built to say, carried alongside the
    payload so a later run can assert what the intake endpoint under test
    parsed back out against what Blast put in (M3: category-versus-outcome
    reporting reads this).
    """

    from_addr: str
    subject: str
    body_core: str


@dataclass(frozen=True)
class InboundEmail:
    """One generated inbound-email payload plus the ground truth needed to
    grade whatever the endpoint under test does with it.

    Fields mirror what a SendGrid Inbound Parse webhook delivers in parsed
    mode: headers, to, from, subject, text, html, envelope, charsets, and
    attachments. `charsets` maps a subset of {"to", "from", "subject", "html"}
    to the charset that field was declared (or actually encoded) in; the
    clean generator sets these to match reality, and later chaos generators
    are expected to deliberately mismatch them to probe encoding bugs.
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
