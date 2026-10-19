"""Real mail as a Blast source: `blast generate|fire --corpus <dir>`.

The generated corpus is synthetic by construction. A directory of real mail is
not: it carries the addresses of the people who sent and received it, and a
helpdesk that opens a ticket for one of those messages may well reply to them.
So the one rule this module exists to keep is that no address read from a
corpus file reaches a target.

Every `.eml` and `.box` file under the directory (recursive, extension matched
case-insensitively) is read, in order of its path relative to the directory.
Any other file is skipped and counted. Each file is parsed with
`email.message_from_bytes` and rebuilt as an `InboundEmail`:

- Every address is rewritten to `<stem>-<hash>@corpus.example.test`, where the
  stem is the old local part reduced to `[a-z0-9-]` and the hash is the first
  ten hex digits of the sha256 of the whole old address. The same address
  always maps to the same new one, so a sender who appears in several files is
  still one sender. An address already on a reserved domain or TLD is left as
  it is. Address headers (From, To, Cc, Reply-To, Sender, Return-Path, the
  Resent and delivery headers, and the rest of `ADDRESS_HEADERS`) are parsed
  and rebuilt; anything in them that is not an address is dropped. Every other
  header, the subject, the text and HTML bodies, attachment file names and
  textual attachments (`text/*`, `message/*`, JSON and XML) have anything that
  looks like an address rewritten by pattern. Binary attachments are sent as
  they are.
- The envelope sender is the rewritten Return-Path, else the first From
  address. The envelope recipients are the To addresses, else the Cc ones,
  else `support@corpus.example.test`.
- The message is tagged the way the pipeline tools tag theirs
  (`pipeline.messages.stamp`): an `X-TestingHQ-Tag` header, the
  `[testinghq:<tag>]` marker at the end of the text and HTML bodies, and a
  Message-ID derived from the tag. The marker is also appended to the subject,
  so a helpdesk whose only search is over subjects can still find the message.

After the rewrite each payload is checked twice: its address headers through
the same `guardrails.require_synthetic_content` the send path uses, and every
header and body through the address pattern. A file that is empty, has no
header fields, cannot be rebuilt, or fails either check becomes an
`unparseable` entry with the reason; it is recorded and never sent.

Determinism: the payload for a file is a pure function of its bytes, its index,
the seed and the tag prefix, so `blast replay` can rebuild a corpus run from
the same directory and check the hashes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import Message
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..core import guardrails
from ..core.report import CORPUS, UNPARSEABLE as UNPARSEABLE_OUTCOME
from ..pipeline import messages
from .corpus import CorpusError
from .payload import Attachment, Envelope, GroundTruth, InboundEmail, encode_header_value

#: The file extensions read from a corpus directory, matched case-insensitively.
EXTENSIONS = (".eml", ".box")

#: The category every corpus record carries.
CATEGORY = CORPUS

#: The outcome of a corpus file that could not be turned into a payload.
UNPARSEABLE = UNPARSEABLE_OUTCOME

#: Where every rewritten address lives. Under `.test`, so it is synthetic by the
#: guardrails' own rule.
SYNTHETIC_DOMAIN = "corpus.example.test"

#: The envelope recipient of a file that names no To or Cc address.
DEFAULT_RECIPIENT = f"support@{SYNTHETIC_DOMAIN}"

#: Headers whose value is a list of addresses. Parsed and rebuilt, not pattern
#: rewritten, so a display name or comment cannot hide an address.
ADDRESS_HEADERS = (
    "From", "To", "Cc", "Bcc", "Reply-To", "Sender",
    "Resent-From", "Resent-To", "Resent-Cc", "Resent-Bcc", "Resent-Sender",
    "Resent-Reply-To", "Return-Path", "Delivered-To", "X-Original-To",
    "Envelope-To", "X-Envelope-To", "X-Envelope-From", "Errors-To",
    "Disposition-Notification-To", "Return-Receipt-To", "Mail-Followup-To",
    "Mail-Reply-To", "X-Sender", "X-Receiver", "Apparently-To",
    "X-Original-From", "X-Forwarded-To",
)
_ADDRESS_HEADER_NAMES = {name.casefold(): name for name in ADDRESS_HEADERS}

#: Headers not copied from the file: the MIME structure is rebuilt from the
#: parts, the Message-ID and tag come from `stamp`, and the subject is set from
#: the payload's own field.
_DROPPED_HEADERS = frozenset(
    {
        "content-transfer-encoding", "content-length", "content-disposition",
        "content-id", "content-description", "content-md5", "message-id",
        "subject", messages.TAG_HEADER.casefold(),
    }
)

#: Canonical spelling for headers other code looks up by exact name.
_CANONICAL = {"mime-version": "MIME-Version", "content-type": "Content-Type", "date": "Date"}

#: Subtypes of `application/` whose content is text and is pattern rewritten.
_TEXT_SUBTYPE = re.compile(r"(json|xml)$")

#: Anything that looks like an address: a dot-atom or quoted local part, `@`,
#: then a domain or an address literal. Deliberately broader than RFC 5322
#: (`\w` is Unicode, the domain needs no dot), because the cost of a false
#: match is a rewritten word and the cost of a miss is a real address sent.
ADDRESS_PATTERN = re.compile(
    r'(?:"[^"\r\n]*"|[\w.!#$%&\'*+/=?^`{|}~-]+)'
    r"@(?:\[[^\]\s]*\]|[\w-]+(?:\.[\w-]+)*)"
)
_LEADING_PUNCTUATION = ".!#$%&'*+/=?^`{|}~-"


class UnusableMessage(ValueError):
    """A corpus file that cannot become a payload. The message is the reason
    the record carries."""


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def rewrite_address(address: str) -> str:
    """The synthetic address that stands in for `address`.

    A pure function of the address (case-insensitive), so a sender seen in two
    files maps to one new address. An address already on a reserved domain or
    TLD comes back unchanged."""
    address = address.strip()
    if guardrails.is_synthetic_address(address):
        return address
    local = address.rpartition("@")[0]
    digest = hashlib.sha256(
        address.casefold().encode("utf-8", "surrogatepass")
    ).hexdigest()[:10]
    stem = re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")[:24].strip("-")
    return f"{stem or 'user'}-{digest}@{SYNTHETIC_DOMAIN}"


def _split_lead(found: str) -> Tuple[str, str]:
    """Leading punctuation the pattern swept into a local part (a quote or a
    dash before the address in prose), and the address after it. A local part
    that is nothing but punctuation keeps it all."""
    local = found.rpartition("@")[0]
    lead = local[: len(local) - len(local.lstrip(_LEADING_PUNCTUATION))]
    if lead == local:
        return "", found
    return lead, found[len(lead):]


def _rewrite_match(match: "re.Match[str]") -> str:
    lead, address = _split_lead(match.group(0))
    return lead + rewrite_address(address)


def rewrite_text(text: str) -> str:
    """`text` with everything that looks like an address rewritten."""
    if not text or "@" not in text:
        return text
    return ADDRESS_PATTERN.sub(_rewrite_match, text)


def rewrite_bytes(data: bytes) -> bytes:
    """`rewrite_text` over bytes, leaving every byte outside an address as it
    was (undecodable bytes pass through as surrogates)."""
    if b"@" not in data:
        return data
    text = data.decode("utf-8", "surrogateescape")
    return rewrite_text(text).encode("utf-8", "surrogateescape")


def unsafe_addresses(text: str) -> List[str]:
    """Every address-looking string in `text` that is not synthetic."""
    if not text or "@" not in text:
        return []
    found = []
    for match in ADDRESS_PATTERN.finditer(text):
        candidate = _split_lead(match.group(0))[1]
        if not guardrails.is_synthetic_address(candidate):
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    """`text` with any raw bytes the parser kept as surrogates decoded as
    UTF-8 (a header written in raw UTF-8 is common), else replaced."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    raw = text.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _one_line(value: Any) -> str:
    return _clean(" ".join(line.strip() for line in str(value).splitlines()).strip())


