"""Tagging: the identity that makes a message findable after it is sent.

A payload is only verifiable if the system that received it can be found again,
and the only handle guaranteed to survive an arbitrary intake pipeline is one the
pipeline was not expecting: a unique token in a header, in the body, in the HTML
and in the Message-ID.

Deliberately NOT in the subject. The obvious way to make a payload findable is
to put the token there, and it would destroy the one thing `verify` is built to
check: a subject carrying a synthetic suffix is not the subject that was sent,
so verifying it compares a mangled expectation against a mangled result and
passes. The tag goes somewhere inert instead.

Every tag is a pure function of (prefix, seed, index), so a run artifact can be
re-read later and mean the same thing. What makes two runs' tags different is
the prefix: the CLI default is `run_tag_prefix()`, `hq-<yymmddHHMM>-<4 hex>`,
drawn once per run, so a second run against a persistent store cannot find the
first run's records and report them as duplicates. `--tag-prefix` replaces it
with a fixed value, and the artifact records whichever was used.

The Message-ID is derived from the tag for the same reason. A pipeline that
drops a repeated Message-ID, as a correct helpdesk does, would otherwise drop
every message of a second run with the same seed and the run would report
them lost."""
from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from .readback import Probe

#: The header the tag travels in. Namespaced so it cannot collide with a real
#: message header an intake system might already key on.
TAG_HEADER = "X-TestingHQ-Tag"

#: The prefix TestingHQ stamps on its own domain when generating addresses for
#: tag-bearing payloads. All reserved-example, so it satisfies the engine-wide
#: synthetic-content contract and trips no guardrail.
TAG_DOMAIN_SUFFIX = "hq.example.test"

#: The family every TestingHQ tag prefix starts with, and the prefix library
#: callers get when they pass none. The CLI default is `run_tag_prefix()`.
DEFAULT_TAG_PREFIX = "hq"

_TAG_SHAPE = re.compile(r"^(?P<prefix>.+?)-(?P<seed>-?\d+)-(?P<index>\d{4,})$")

_MARKER_OPEN = "[testinghq:"
_MARKER_CLOSE = "]"


def tag_marker(tag: str) -> str:
    """The exact marker string a stamped payload carries, and the exact string
    an adapter searches for. One function, both ends, so they cannot drift."""
    if not tag:
        raise ValueError("tag must be a non-empty string")
    return f"{_MARKER_OPEN}{tag}{_MARKER_CLOSE}"


