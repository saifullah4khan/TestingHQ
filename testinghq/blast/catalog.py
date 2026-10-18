"""Named edge-case catalog for Blast.

A fixed set of individually named payload shapes worth testing on their own
merits, independent of the random messiness mix in blast/corrupt.py. Generic
only: every case is a shape that could reach any inbound-parse pipeline.

`build(name, seed)` seeds `random.Random("catalog:<name>:<seed>")` and hands it
to the case's builder, so a (name, seed) pair always produces the same
InboundEmail and different cases do not share a base message. Every case
starts from a clean email built the way blast.generate builds one and
describes only how it differs.

CATALOG_CATEGORIES assigns each case the category its verdict is graded by:
a `clean` case must 2xx, the others may be refused with a 4xx but must never
5xx or go unanswered.
"""
from __future__ import annotations

import random
from typing import Callable, Dict, List, Tuple

from .attachments import (
    attach,
    degenerate_attachment,
    rtl_override_attachment,
    zero_byte_attachment,
)
from .corrupt import (
    GMAIL_QUOTE_STYLE,
    _escape,
    _quote_moment,
    _quoted,
    gmail_attribution,
    outlook_block,
    sabotage_field,
)
from .generate import (
    BODY_SENTENCES,
    FIRST_NAMES,
    LAST_NAMES,
    RESERVED_DOMAINS,
    _message_id,
    _person,
    generate_email,
)
from .payload import Envelope, GroundTruth, InboundEmail
from .serialize import envelope_json


def _address(rng: random.Random) -> str:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return f"{first}.{last}{rng.randint(0, 999)}@{rng.choice(RESERVED_DOMAINS)}"


def _base(rng: random.Random) -> InboundEmail:
    """A clean starting point, built the way the clean generator builds one."""
    return generate_email(rng, index=0)


def _with_ground_truth(email: InboundEmail, **overrides) -> GroundTruth:
    fields = {
        "from_addr": email.ground_truth.from_addr,
        "subject": email.ground_truth.subject,
        "body_core": email.ground_truth.body_core,
    }
    fields.update(overrides)
    return GroundTruth(**fields)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def empty_subject(rng: random.Random) -> InboundEmail:
    """Subject header is present but empty."""
    email = _base(rng)
    return email.with_changes(subject="", ground_truth=_with_ground_truth(email, subject=""))


def whitespace_only_body(rng: random.Random) -> InboundEmail:
    """Body text and html are present but contain only whitespace."""
    email = _base(rng)
    return email.with_changes(text="   \n\t  \n", html="<html>\n \n</html>")


def html_only_no_text(rng: random.Random) -> InboundEmail:
    """An HTML part with no plain-text alternative."""
    return _base(rng).with_changes(text="")


def text_only_no_html(rng: random.Random) -> InboundEmail:
    """A plain-text-only message with no HTML part."""
    return _base(rng).with_changes(html="")


EXTREMELY_LONG_SUBJECT_LENGTH = 10_000


