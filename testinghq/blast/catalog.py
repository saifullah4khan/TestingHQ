"""Named edge-case catalog for Blast.

A fixed set of specific, individually-named payload shapes that are worth
testing on their own merits, independent of the random messiness-level
pipeline in blast/corrupt.py. Generic only: every case here is a shape of
email that could show up on any inbound-parse pipeline (empty subject, a
reply-to mismatch, plus-addressing, a header-injection attempt, ...), never
something specific to any one business or product.

`build(name, seed)` seeds a single random.Random(seed) and hands it to the
named case's builder, so a given (name, seed) pair always produces a
byte-identical InboundEmail. Every case starts from a normal clean email
(built the same way blast.generate does) and describes only how it differs,
so each case stays small and its intent stays readable.
"""
from __future__ import annotations

import dataclasses
import random
from typing import Callable, Dict, List, Tuple

from .attachments import degenerate_attachment, zero_byte_attachment
from .corrupt import encoding_sabotage, structural_noise
from .generate import FIRST_NAMES, LAST_NAMES, RESERVED_DOMAINS, generate_email
from .payload import Envelope, GroundTruth, InboundEmail


def _address(rng: random.Random) -> str:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return f"{first}.{last}{rng.randint(0, 999)}@{rng.choice(RESERVED_DOMAINS)}"


def _base(rng: random.Random) -> InboundEmail:
    """A normal, well-formed starting point, built the same way the clean
    generator builds one, so every catalog case only has to describe its
    one difference from a well-formed email."""
    return generate_email(rng, index=0)


def _with_ground_truth(email: InboundEmail, **overrides) -> GroundTruth:
    fields = dataclasses.asdict(email.ground_truth)
    fields.update(overrides)
    return GroundTruth(**fields)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def empty_subject(rng: random.Random) -> InboundEmail:
    """Subject header is present but empty."""
    email = _base(rng)
    return dataclasses.replace(
        email, subject="", ground_truth=_with_ground_truth(email, subject="")
    )


def whitespace_only_body(rng: random.Random) -> InboundEmail:
    """Body text and html are present but contain only whitespace."""
    email = _base(rng)
    return dataclasses.replace(email, text="   \n\t  \n", html="<html>\n \n</html>")


def html_only_no_text(rng: random.Random) -> InboundEmail:
    """A common real-world shape: an HTML part with no plain-text
    alternative at all."""
    email = _base(rng)
    return dataclasses.replace(email, text="")


def text_only_no_html(rng: random.Random) -> InboundEmail:
    """The inverse: a plain-text-only message with no HTML part."""
    email = _base(rng)
    return dataclasses.replace(email, html="")


def extremely_long_subject(rng: random.Random) -> InboundEmail:
    """A subject line far past what most UIs or database columns expect."""
    email = _base(rng)
    long_subject = (email.subject + " ") * 80  # a few thousand characters
    return dataclasses.replace(
        email,
        subject=long_subject,
        ground_truth=_with_ground_truth(email, subject=long_subject),
    )


def unicode_heavy_subject(rng: random.Random) -> InboundEmail:
    """A subject built almost entirely from non-Latin scripts and emoji."""
    email = _base(rng)
    subject = "订单确认 🎉 подтверждение заказа مؤكد"
    return dataclasses.replace(
        email, subject=subject, ground_truth=_with_ground_truth(email, subject=subject)
    )


def rtl_body_text(rng: random.Random) -> InboundEmail:
    """A body written primarily in a right-to-left script (Arabic)."""
    email = _base(rng)
    body = "مرحبا، هذه رسالة تجريبية باللغة العربية لاختبار النصوص من اليمين إلى اليسار."
    return dataclasses.replace(
        email,
        text=body,
        html=f"<p dir=\"rtl\">{body}</p>",
        ground_truth=_with_ground_truth(email, body_core=body),
    )


def many_recipients(rng: random.Random) -> InboundEmail:
    """A message envelope-addressed to a large number of recipients."""
    email = _base(rng)
    recipients = tuple(_address(rng) for _ in range(25))
    to_header = ", ".join(recipients)
    return dataclasses.replace(
        email,
        to=to_header,
        envelope=Envelope(to=recipients, from_addr=email.envelope.from_addr),
    )


def single_char_local_part(rng: random.Random) -> InboundEmail:
    """A sender address with a one-character local part."""
    email = _base(rng)
    domain = rng.choice(RESERVED_DOMAINS)
    addr = f"a@{domain}"
    return dataclasses.replace(
        email,
        from_addr=f"A <{addr}>",
        envelope=Envelope(to=email.envelope.to, from_addr=addr),
        ground_truth=_with_ground_truth(email, from_addr=addr),
    )


def plus_addressing(rng: random.Random) -> InboundEmail:
    """A recipient address using the local-part `+tag` convention."""
    email = _base(rng)
    domain = rng.choice(RESERVED_DOMAINS)
    addr = f"alice+billing.tag@{domain}"
    return dataclasses.replace(
        email,
        to=f"Alice <{addr}>",
        envelope=Envelope(to=(addr,), from_addr=email.envelope.from_addr),
    )