def _quote_name(name: str) -> str:
    if not name:
        return ""
    if re.search(r'[()<>@,;:\\".\[\]]', name):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return name


def _header_addresses(header: Any) -> List[Tuple[str, str]]:
    """(display name, address) pairs in one parsed address header."""
    try:
        parsed = [(a.display_name, a.addr_spec) for a in header.addresses]
    except Exception:  # an unparseable header falls back to the lenient parser
        parsed = getaddresses([_one_line(header)])
    return parsed


@dataclass(frozen=True)
class _Addresses:
    """One rewritten address header: the bare addresses, the decoded display
    form for the payload field, and the RFC 2047 form for `headers`."""

    bare: Tuple[str, ...]
    display: str
    encoded: str


def _rewrite_address_header(values: Sequence[Any]) -> _Addresses:
    bare: List[str] = []
    display: List[str] = []
    encoded: List[str] = []
    for value in values:
        for name, address in _header_addresses(value):
            address = _clean((address or "").strip())
            if "@" not in address:
                continue
            new_address = rewrite_address(address)
            new_name = _one_line(rewrite_text(name or ""))
            bare.append(new_address)
            quoted = _quote_name(new_name)
            display.append(f"{quoted} <{new_address}>" if quoted else new_address)
            encoded.append(formataddr((new_name, new_address), charset="utf-8"))
    return _Addresses(tuple(bare), ", ".join(display), ", ".join(encoded))


