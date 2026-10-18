"""Clean corpus generator for Blast.

Produces well-formed InboundEmail objects deterministically from a seed. All
choices come from one `random.Random(seed)` created inside `generate_corpus`;
nothing reads module-level random state, the clock, or the environment.

A clean message looks like mail a real client sent and a real MTA delivered:
the display name and the local part come from one person, the subject and the
body are about the same topic, the weekday matches the date, the HTML has a
Gmail or Outlook wrapper, replies carry In-Reply-To and References, and every
message has Received, Return-Path and Authentication-Results headers.

Every address and host uses only reserved names (RFC 2606 / RFC 6761):
example.com, example.net, example.org, example.edu, or a name ending in .test,
.invalid, .example or .localhost. IP addresses come from TEST-NET-1
(192.0.2.0/24, RFC 5737). Mutators in blast/corrupt.py must keep this true.
"""
from __future__ import annotations

import datetime
import html as html_lib
import random
from typing import List, Tuple

from .payload import Envelope, GroundTruth, InboundEmail, encode_header_value

# Only RFC 2606 / RFC 6761 reserved domains and TLDs.
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
    "alice", "bob", "carol", "dave", "erin", "frank",
    "grace", "heidi", "ivan", "judy", "mallory", "oscar",
)

LAST_NAMES: Tuple[str, ...] = (
    "smith", "jones", "lee", "patel", "garcia", "kim",
    "brown", "chen", "diaz", "nguyen", "muller", "rossi",
)

# Two topics are not ASCII, so clean subjects exercise RFC 2047 too.
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
    "the café booking",
    "the Zürich office move",
)

SUBJECT_TEMPLATES: Tuple[str, ...] = (
    "Re: {topic}",
    "{topic} - action needed",
    "Following up on {topic}",
    "{topic}",
    "Quick question about {topic}",
    "Update on {topic}",
)

#: Opening sentences. Each names the topic, so subject and body agree.
OPENERS: Tuple[str, ...] = (
    "I am writing about {topic}.",
    "Quick note regarding {topic}.",
    "I have a question about {topic}.",
    "Following up on {topic} from earlier.",
    "This message is about {topic}.",
)

GREETINGS: Tuple[str, ...] = ("Hi", "Hello", "Hey", "Dear team", "Good morning")

#: Greetings that take the recipient's first name.
NAMED_GREETINGS = frozenset({"Hi", "Hello", "Hey"})

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
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

MONTH_NAMES: Tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

WEEKDAYS: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WEEKDAY_NAMES: Tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)

TIMEZONES: Tuple[str, ...] = ("+0000", "-0500", "+0100", "+0530", "-0800", "+0900")

HEX_DIGITS = "0123456789abcdef"

#: Local-part styles, built from one person's first and last name.
LOCAL_STYLES: Tuple[str, ...] = ("first.last", "flast", "first_last", "firstl", "first")


def _local_part(first: str, last: str, style: str) -> str:
    if style == "first.last":
        return f"{first}.{last}"
    if style == "flast":
        return f"{first[0]}{last}"
    if style == "first_last":
        return f"{first}_{last}"
    if style == "firstl":
        return f"{first}{last[0]}"
    return first