def long_local_part(rng: random.Random) -> InboundEmail:
    """A sender address with an unusually long local part, still under a
    reserved domain."""
    email = _base(rng)
    domain = rng.choice(RESERVED_DOMAINS)
    local = "x" * 180
    addr = f"{local}@{domain}"
    return dataclasses.replace(
        email,
        from_addr=f"{addr}",
        envelope=Envelope(to=email.envelope.to, from_addr=addr),
        ground_truth=_with_ground_truth(email, from_addr=addr),
    )


def reply_to_mismatch(rng: random.Random) -> InboundEmail:
    """A Reply-To header pointing at a different reserved address than
    From, a classic phishing-adjacent shape worth parsing correctly."""
    email = _base(rng)
    reply_to = _address(rng)
    headers = dict(email.headers)
    headers["Reply-To"] = reply_to
    return dataclasses.replace(email, headers=headers)


def header_injection_attempt(rng: random.Random) -> InboundEmail:
    """A subject header value that embeds a CRLF and a fake extra header,
    simulating a (synthetic, harmless) header-injection attempt so the
    endpoint under test can be checked for safe header parsing."""
    email = _base(rng)
    injected_subject = f"{email.subject}\r\nX-Injected: evil"
    headers = dict(email.headers)
    headers["Subject"] = injected_subject
    return dataclasses.replace(email, subject=injected_subject, headers=headers)


def mixed_line_endings(rng: random.Random) -> InboundEmail:
    """Body text mixing \\n, \\r\\n, and bare \\r line endings."""
    email = _base(rng)
    lines = email.text.splitlines() or [""]
    endings = ["\n", "\r\n", "\r"]
    mixed = "".join(
        line + endings[i % len(endings)] for i, line in enumerate(lines)
    )
    return dataclasses.replace(email, text=mixed)


def zero_length_envelope(rng: random.Random) -> InboundEmail:
    """An envelope with no recipients at all: the InboundEmail model
    explicitly allows this (see payload.py's permissiveness note); this
    case exercises it directly rather than leaving it purely theoretical."""
    email = _base(rng)
    return dataclasses.replace(email, to="", envelope=Envelope(to=(), from_addr=email.envelope.from_addr))


def zero_byte_attachment_case(rng: random.Random) -> InboundEmail:
    """A message with exactly one attachment, which is zero bytes long."""
    email = _base(rng)
    return dataclasses.replace(email, attachments=(zero_byte_attachment(rng),))


def degenerate_attachment_case(rng: random.Random) -> InboundEmail:
    """A message whose single attachment has an awkward filename, an empty
    or nonsense content type, or a small amount of arbitrary content."""
    email = _base(rng)
    return dataclasses.replace(email, attachments=(degenerate_attachment(rng),))


def attachment_only_minimal_body(rng: random.Random) -> InboundEmail:
    """Almost no body at all; the payload's substance is the attachment."""
    email = _base(rng)
    return dataclasses.replace(
        email, text="", html="", attachments=(zero_byte_attachment(rng),)
    )


def declared_charset_mismatch(rng: random.Random) -> InboundEmail:
    """A well-known instance of the encoding-sabotage mutator applied to
    the subject: the charsets field declares one charset while the subject
    content was produced as if encoded in another. See
    blast/corrupt.py:encoding_sabotage for the mechanics."""
    email = _base(rng)
    return encoding_sabotage(email, rng)


def heavily_quoted_reply_chain(rng: random.Random) -> InboundEmail:
    """Several layers of quoted reply chain stacked up, as a long-running
    email thread would produce."""
    email = _base(rng)
    for _ in range(4):
        email = structural_noise(email, rng)
    return email


CATALOG: Dict[str, Callable[[random.Random], InboundEmail]] = {
    "empty_subject": empty_subject,
    "whitespace_only_body": whitespace_only_body,
    "html_only_no_text": html_only_no_text,
    "text_only_no_html": text_only_no_html,
    "extremely_long_subject": extremely_long_subject,
    "unicode_heavy_subject": unicode_heavy_subject,
    "rtl_body_text": rtl_body_text,
    "many_recipients": many_recipients,
    "single_char_local_part": single_char_local_part,
    "plus_addressing": plus_addressing,
    "long_local_part": long_local_part,
    "reply_to_mismatch": reply_to_mismatch,
    "header_injection_attempt": header_injection_attempt,
    "mixed_line_endings": mixed_line_endings,
    "zero_length_envelope": zero_length_envelope,
    "zero_byte_attachment": zero_byte_attachment_case,
    "degenerate_attachment": degenerate_attachment_case,
    "attachment_only_minimal_body": attachment_only_minimal_body,
    "declared_charset_mismatch": declared_charset_mismatch,
    "heavily_quoted_reply_chain": heavily_quoted_reply_chain,
}


def build(name: str, seed: int) -> InboundEmail:
    """Build the named catalog case from `seed`. Raises KeyError with the
    available names if `name` is not in CATALOG."""
    try:
        case = CATALOG[name]
    except KeyError:
        raise KeyError(
            f"unknown catalog case: {name!r}; choose from {sorted(CATALOG)}"
        ) from None
    rng = random.Random(seed)
    return case(rng)


def build_all(seed: int) -> List[Tuple[str, InboundEmail]]:
    """Build every catalog case from the same seed, in name-sorted order,
    for callers that want the full catalog as one deterministic batch."""
    return [(name, build(name, seed)) for name in sorted(CATALOG)]