def run_tag_prefix(
    now: Optional[datetime] = None, token: Optional[str] = None
) -> str:
    """A tag prefix no other run produces: `hq-<yymmddHHMM>-<4 hex>`.

    The minute stamp makes a stray record's age readable at a glance; the
    random suffix separates two runs started in the same minute. `now` and
    `token` are parameters so a test can pin them."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%y%m%d%H%M")
    suffix = token if token is not None else os.urandom(2).hex()
    return f"{DEFAULT_TAG_PREFIX}-{stamp}-{suffix}"


def split_tag(tag: Optional[str]) -> Optional[Tuple[str, int, int]]:
    """(prefix, seed, index) for a tag `make_tag` could have produced, or None.

    Lets a reader tell this run's records (same prefix) from another
    TestingHQ run's (a prefix in the `hq` family) from records TestingHQ never
    tagged at all."""
    if not tag or not isinstance(tag, str):
        return None
    match = _TAG_SHAPE.match(tag.strip())
    if match is None:
        return None
    return match.group("prefix"), int(match.group("seed")), int(match.group("index"))


def make_tag(prefix: str, seed: int, index: int) -> str:
    """The unique token for one message of one run.

    Unique within a run by `index`, and across runs by (`prefix`, `seed`).
    """
    if not prefix:
        raise ValueError("tag prefix must be a non-empty string")
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index}")
    return f"{prefix}-{seed}-{index:04d}"


def find_tag(text: Optional[str]) -> Optional[str]:
    """Recover a tag from any text a system stored: a header blob, a body, an
    HTML fragment. Returns None when the text carries no tag.

    Two forms are recognised, because the tag travels in two forms: a bare
    value on its own header line, and a bracketed marker inside prose. Scans
    rather than assuming a position, because the whole point is that the system
    under test is free to store the message however it likes.

    The header form is matched case-insensitively on the header name only,
    never on the value, so a body that happens to contain the letters of the
    header name is not mistaken for one.
    """
    if not text or not isinstance(text, str):
        return None

    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            continue
        if name.strip().lower() != TAG_HEADER.lower():
            continue
        candidate = value.strip()
        if candidate:
            return candidate
        return None

    start = text.find(_MARKER_OPEN)
    while start != -1:
        end = text.find(_MARKER_CLOSE, start)
        if end == -1:
            return None
        candidate = text[start + len(_MARKER_OPEN) : end]
        if candidate:
            return candidate
        start = text.find(_MARKER_OPEN, start + 1)
    return None


def _body_suffix(tag: str) -> str:
    return f"\n{tag_marker(tag)}\n"


def stamp(
    email: InboundEmail,
    tag: str,
    *,
    message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    subject: Optional[str] = None,
) -> InboundEmail:
    """Stamp a payload with a tag, plus any RFC 5322 threading headers the caller
    is pinning.

    The copy is total: headers are copied rather than shared, because
    `InboundEmail` is frozen but its `headers` dict is not, and mutating it in
    place would corrupt the generated corpus other code still holds.

    A pinned `subject` overrides `ground_truth` too. The ground truth's job is
    to describe what went on the wire, and a stamped payload whose `subject` and
    `ground_truth.subject` disagree have every check grade against something
    nobody sent. That was not hypothetical: the reply-first scenario stamps
    `Re: ` prefixes, and leaving the ground truth alone made both threaded
    scenarios report a mismatch on a reply's subject.

    Unless the caller pins `message_id`, the Message-ID becomes
    `<{tag}@hq.example.test>`, replacing the generator's: the generator's id
    is a function of the seed alone, so two runs with one seed would share it.
    """

    headers = dict(email.headers)
    headers[TAG_HEADER] = tag
    headers["Message-ID"] = (
        message_id if message_id is not None else f"<{build_chain_message_id(tag)}>"
    )
    if in_reply_to is not None:
        headers["In-Reply-To"] = in_reply_to
    if references is not None:
        headers["References"] = references
    if subject is not None:
        # The header blob is what every wire format serializes, so a pinned
        # subject has to land there too or the two disagree on the wire.
        headers["Subject"] = subject

    changes: dict = {
        "headers": headers,
        "text": email.text + _body_suffix(tag),
        "html": email.html + f"<p>{tag_marker(tag)}</p>",
    }
    if subject is not None:
        changes["subject"] = subject
        changes["ground_truth"] = replace(email.ground_truth, subject=subject)
    return _with_changes(email, changes)


def _with_changes(email: InboundEmail, changes: dict) -> InboundEmail:
    """`InboundEmail.with_changes` where the payload model has it, so derived
    data stays in sync by the model's own rule; `dataclasses.replace`
    otherwise, with the headers already set directly by the caller.

    The subject goes in a second call: the model only rewrites the Subject
    header (RFC 2047 encoded where needed) when `headers` is not passed in
    the same call."""
    helper = getattr(email, "with_changes", None)
    if not callable(helper):
        return replace(email, **changes)
    changes = dict(changes)
    subject = changes.pop("subject", None)
    updated = helper(**changes)
    if subject is not None:
        updated = updated.with_changes(subject=subject)
    return updated


def message_id_of(email: InboundEmail) -> str:
    """The payload's Message-ID, bare (angle brackets stripped).

    Falls back to the tag when the payload carries no Message-ID header at all,
    so a thread can always be built from a probe and so a readback comparing
    threading has something well-defined to compare against.
    """
    value = (email.headers or {}).get("Message-ID")
    if isinstance(value, str) and value.strip():
        return value.strip().strip("<>").strip()
    return find_tag(email.text) or ""


def build_chain_message_id(tag: str) -> str:
    """The bare Message-ID for the message carrying `tag`: unique wherever
    the tag is, and reproducible from it. Reserved-domain, so it satisfies the
    synthetic-content contract."""
    return f"{tag}@{TAG_DOMAIN_SUFFIX}"


def probe_for(
    email: InboundEmail,
    tag: str,
    record_id: str,
    payload_sha256: str,
) -> Probe:
    """Build the Probe an adapter is handed for this payload.

    `payload_sha256` is passed in rather than computed here so this module
    stays free of a dependency on `core.report`; the hash belongs to the
    reporting layer and the caller already has it.
    """
    recipient = email.envelope.to[0] if email.envelope.to else ""
    return Probe(
        record_id=record_id,
        tag=tag,
        payload_sha256=payload_sha256,
        from_addr=email.ground_truth.from_addr,
        subject=email.ground_truth.subject,
        recipient=recipient,
        message_id=message_id_of(email),
        attachment_names=tuple(
            attachment.filename for attachment in email.attachments
        ),
    )


def stamp_corpus(
    corpus: Sequence[InboundEmail],
    seed: int,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
) -> List[Tuple[InboundEmail, str, str]]:
    """Stamp a generated corpus, returning (email, tag, record_id) triples.

    `record_id` follows the same `{category}-{seed}-{index:04d}` shape the rest
    of the engine uses for blast and barrage record ids, with `clean` as the
    category, because these payloads are clean by construction: see
    `testinghq/pipeline/verify.py` for why verification never runs against a
    garbled payload.
    """
    stamped = []
    for index, email in enumerate(corpus):
        tag = make_tag(tag_prefix, seed, index)
        stamped.append((stamp(email, tag), tag, f"clean-{seed}-{index:04d}"))
    return stamped


def thread_header_text(email: InboundEmail) -> str:
    """The header blob as a single string, for adapters that store raw headers
    and for tests that assert the stamp really travelled."""
    return "".join(f"{name}: {value}\r\n" for name, value in (email.headers or {}).items())


def harvest_tag(*candidates: Optional[str]) -> Optional[str]:
    """The first tag recoverable from any of `candidates`, in order. The order
    is the search priority documented at the top of this module, so an adapter
    that stores several of these resolves the same way every time."""
    for candidate in candidates:
        tag = find_tag(candidate)
        if tag:
            return tag
    return None