def _person(rng: random.Random) -> Tuple[str, str]:
    """A display name and an address for the same person."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    local = _local_part(first, last, rng.choice(LOCAL_STYLES))
    digits = rng.randint(0, 99)
    if digits < 30:
        local = f"{local}{digits}"
    return f"{first.capitalize()} {last.capitalize()}", f"{local}@{rng.choice(RESERVED_DOMAINS)}"


def _address(rng: random.Random) -> str:
    """An address on a reserved domain, for callers that need only one."""
    return _person(rng)[1]


def _header_address(name: str, address: str) -> str:
    return f"{name} <{address}>"


def _token(rng: random.Random, length: int) -> str:
    return "".join(rng.choice(HEX_DIGITS) for _ in range(length))


def _message_id(rng: random.Random, domain: str) -> str:
    return f"<{_token(rng, 16)}@{domain}>"


def synthetic_datetime(rng: random.Random) -> Tuple[datetime.datetime, str]:
    """A seeded date and a timezone offset string. Never reads the clock."""
    when = datetime.datetime(
        rng.randint(2022, 2025), rng.randint(1, 12), rng.randint(1, 28),
        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
    )
    return when, rng.choice(TIMEZONES)


def rfc5322_date(when: datetime.datetime, tz: str) -> str:
    """`Tue, 05 Mar 2024 10:12:33 +0000`, with the weekday computed from the
    date. Built from tables rather than strftime, which is locale dependent."""
    return (
        f"{WEEKDAYS[when.weekday()]}, {when.day:02d} {MONTHS[when.month - 1]} "
        f"{when.year} {when.hour:02d}:{when.minute:02d}:{when.second:02d} {tz}"
    )


def _extra_sentence(rng: random.Random, sender_name: str) -> Tuple[str, str]:
    """About a sixth of bodies carry a URL, an order number, or a signature
    block. Returns (sentence for the body, signature block)."""
    if rng.random() >= 1 / 6:
        return "", ""
    kind = rng.choice(("url", "order", "signature"))
    if kind == "url":
        return f"The details are at https://www.example.com/orders/{rng.randint(10000, 99999)}.", ""
    if kind == "order":
        return f"My order number is {rng.randint(100000, 999999)}-{rng.randint(10, 99)}.", ""
    phone = f"+1 555 01{rng.randint(0, 99):02d}"
    return "", f"--\n{sender_name}\n{phone}"


def _gmail_html(lines: List[str]) -> str:
    inner = "<div><br></div>".join(f"<div>{line}</div>" for line in lines)
    return f'<html><body><div dir="ltr">{inner}</div></body></html>'


def _outlook_html(lines: List[str]) -> str:
    inner = "".join(f'<p class="MsoNormal">{line}</p>' for line in lines)
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; '
        'charset=utf-8"></head><body lang="EN-US">'
        f'<div class="WordSection1">{inner}</div></body></html>'
    )


def _html_lines(*paragraphs: str) -> List[str]:
    return [
        html_lib.escape(p, quote=False).replace("\n", "<br>") for p in paragraphs if p
    ]


def generate_email(rng: random.Random, index: int) -> InboundEmail:
    """Build one well-formed InboundEmail from `rng`. `index` plays no part in
    content selection; determinism comes from the order of draws below."""
    sender_name, sender_addr = _person(rng)
    recipient_name, recipient_addr = _person(rng)

    topic = rng.choice(TOPICS)
    subject = rng.choice(SUBJECT_TEMPLATES).format(topic=topic)
    subject = subject[0].upper() + subject[1:]
    greeting = rng.choice(GREETINGS)
    closing = rng.choice(CLOSINGS)

    sentences = rng.sample(BODY_SENTENCES, k=rng.randint(1, 3))
    opener = rng.choice(OPENERS).format(topic=topic)
    extra, signature = _extra_sentence(rng, sender_name)
    body_core = " ".join([opener, *sentences] + ([extra] if extra else []))

    salutation = (
        f"{greeting} {recipient_name.split()[0]},"
        if greeting in NAMED_GREETINGS
        else f"{greeting},"
    )
    sign_off = f"{closing},\n{sender_name.split()[0]}"
    text = f"{salutation}\n\n{body_core}\n\n{sign_off}\n"
    if signature:
        text += f"\n{signature}\n"
    wrapper = rng.choice((_gmail_html, _outlook_html))
    html = wrapper(_html_lines(salutation, body_core, sign_off, signature))

    sender_domain = sender_addr.split("@", 1)[1]
    recipient_domain = recipient_addr.split("@", 1)[1]
    when, tz = synthetic_datetime(rng)
    date = rfc5322_date(when, tz)
    ip = f"192.0.2.{rng.randint(1, 254)}"
    relay = f"mail-{_token(rng, 6)}.{sender_domain}"

    headers = {
        "Received": (
            f"from {relay} ({relay} [{ip}]) by mx.{recipient_domain} with ESMTPS "
            f"id {_token(rng, 12)} for <{recipient_addr}>; {date}"
        ),
        "Return-Path": f"<{sender_addr}>",
        "Authentication-Results": (
            f"mx.{recipient_domain}; spf=pass smtp.mailfrom={sender_addr}; "
            f"dkim=pass header.d={sender_domain}; dmarc=pass header.from={sender_domain}"
        ),
        "MIME-Version": "1.0",
        "From": _header_address(sender_name, sender_addr),
        "To": _header_address(recipient_name, recipient_addr),
        "Subject": encode_header_value(subject),
        "Date": date,
        "Message-ID": _message_id(rng, sender_domain),
    }
    if subject.startswith("Re:"):
        parent = _message_id(rng, recipient_domain)
        root = _message_id(rng, sender_domain)
        headers["In-Reply-To"] = parent
        headers["References"] = f"{root} {parent}"
    headers["Content-Type"] = (
        f'multipart/alternative; boundary="000000000000{_token(rng, 14)}"'
    )

    charsets = {
        "to": "UTF-8",
        "from": "UTF-8",
        "subject": "UTF-8",
        "text": "UTF-8",
        "html": "UTF-8",
    }

    return InboundEmail(
        to=headers["To"],
        from_addr=headers["From"],
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

    Deterministic: the same seed and count give equal objects in any process.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    rng = random.Random(seed)
    return [generate_email(rng, index) for index in range(count)]