def extremely_long_subject(rng: random.Random) -> InboundEmail:
    """A subject of exactly 10,000 characters."""
    email = _base(rng)
    repeated = (email.subject + " ") * (EXTREMELY_LONG_SUBJECT_LENGTH // (len(email.subject) + 1) + 1)
    subject = repeated[:EXTREMELY_LONG_SUBJECT_LENGTH]
    return email.with_changes(
        subject=subject, ground_truth=_with_ground_truth(email, subject=subject)
    )


def unicode_heavy_subject(rng: random.Random) -> InboundEmail:
    """A subject built almost entirely from non-Latin scripts and emoji."""
    email = _base(rng)
    subject = "订单确认 🎉 подтверждение заказа مؤكد"
    return email.with_changes(
        subject=subject, ground_truth=_with_ground_truth(email, subject=subject)
    )


def rtl_body_text(rng: random.Random) -> InboundEmail:
    """A body written in a right-to-left script (Arabic)."""
    email = _base(rng)
    body = "مرحبا، هذه رسالة تجريبية باللغة العربية لاختبار النصوص من اليمين إلى اليسار."
    return email.with_changes(
        text=body,
        html=f'<html><body><div dir="rtl">{body}</div></body></html>',
        ground_truth=_with_ground_truth(email, body_core=body),
    )


MANY_RECIPIENTS = 500


def many_recipients(rng: random.Random) -> InboundEmail:
    """500 addresses in the To header, delivered to one envelope recipient,
    which is what SendGrid posts for a message sent to a large list."""
    email = _base(rng)
    delivered = email.envelope.to[0]
    others = [_address(rng) for _ in range(MANY_RECIPIENTS - 1)]
    to_header = ", ".join([delivered, *others])
    return email.with_changes(
        to=to_header,
        envelope=Envelope(to=(delivered,), from_addr=email.envelope.from_addr),
    )


def single_char_local_part(rng: random.Random) -> InboundEmail:
    """A sender address with a one-character local part."""
    email = _base(rng)
    addr = f"a@{rng.choice(RESERVED_DOMAINS)}"
    return email.with_changes(
        from_addr=f"A <{addr}>",
        envelope=Envelope(to=email.envelope.to, from_addr=addr),
        ground_truth=_with_ground_truth(email, from_addr=addr),
    )


def plus_addressing(rng: random.Random) -> InboundEmail:
    """A recipient address using the local-part `+tag` convention."""
    email = _base(rng)
    addr = f"alice+billing.tag@{rng.choice(RESERVED_DOMAINS)}"
    return email.with_changes(
        to=f"Alice <{addr}>",
        envelope=Envelope(to=(addr,), from_addr=email.envelope.from_addr),
    )


def long_local_part(rng: random.Random) -> InboundEmail:
    """A sender address with a 180-character local part."""
    email = _base(rng)
    addr = f"{'x' * 180}@{rng.choice(RESERVED_DOMAINS)}"
    return email.with_changes(
        from_addr=addr,
        envelope=Envelope(to=email.envelope.to, from_addr=addr),
        ground_truth=_with_ground_truth(email, from_addr=addr),
    )


def reply_to_mismatch(rng: random.Random) -> InboundEmail:
    """A Reply-To pointing somewhere other than From."""
    email = _base(rng)
    headers = dict(email.headers)
    headers["Reply-To"] = _address(rng)
    return email.with_changes(headers=headers)


def header_injection_attempt(rng: random.Random) -> InboundEmail:
    """A subject that embeds a CRLF and a fake extra header."""
    email = _base(rng)
    return email.with_changes(subject=f"{email.subject}\r\nX-Injected: evil")


def mixed_line_endings(rng: random.Random) -> InboundEmail:
    """Body text mixing \\n, \\r\\n, and bare \\r line endings."""
    email = _base(rng)
    lines = email.text.splitlines() or [""]
    endings = ["\n", "\r\n", "\r"]
    mixed = "".join(line + endings[i % len(endings)] for i, line in enumerate(lines))
    return email.with_changes(text=mixed)


def zero_length_envelope(rng: random.Random) -> InboundEmail:
    """An envelope with no recipients and an empty To."""
    email = _base(rng)
    return email.with_changes(
        to="", envelope=Envelope(to=(), from_addr=email.envelope.from_addr)
    )


def zero_byte_attachment_case(rng: random.Random) -> InboundEmail:
    """Exactly one attachment, zero bytes long."""
    email = _base(rng)
    return attach(email, (zero_byte_attachment(rng),))


def degenerate_attachment_case(rng: random.Random) -> InboundEmail:
    """One attachment with an awkward filename, an empty or nonsense content
    type, or arbitrary content."""
    email = _base(rng)
    return attach(email, (degenerate_attachment(rng),))


def attachment_only_minimal_body(rng: random.Random) -> InboundEmail:
    """No body at all; the substance is the attachment."""
    email = _base(rng).with_changes(text="", html="")
    return attach(email, (zero_byte_attachment(rng),))


def declared_charset_mismatch(rng: random.Random) -> InboundEmail:
    """The subject sent as latin-1, cp1252 or Shift_JIS bytes under a charsets
    entry that says UTF-8. See blast/corrupt.py:encoding_sabotage."""
    return sabotage_field(_base(rng), rng, "subject")


def heavily_quoted_reply_chain(rng: random.Random) -> InboundEmail:
    """A long thread: the reply, four nested Gmail quotes, and inside the
    deepest one an Outlook reply carrying the "-----Original Message-----"
    block of the message the thread started from. The structure is fixed; only names, dates and
    sentences come from the seed."""
    email = _base(rng)
    topic_subject = email.subject[4:] if email.subject.startswith("Re: ") else email.subject
    people = [_person(rng) for _ in range(5)]
    bodies = [" ".join(rng.sample(BODY_SENTENCES, k=2)) for _ in range(5)]

    def gmail_quote_html(attribution: str, inner: str) -> str:
        return (
            f'<div class="gmail_quote"><div class="gmail_attr">{_escape(attribution)}<br></div>'
            f'<blockquote class="gmail_quote" style="{GMAIL_QUOTE_STYLE}">{inner}'
            "</blockquote></div>"
        )

    # Message 3 was written in Outlook as a reply to message 4, the oldest.
    outlook = outlook_block(
        "-----Original Message-----", people[4][0], people[4][1], _quote_moment(rng),
        email.to, topic_subject,
    )
    text_chain = f"{bodies[3]}\n\n{outlook}\n{bodies[4]}"
    html_chain = (
        f"<div>{_escape(bodies[3])}</div><div>"
        f"{_escape(outlook + chr(10) + bodies[4]).replace(chr(10), '<br>')}</div>"
    )
    # Messages 2, 1 and 0 each quote the one before in Gmail, and the reply
    # quotes message 0: four nested Gmail quotes in all.
    for level in (2, 1, 0):
        name, address = people[level + 1]
        attribution = gmail_attribution(_quote_moment(rng), name, address)
        text_chain = f"{bodies[level]}\n\n{attribution}\n{_quoted(text_chain)}"
        html_chain = f"<div>{_escape(bodies[level])}</div>" + gmail_quote_html(
            attribution, html_chain
        )
    head = gmail_attribution(_quote_moment(rng), people[0][0], people[0][1])
    reply = email.ground_truth.body_core
    text = f"{reply}\n\n{head}\n{_quoted(text_chain)}\n"
    html = (
        f'<html><body><div dir="ltr"><div>{_escape(reply)}</div>'
        f"{gmail_quote_html(head, html_chain)}</div></body></html>"
    )

    domain = email.envelope.from_addr.split("@", 1)[1]
    ancestry = [_message_id(rng, domain) for _ in range(5)]
    headers = dict(email.headers)
    headers["In-Reply-To"] = ancestry[-1]
    headers["References"] = " ".join(ancestry)
    subject = f"Re: {topic_subject}"
    return email.with_changes(headers=headers).with_changes(
        subject=subject,
        text=text,
        html=html,
        ground_truth=_with_ground_truth(email, subject=subject),
    )


def null_byte_in_body(rng: random.Random) -> InboundEmail:
    """A NUL character in the middle of the body text and html."""
    email = _base(rng)
    middle = len(email.text) // 2
    text = email.text[:middle] + "\x00" + email.text[middle:]
    html = email.html.replace("</body>", "\x00</body>", 1)
    return email.with_changes(text=text, html=html)


def rtl_override_in_filename(rng: random.Random) -> InboundEmail:
    """An attachment whose name uses U+202E so `invoice<RLO>fdp.exe` displays
    as `invoiceexe.pdf`, with PE bytes under a PDF content type."""
    return attach(_base(rng), (rtl_override_attachment(rng),))


def missing_boundary_field(rng: random.Random) -> InboundEmail:
    """A form part whose Content-Disposition has no name parameter."""
    return _base(rng).with_changes(
        extra_form_fields=(("", b"a form part with no name"),)
    )


def duplicate_form_field(rng: random.Random) -> InboundEmail:
    """A second `subject` form field after the first, with a different value.
    The ground truth is the first one."""
    email = _base(rng)
    return email.with_changes(
        extra_form_fields=(("subject", f"Second subject: {email.subject}".encode("utf-8")),)
    )


def malformed_envelope_json(rng: random.Random) -> InboundEmail:
    """An `envelope` field that is truncated JSON."""
    email = _base(rng)
    truncated = envelope_json(email)[:-1].encode("utf-8")
    return email.with_changes(raw_fields={**email.raw_fields, "envelope": truncated})


OVERSIZED_BODY_BYTES = 1024 * 1024


def oversized_body_1mb(rng: random.Random) -> InboundEmail:
    """A plain-text body of exactly 1 MiB: the message, then quoted history."""
    email = _base(rng)
    filler_line = "> " + " ".join(BODY_SENTENCES) + "\n"
    needed = OVERSIZED_BODY_BYTES - len(email.text.encode("utf-8")) - 1
    filler = (filler_line * (needed // len(filler_line) + 1))[:needed]
    return email.with_changes(text=email.text + "\n" + filler)


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
    "null_byte_in_body": null_byte_in_body,
    "rtl_override_in_filename": rtl_override_in_filename,
    "missing_boundary_field": missing_boundary_field,
    "duplicate_form_field": duplicate_form_field,
    "malformed_envelope_json": malformed_envelope_json,
    "oversized_body_1mb": oversized_body_1mb,
}

#: The verdict category each case is graded as (core/report.py labels).
CATALOG_CATEGORIES: Dict[str, str] = {
    "empty_subject": "messy-but-valid",
    "whitespace_only_body": "degenerate",
    "html_only_no_text": "clean",
    "text_only_no_html": "clean",
    "extremely_long_subject": "messy-but-valid",
    "unicode_heavy_subject": "clean",
    "rtl_body_text": "clean",
    "many_recipients": "messy-but-valid",
    "single_char_local_part": "clean",
    "plus_addressing": "clean",
    "long_local_part": "messy-but-valid",
    "reply_to_mismatch": "clean",
    "header_injection_attempt": "structurally-malformed",
    "mixed_line_endings": "messy-but-valid",
    "zero_length_envelope": "degenerate",
    "zero_byte_attachment": "messy-but-valid",
    "degenerate_attachment": "structurally-malformed",
    "attachment_only_minimal_body": "messy-but-valid",
    "declared_charset_mismatch": "structurally-malformed",
    "heavily_quoted_reply_chain": "clean",
    "null_byte_in_body": "structurally-malformed",
    "rtl_override_in_filename": "structurally-malformed",
    "missing_boundary_field": "structurally-malformed",
    "duplicate_form_field": "structurally-malformed",
    "malformed_envelope_json": "structurally-malformed",
    "oversized_body_1mb": "messy-but-valid",
}


def build(name: str, seed: int) -> InboundEmail:
    """Build the named case from `seed`. Raises KeyError naming the choices
    for an unknown name."""
    try:
        case = CATALOG[name]
    except KeyError:
        raise KeyError(
            f"unknown catalog case: {name!r}; choose from {sorted(CATALOG)}"
        ) from None
    return case(random.Random(f"catalog:{name}:{seed}"))


def build_all(seed: int) -> List[Tuple[str, InboundEmail]]:
    """Every case from the same seed, in name-sorted order."""
    return [(name, build(name, seed)) for name in sorted(CATALOG)]

