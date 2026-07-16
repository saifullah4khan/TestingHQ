"""Clean corpus generator for Blast.

Produces well-formed InboundEmail objects deterministically from a seed. All
choices are drawn from a single `random.Random(seed)` instance created inside
`generate_corpus`; nothing here reads module-level random state, the clock,
or the environment. Given the same seed and count, `generate_corpus` always
returns byte-identical InboundEmail objects (and therefore byte-identical
serialized output via blast/serialize.py).

Every generated address uses only the reserved domains and TLDs from the
engine-wide contract (RFC 2606 / RFC 6761): example.com, example.net,
example.org, example.edu, or any domain ending in .test, .invalid, .example,
or .localhost. This holds for the clean generator here and must continue to
hold for every mutator in blast/corrupt.py: a mutator must never garble an
address into a non-reserved domain.
"""
from __future__ import annotations

import random
from typing import List, Tuple

from .payload import Attachment, Envelope, GroundTruth, InboundEmail

# Only RFC 2606 / RFC 6761 reserved domains and TLDs, per the engine-wide
# contract. Do not add a domain here that is not one of the four reserved
# domains or does not end in one of the four reserved TLDs.
RESERVED_DOMAINS: Tuple[str, ...] = (
    "example.com",
    "example.net",
    "example.org",
    "example.edu",
    "mail.example.test",
    "corp.example.invalid",
    "widgets.example",
    "internal.localhost",
)

FIRST_NAMES: Tuple[str, ...] = (
    "alice",
    "bob",
    "carol",
    "dave",
    "erin",
    "frank",
    "grace",
    "heidi",
    "ivan",
    "judy",
    "mallory",
    "oscar",
)

LAST_NAMES: Tuple[str, ...] = (
    "smith",
    "jones",
    "lee",
    "patel",
    "garcia",
    "kim",
    "brown",
    "chen",
    "diaz",
    "nguyen",
    "muller",
    "rossi",
)

TOPICS: Tuple[str, ...] = (
    "your order",
    "the invoice",
    "our meeting",
    "the shipment",
    "account access",
    "the contract",
    "your request",
    "the schedule",
    "onboarding",
    "the quarterly report",
    "the support ticket",
    "the renewal",
)

SUBJECT_TEMPLATES: Tuple[str, ...] = (
    "Re: {topic}",
    "{topic} - action needed",
    "Following up on {topic}",
    "{topic}",
    "Quick question about {topic}",
    "Update on {topic}",
)

GREETINGS: Tuple[str, ...] = ("Hi", "Hello", "Hey", "Dear team", "Good morning")

CLOSINGS: Tuple[str, ...] = ("Thanks", "Best regards", "Best", "Cheers", "Sincerely")

BODY_SENTENCES: Tuple[str, ...] = (
    "I wanted to follow up on this.",
    "Please let me know if you have any questions.",
    "Attached is what we discussed.",
    "Can you confirm receipt of this message?",
    "Looking forward to your reply.",
    "This relates to what we discussed last week.",
    "Let me know if the timeline still works for you.",
    "Happy to hop on a call if that is easier.",
)

MONTHS: Tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

WEEKDAYS: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

HEX_DIGITS = "0123456789abcdef"


def _local_part(rng: random.Random) -> str:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    digits = rng.randint(0, 99)
    if digits:
        return f"{first}.{last}{digits}"
    return f"{first}.{last}"


def _address(rng: random.Random) -> str:
    return f"{_local_part(rng)}@{rng.choice(RESERVED_DOMAINS)}"


def _display_name(rng: random.Random) -> str:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return f"{first.capitalize()} {last.capitalize()}"


def _header_address(name: str, address: str) -> str:
    return f"{name} <{address}>"


def _subject(rng: random.Random) -> str:
    template = rng.choice(SUBJECT_TEMPLATES)
    topic = rng.choice(TOPICS)
    return template.format(topic=topic)


def _body_core(rng: random.Random, topic: str) -> str:
    count = rng.randint(1, 3)
    sentences = rng.sample(BODY_SENTENCES, k=count)
    return f"This message is about {topic}. " + " ".join(sentences)


def _message_id(rng: random.Random, domain: str) -> str:
    token = "".join(rng.choice(HEX_DIGITS) for _ in range(16))
    return f"<{token}@{domain}>"


def _synthetic_date(rng: random.Random) -> str:
    """A deterministic, plausible RFC 5322 Date header value. All fields are
    drawn from the seeded rng; this never reads the real clock, so the same
    seed always produces the same Date string."""
    weekday = rng.choice(WEEKDAYS)
    day = rng.randint(1, 28)
    month = rng.choice(MONTHS)
    year = rng.randint(2022, 2025)
    hour = rng.randint(0, 23)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    return f"{weekday}, {day:02d} {month} {year} {hour:02d}:{minute:02d}:{second:02d} +0000"


def generate_email(rng: random.Random, index: int) -> InboundEmail:
    """Build one well-formed InboundEmail using the given rng. `index` is
    accepted for callers that want it in logging or filenames; it plays no
    part in content selection, so ordering-independent replay of a single
    index is not implied. Determinism comes entirely from `rng`'s sequence of
    draws, in the order they happen below."""
    sender_name = _display_name(rng)
    sender_addr = _address(rng)
    recipient_name = _display_name(rng)
    recipient_addr = _address(rng)

    topic = rng.choice(TOPICS)
    subject = _subject(rng)
    greeting = rng.choice(GREETINGS)
    closing = rng.choice(CLOSINGS)
    body_core = _body_core(rng, topic)

    text = f"{greeting},\n\n{body_core}\n\n{closing},\n{sender_name}\n"
    html = (
        f"<p>{greeting},</p>"
        f"<p>{body_core}</p>"
        f"<p>{closing},<br>{sender_name}</p>"
    )

    from_header = _header_address(sender_name, sender_addr)
    to_header = _header_address(recipient_name, recipient_addr)

    domain = sender_addr.split("@", 1)[1]
    headers = {
        "From": from_header,
        "To": to_header,
        "Subject": subject,
        "Date": _synthetic_date(rng),
        "Message-ID": _message_id(rng, domain),
        "MIME-Version": "1.0",
        "Content-Type": 'multipart/alternative; boundary="clean-boundary"',
    }

    charsets = {
        "to": "UTF-8",
        "from": "UTF-8",
        "subject": "UTF-8",
        "html": "UTF-8",
    }

    return InboundEmail(
        to=to_header,
        from_addr=from_header,
        subject=subject,
        text=text,
        html=html,
        envelope=Envelope(to=(recipient_addr,), from_addr=sender_addr),
        ground_truth=GroundTruth(
            from_addr=sender_addr, subject=subject, body_core=body_core
        ),
        headers=headers,
        charsets=charsets,
        attachments=(),
    )


def generate_corpus(seed: int, count: int) -> List[InboundEmail]:
    """Generate `count` well-formed InboundEmail objects from `seed`.

    Deterministic: generate_corpus(seed, count) called twice, in the same
    process or a fresh one, returns objects that compare equal field for
    field. A single random.Random(seed) is created here and threaded through
    every draw; nothing else contributes entropy.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    rng = random.Random(seed)
    return [generate_email(rng, index) for index in range(count)]