def _part_text(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(part.get_payload() or "").encode("utf-8", "surrogateescape")
    charset = part.get_content_charset() or "utf-8"
    try:
        text = bytes(raw).decode(charset, errors="replace")
    except LookupError:
        text = bytes(raw).decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_textual(content_type: str) -> bool:
    maintype, _, subtype = content_type.partition("/")
    return maintype in ("text", "message") or bool(
        maintype == "application" and _TEXT_SUBTYPE.search(subtype)
    )


def _attachment(part: Message) -> Attachment:
    content_type = part.get_content_type()
    payload = part.get_payload()
    if isinstance(payload, list):
        # message/rfc822 and friends: the payload is the embedded message.
        try:
            content = b"".join(
                sub.as_bytes(policy=policy.SMTP) if isinstance(sub, Message) else b""
                for sub in payload
            )
        except Exception:
            content = b""
    else:
        decoded = part.get_payload(decode=True)
        content = bytes(decoded) if isinstance(decoded, (bytes, bytearray)) else b""
    if _is_textual(content_type):
        content = rewrite_bytes(content)
    filename = rewrite_text(_one_line(part.get_filename() or ""))
    return Attachment(filename=_one_line(filename), content_type=content_type, content=content)


def _leaves(part: Message) -> Iterable[Message]:
    """Every non-multipart part, depth first. An embedded message is one
    leaf, so it travels as an attachment rather than being flattened."""
    payload = part.get_payload()
    if part.get_content_maintype() == "multipart" and isinstance(payload, list):
        for sub in payload:
            if isinstance(sub, Message):
                yield from _leaves(sub)
        return
    yield part


def _bodies(message: Message) -> Tuple[str, str, Tuple[Attachment, ...]]:
    texts: List[str] = []
    htmls: List[str] = []
    attachments: List[Attachment] = []
    for part in _leaves(message):
        content_type = part.get_content_type()
        inline = part.get_content_disposition() != "attachment" and not part.get_filename()
        if inline and content_type == "text/plain":
            texts.append(_part_text(part))
        elif inline and content_type == "text/html":
            htmls.append(_part_text(part))
        else:
            attachments.append(_attachment(part))
    text = rewrite_text("\n".join(texts))
    html = rewrite_text("\n".join(htmls))
    return text, html, tuple(attachments)


def _copied_headers(message: Message) -> Dict[str, str]:
    """Every header that is neither an address header nor dropped, first
    occurrence of each name only, addresses rewritten, RFC 2047 encoded where
    the value is not ASCII."""
    headers: Dict[str, str] = {}
    seen = set()
    for name, value in message.items():
        key = name.casefold()
        if key in seen or key in _DROPPED_HEADERS or key in _ADDRESS_HEADER_NAMES:
            continue
        seen.add(key)
        headers[_CANONICAL.get(key, name)] = encode_header_value(
            rewrite_text(_one_line(value))
        )
    return headers


def parse_message(data: bytes) -> InboundEmail:
    """The untagged, rewritten payload for one corpus file's bytes.

    Raises UnusableMessage for a file that holds no message, and lets any
    other error propagate; `load_entry` turns both into an unparseable entry."""
    if not data.strip():
        raise UnusableMessage("the file is empty")
    message = message_from_bytes(data, policy=policy.default)
    if not message.keys():
        raise UnusableMessage("no header fields: the file does not start with a message header")

    rewritten: Dict[str, _Addresses] = {}
    for key, name in _ADDRESS_HEADER_NAMES.items():
        values = message.get_all(name)
        if values:
            rewritten[key] = _rewrite_address_header(values)

    headers = _copied_headers(message)
    for key, name in _ADDRESS_HEADER_NAMES.items():
        if key not in rewritten:
            continue
        if key == "return-path":
            first = rewritten[key].bare[:1]
            headers[name] = f"<{first[0]}>" if first else "<>"
        elif rewritten[key].encoded:
            headers[name] = rewritten[key].encoded

    empty = _Addresses((), "", "")
    sender = rewritten.get("from", empty)
    to = rewritten.get("to", empty)
    cc = rewritten.get("cc", empty)
    return_path = rewritten.get("return-path", empty)

    subject = rewrite_text(_one_line(message.get("Subject", "")))
    text, html, attachments = _bodies(message)

    envelope_from = (return_path.bare or sender.bare or ("",))[0]
    envelope_to = to.bare or cc.bare or (DEFAULT_RECIPIENT,)
    headers["Subject"] = encode_header_value(subject)
    return InboundEmail(
        to=to.display,
        from_addr=sender.display,
        subject=subject,
        text=text,
        html=html,
        envelope=Envelope(to=tuple(envelope_to), from_addr=envelope_from),
        ground_truth=GroundTruth(
            from_addr=(sender.bare or ("",))[0], subject=subject, body_core=text.strip(),
        ),
        headers=headers,
        charsets={name: "UTF-8" for name in ("to", "from", "subject", "text", "html")},
        attachments=attachments,
    )


def tag(email: InboundEmail, tag_value: str) -> InboundEmail:
    """Stamp `email` as the pipeline tools stamp theirs, plus the marker on
    the subject."""
    marker = messages.tag_marker(tag_value)
    subject = f"{email.subject} {marker}" if email.subject else marker
    return messages.stamp(email, tag_value, subject=subject)


def address_fields(email: InboundEmail) -> List[str]:
    """Every address-bearing value of a corpus payload, for the guardrail:
    the fields the send path checks plus every address header. A null
    Return-Path (`<>`) names nobody and is left out."""
    fields = [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]
    for name, value in email.headers.items():
        if name.casefold() not in _ADDRESS_HEADER_NAMES:
            continue
        if name.casefold() == "return-path" and value.strip() == "<>":
            continue
        fields.append(value)
    return fields


def check_synthetic(email: InboundEmail) -> None:
    """Raise UnusableMessage if any address in `email` is not synthetic."""
    try:
        guardrails.require_synthetic_content(address_fields(email))
    except guardrails.GuardrailError as exc:
        raise UnusableMessage(f"an address could not be made synthetic: {exc}") from None
    texts = [email.subject, email.text, email.html, *email.headers.values()]
    texts.extend(a.filename for a in email.attachments)
    for text in texts:
        leaked = unsafe_addresses(text)
        if leaked:
            raise UnusableMessage(
                f"an address could not be made synthetic: {leaked[0]!r}"
            )


# ---------------------------------------------------------------------------
# The directory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusEntry:
    """One corpus file: its payload, or why it has none."""

    index: int
    source: str
    email: Optional[InboundEmail] = None
    error: Optional[str] = None


def scan(corpus_dir: Path) -> Tuple[List[Tuple[str, Path]], int]:
    """(relative name, path) of every mail file under `corpus_dir` in name
    order, and how many other files were skipped."""
    found: List[Tuple[str, Path]] = []
    ignored = 0
    for path in corpus_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in EXTENSIONS:
            found.append((path.relative_to(corpus_dir).as_posix(), path))
        else:
            ignored += 1
    found.sort(key=lambda item: item[0])
    return found, ignored


def load_entry(index: int, source: str, data: bytes, seed: int, tag_prefix: str) -> CorpusEntry:
    """The entry for one file's bytes. Never raises: whatever goes wrong is
    the entry's `error`."""
    from ..core.formats import MimeFormat
    from ..core.report import payload_sha256
    from ..core.transport import DEFAULT_BOUNDARY

    try:
        email = tag(parse_message(data), messages.make_tag(tag_prefix, seed, index))
        check_synthetic(email)
        # Build both wire shapes once here, so a message the formats cannot
        # serialize is recorded now rather than failing mid-run.
        MimeFormat(DEFAULT_BOUNDARY).message_bytes(email)
        payload_sha256(email)
    except UnusableMessage as exc:
        return CorpusEntry(index=index, source=source, error=str(exc))
    except Exception as exc:  # any parser or builder failure is the file's
        return CorpusEntry(
            index=index, source=source, error=f"{type(exc).__name__}: {exc}"
        )
    return CorpusEntry(index=index, source=source, email=email)


class MailCorpusPlan:
    """A corpus read from a directory, with the interface `CorpusPlan` gives
    the blast commands: `seed`, `size`, `build()`, `cases()`, `config()`.

    `entries` holds every file read, parsed or not, in order; `build()`
    returns only the ones that became payloads."""

    def __init__(
        self,
        corpus_dir: str,
        *,
        seed: int = 0,
        count: Optional[int] = None,
        tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    ) -> None:
        if count is not None and count < 1:
            raise CorpusError(f"--count must be 1 or more with --corpus, got {count}")
        if not tag_prefix:
            raise CorpusError("--tag-prefix must not be empty")
        root = Path(corpus_dir)
        if not root.is_dir():
            raise CorpusError(f"--corpus {corpus_dir!r} is not a directory")
        files, ignored = scan(root)
        if not files:
            raise CorpusError(
                f"--corpus {corpus_dir!r} holds no .eml or .box files "
                f"({ignored} other file(s) ignored)"
            )
        if count is not None:
            files = files[:count]
        self.corpus_dir = str(corpus_dir)
        self.seed = seed
        self.tag_prefix = tag_prefix
        self.ignored = ignored
        entries = []
        for index, (source, path) in enumerate(files):
            try:
                data = path.read_bytes()
            except OSError as exc:
                entries.append(CorpusEntry(index=index, source=source, error=f"could not read: {exc}"))
                continue
            entries.append(load_entry(index, source, data, seed, tag_prefix))
        self.entries: Tuple[CorpusEntry, ...] = tuple(entries)

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def unparseable(self) -> Tuple[CorpusEntry, ...]:
        return tuple(entry for entry in self.entries if entry.email is None)

    def build(self) -> List[Tuple[InboundEmail, str]]:
        return [(entry.email, CATEGORY) for entry in self.entries if entry.email is not None]

    def cases(self) -> None:
        return None

    def config(self) -> Dict[str, Any]:
        return {
            "corpus_dir": self.corpus_dir,
            "corpus_ignored": self.ignored,
            "count": self.size,
            "seed": self.seed,
            "tag_prefix": self.tag_prefix,
        }

    @classmethod
    def from_config(cls, seed: int, config: Mapping[str, Any]) -> "MailCorpusPlan":
        """Rebuild the plan an artifact's `config` records, from the same
        directory."""
        return cls(
            config["corpus_dir"],
            seed=seed,
            count=config.get("count"),
            tag_prefix=config.get("tag_prefix") or messages.DEFAULT_TAG_PREFIX,
        )
