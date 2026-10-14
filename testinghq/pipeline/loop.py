"""loop: auto-reply and mail-loop detection.

Two auto-responders answering each other create thousands of tickets overnight.
This sends machine-generated mail a correct pipeline must recognise and must not
act on, then reports what the pipeline did with each message.

The loop bait is two-phase, because that is how a live loop starts. First an
ordinary message goes in and the pipeline's outbound sink is read for its
acknowledgement. Then the bait goes in: an auto-reply from the same customer's
mailbox, addressed to the pipeline's reply address, with `In-Reply-To` set to the
Message-ID of that acknowledgement. A pipeline that answers it is in a loop.
When no acknowledgement is seen the bait falls back to a fabricated parent id and
the report says so.

The outbound sink is enumerated after the run when the adapter can list. A new
outbound record that carries no tag of this run and replies to none of its
messages is "outbound, unattributed", and it makes the auto-reply check a
finding: an auto-reply that quotes nothing cannot be matched by tag, so the tool
cannot say the pipeline stayed quiet.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from email import message_from_bytes
from email import policy as email_policy
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import Attachment, Envelope, GroundTruth, InboundEmail
from ..core.formats import EncodedBody, MimeFormat, SendgridRawFormat, WireFormat
from ..core.targets import resolve_target
from ..core.transport import DEFAULT_BOUNDARY, encode_multipart
from .adapters import ReadbackConfig, parse_readback_config
from .common import (
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    READBACK_FAILED,
    VERDICT_SOURCE_READBACK,
    close_adapter,
    prepare_readback,
    read_back_all,
    require_synthetic,
    send_all,
)
from .messages import build_chain_message_id, make_tag, probe_for, stamp
from .readback import Probe, can_enumerate
from .toolconfig import ToolConfigError, read_tool_table, read_tool_table_if_present

SCHEMA_VERSION = 1

#: The sender locals a bulk pipeline should never reply to.
NO_REPLY_LOCALS: Tuple[str, ...] = ("noreply", "no-reply", "donotreply", "do-not-reply")

OOO_SUBJECTS: Tuple[str, ...] = (
    "Automatic reply: your message",
    "Out of Office",
    "Automatic reply: away until Monday",
)

_SYNTHETIC_DOMAIN = "example.com"
MAILBOX = "intake@example.com"
DEFAULT_REPLY_ADDRESS = f"no-reply@{_SYNTHETIC_DOMAIN}"

#: The customer whose mailbox autoresponds to the pipeline's acknowledgement.
CUSTOMER = "Sana Malik <sana.malik@example.net>"

_DSN_STATUS = (
    b"Reporting-MTA: dns; mx.example.com\r\n"
    b"Arrival-Date: Mon, 6 Oct 2025 09:14:02 +0000\r\n"
    b"\r\n"
    b"Final-Recipient: rfc822; gone@example.org\r\n"
    b"Action: failed\r\n"
    b"Status: 5.1.1\r\n"
    b"Diagnostic-Code: smtp; 550 5.1.1 <gone@example.org>: user unknown\r\n"
)
_DSN_HEADERS = (
    b"From: intake@example.com\r\n"
    b"To: gone@example.org\r\n"
    b"Subject: Re: your ticket\r\n"
)

#: (kind, subject, body, from_addr, headers, attachments)
MACHINE_MAIL_SHAPES: Tuple[Tuple[str, str, str, str, Tuple[Tuple[str, str], ...], Tuple[Attachment, ...]], ...] = (
    ("auto-submitted-auto-replied", "Re: your refund request",
     "This is an automatic acknowledgement. No action is needed.",
     "robot@example.com", (("Auto-Submitted", "auto-replied"),), ()),
    ("auto-submitted-auto-generated", "Your order has shipped",
     "This message was generated automatically.",
     "robot@example.com", (("Auto-Submitted", "auto-generated"),), ()),
    ("precedence-bulk", "Monthly product bulletin",
     "Here is this month's bulletin. You are receiving it as a list member.",
     "newsletter@example.com", (("Precedence", "bulk"),), ()),
    ("precedence-list", "[users] Re: meeting time",
     "Replying to the list, as the footer asks.",
     "users@example.com", (("Precedence", "list"),), ()),
    ("precedence-junk", "Special offer for you",
     "Unsubscribe at your peril.",
     "deals@example.com", (("Precedence", "junk"),), ()),
    ("out-of-office", OOO_SUBJECTS[0],
     "I am away until Monday and will reply then.",
     "colleague@example.com",
     (("Auto-Submitted", "auto-replied"), ("X-Original-To", MAILBOX)), ()),
    ("out-of-office-alternate", OOO_SUBJECTS[1],
     "On leave. Messages are queued for my return.",
     "colleague@example.com", (("X-Original-To", MAILBOX),), ()),
    ("bounce-dsn", "Undelivered Mail Returned to Sender",
     "This is the mail system at host mx.example.com. Your message could not be "
     "delivered to one or more recipients.",
     "MAILER-DAEMON@example.com",
     (
         ("Content-Type",
          'multipart/report; report-type=delivery-status; boundary="dsn-boundary-1"'),
         ("Return-Path", "<>"),
     ),
     (
         Attachment("details.txt", "message/delivery-status", _DSN_STATUS),
         Attachment("original-headers.txt", "text/rfc822-headers", _DSN_HEADERS),
     )),
    ("noreply-sender", "Your payment was received",
     "We have received your payment. Do not reply to this message.",
     "noreply@billing.example.com", (), ()),
    ("noreply-sender-alternate", "Your invoice is attached",
     "This is a receipt. The sender cannot receive replies.",
     "no-reply@billing.example.com", (), ()),
    ("list-headers", "[users] Re: meeting time",
     "Please stop replying to this list address.",
     "users@example.com",
     (("List-Unsubscribe", "<mailto:unsubscribe@example.com>"),), ()),
    ("list-headers-id", "Weekly digest",
     "The digest, delivered to a list.",
     "digest@example.com", (("List-Id", "<users-digest.example.com>"),), ()),
)

KIND_LOOP_BAIT = "loop-bait"
KIND_PRECURSOR = "ordinary-precursor"

#: Every kind a run sends to be judged. The precursor is not judged.
MACHINE_MAIL_KINDS: Tuple[str, ...] = tuple(
    [shape[0] for shape in MACHINE_MAIL_SHAPES] + [KIND_LOOP_BAIT]
)

#: Enough to send every shape once; the bait is always added.
DEFAULT_COUNT = len(MACHINE_MAIL_SHAPES)

#: The longest the run waits for the pipeline's reply to the ordinary message
#: before sending the bait with a fabricated parent. Capped by --max-wait.
DEFAULT_ACK_WAIT = 15.0


def machine_mail_marker(
    headers: Dict[str, str], subject: str, from_addr: str
) -> Optional[str]:
    """What makes this message machine mail, or None for an ordinary one.

    `Auto-Submitted` first (RFC 3834), then `Precedence`, sender and list
    headers, out-of-office subjects, and the bounce shape. Each test is
    anchored, so ordinary mail that mentions a reply is not caught.
    """
    lowered = {name.lower(): value for name, value in (headers or {}).items()}

    auto_submitted = lowered.get("auto-submitted", "").strip().lower()
    if auto_submitted in ("auto-replied", "auto-generated"):
        return f"Auto-Submitted: {auto_submitted}"

    precedence = lowered.get("precedence", "").strip().lower()
    if precedence in ("bulk", "list", "junk"):
        return f"Precedence: {precedence}"

    local = (from_addr or "").split("<")[-1].strip("> ").split("@", 1)[0].lower()
    if local in NO_REPLY_LOCALS:
        return f"From: {local}@"

    for header in ("list-id", "list-unsubscribe"):
        if header in lowered:
            return f"{header.title()} present"

    lowered_subject = (subject or "").strip().lower()
    for phrase in ("automatic reply", "out of office", "auto-reply"):
        if lowered_subject.startswith(phrase):
            return f"Subject starts {phrase!r}"

    if "return-path" in lowered and not lowered["return-path"].strip("<> "):
        return "Return-Path: <>"
    if "mailer-daemon" in (from_addr or "").lower():
        return "From: MAILER-DAEMON@"
    if "multipart/report" in lowered.get("content-type", "").lower():
        return "Content-Type: multipart/report"

    return None


@dataclass(frozen=True)
class MachineMail:
    """One message this run sends, and the header that marks it."""

    kind: str
    marker: str
    email: InboundEmail
    tag: str
    record_id: str
    is_loop_bait: bool = False

    @property
    def message_id(self) -> str:
        return _bare(self.email.headers.get("Message-ID"))

    def probe(self) -> Probe:
        return probe_for(self.email, self.tag, self.record_id, "")


def _bare(value: Optional[str]) -> str:
    return (value or "").strip().strip("<>").strip()


def _message(
    *,
    subject: str,
    body: str,
    from_addr: str,
    headers: Sequence[Tuple[str, str]],
    to: str = MAILBOX,
    attachments: Tuple[Attachment, ...] = (),
) -> InboundEmail:
    envelope_from = from_addr.split("<")[-1].strip("> ")
    return InboundEmail(
        to=to,
        from_addr=from_addr,
        subject=subject,
        text=body + "\n",
        html=f"<html><body>{body}</body></html>",
        envelope=Envelope(to=(to.split("<")[-1].strip("> "),), from_addr=envelope_from),
        ground_truth=GroundTruth(from_addr=from_addr, subject=subject, body_core=body),
        headers=dict(headers),
        charsets={},
        attachments=attachments,
    )


def _tagged(email: InboundEmail, tag: str) -> InboundEmail:
    return stamp(email, tag, message_id=f"<{build_chain_message_id(tag)}>")


def build_machine_corpus(seed: int, count: int, tag_prefix: str) -> List[MachineMail]:
    """`count` machine-mail messages, cycling through every shape before any
    repeats, each tagged and given a Message-ID. Deterministic in its arguments."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    built: List[MachineMail] = []
    for index in range(count):
        kind, subject, body, from_addr, headers, attachments = MACHINE_MAIL_SHAPES[
            index % len(MACHINE_MAIL_SHAPES)
        ]
        if kind == "out-of-office":
            subject = OOO_SUBJECTS[(seed + index) % len(OOO_SUBJECTS)]
        body = f"{body} (ref {seed}-{index})"
        email = _message(subject=subject, body=body, from_addr=from_addr,
                         headers=headers, attachments=attachments)
        marker = machine_mail_marker(email.headers, email.subject, email.from_addr)
        assert marker is not None, kind
        tag = make_tag(tag_prefix, seed, index)
        built.append(MachineMail(kind=kind, marker=marker, email=_tagged(email, tag),
                                 tag=tag, record_id=f"loop-{seed}-{index:04d}"))
    return built


def build_precursor(seed: int, count: int, tag_prefix: str) -> MachineMail:
    """The ordinary message whose acknowledgement the bait replies to."""
    tag = make_tag(tag_prefix, seed, count)
    email = _message(
        subject="Question about order 2291",
        body="Hello, my order 2291 shows as dispatched but has not arrived. "
             "Could you check where it is?",
        from_addr=CUSTOMER,
        headers=(),
    )
    return MachineMail(kind=KIND_PRECURSOR, marker="ordinary mail, not judged",
                       email=_tagged(email, tag), tag=tag,
                       record_id=f"loop-{seed}-precursor")


def build_loop_bait(
    seed: int, count: int, tag_prefix: str, reply_address: str,
    parent_id: Optional[str],
) -> MachineMail:
    """The customer's autoresponder answering the pipeline's acknowledgement.

    `parent_id` is the pipeline's real outbound Message-ID; None falls back to
    a fabricated id on a reserved domain.
    """
    tag = make_tag(tag_prefix, seed, count + 1)
    parent = parent_id or build_chain_message_id(f"{tag}-unseen-parent")
    email = _message(
        subject="Automatic reply: Re: Question about order 2291",
        body="I am out of the office until Monday and will reply when I return.",
        from_addr=CUSTOMER,
        to=reply_address,
        headers=(("Auto-Submitted", "auto-replied"),
                 ("In-Reply-To", f"<{parent}>"),
                 ("References", f"<{parent}>")),
    )
    return MachineMail(
        kind=KIND_LOOP_BAIT,
        marker="auto-reply to the pipeline's own outbound message",
        email=_tagged(email, tag),
        tag=tag,
        record_id=f"loop-{seed}-bait",
        is_loop_bait=True,
    )


# ---------------------------------------------------------------------------
# The DSN on the wire
# ---------------------------------------------------------------------------

#: Headers the DSN message sets itself rather than copying from the payload.
_DSN_OWN_HEADERS = frozenset({"content-type", "mime-version", "content-transfer-encoding"})


def is_dsn(email: InboundEmail) -> bool:
    content_type = next(
        (v for k, v in email.headers.items() if k.lower() == "content-type"), ""
    )
    return content_type.lower().startswith("multipart/report")


def dsn_bytes(email: InboundEmail, boundary: str) -> bytes:
    """The DSN shape as RFC 3464 has it: multipart/report with report-type
    delivery-status, a human-readable part, a message/delivery-status part and
    the returned headers, from the payload's own attachments."""
    report = MIMEMultipart("report", boundary=boundary, report_type="delivery-status")
    report["To"] = email.to
    report["From"] = email.from_addr
    report["Subject"] = email.subject
    for name, value in email.headers.items():
        if name.lower() not in _DSN_OWN_HEADERS:
            report[name] = value
    report.attach(MIMEText(email.text, "plain", "us-ascii"))
    for attachment in email.attachments:
        part = Message()
        part["Content-Type"] = attachment.content_type
        if attachment.content_type == "message/delivery-status":
            part.set_payload([
                message_from_bytes(block)
                for block in attachment.content.split(b"\r\n\r\n") if block.strip()
            ])
        else:
            part.set_payload(attachment.content.decode("ascii"))
        report.attach(part)
    return report.as_bytes(policy=email_policy.SMTP)


class LoopWireFormat(WireFormat):
    """The target's wire format, except that a DSN sent in a raw-MIME format
    (`mime`, `sendgrid-raw`) is a real multipart/report rather than the
    multipart/mixed the generic encoder builds. Parsed formats carry the DSN
    as a provider would present it: the Content-Type header plus a
    message/delivery-status attachment."""

    def __init__(self, base: WireFormat, boundary: str = DEFAULT_BOUNDARY) -> None:
        self.base = base
        self.name = base.name
        self._boundary = boundary

    def encode(self, payload: InboundEmail) -> EncodedBody:
        if not is_dsn(payload):
            return self.base.encode(payload)
        if isinstance(self.base, MimeFormat):
            return EncodedBody(dsn_bytes(payload, self._boundary), "message/rfc822")
        if isinstance(self.base, SendgridRawFormat):
            inner = "mime-" + hashlib.sha256(self._boundary.encode("utf-8")).hexdigest()[:24]
            raw = dsn_bytes(payload, inner).decode("ascii")
            parts = [
                dataclasses.replace(part, value=raw) if getattr(part, "name", None) == "email"
                else part
                # The generic encoder cannot build the delivery-status part,
                # and every field but `email` ignores attachments.
                for part in self.base.parts(dataclasses.replace(payload, attachments=()))
            ]
            return EncodedBody(
                encode_multipart(parts, self._boundary),
                f"multipart/form-data; boundary={self._boundary}",
            )
        return self.base.encode(payload)


def build_items(messages_: Sequence[MachineMail]) -> List[Tuple[InboundEmail, str, str, Probe]]:
    return [(m.email, m.tag, m.record_id, m.probe()) for m in messages_]


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

TICKET_POLICY_NONE = "none"
TICKET_POLICY_ALLOWED = "allowed"
TICKET_POLICIES = (TICKET_POLICY_NONE, TICKET_POLICY_ALLOWED)

REPLY_YES = "yes"
REPLY_NO = "no"
REPLY_UNATTRIBUTED = "unattributed"
REPLY_NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class LoopResult:
    """What the pipeline did with one machine-generated message."""

    tag: str
    kind: str
    marker: str
    ticked: Optional[bool]
    #: True, False, or None when the reply check could not be answered: no
    #: sink configured, or unattributed outbound seen.
    replied: Optional[bool]
    is_loop_bait: bool
    findings: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    reply_state: str = REPLY_NOT_CHECKED

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_json(self) -> Dict[str, Any]:
        return {
            "tag": self.tag,
            "kind": self.kind,
            "marker": self.marker,
            "ticked": self.ticked,
            "replied": self.replied,
            "reply_state": self.reply_state,
            "is_loop_bait": self.is_loop_bait,
            "findings": list(self.findings),
            "notes": list(self.notes),
        }


def judge_one(
    message: MachineMail,
    readbacks: Sequence[Any],
    outbound: Optional[Sequence[Any]],
    ticket_policy: str = TICKET_POLICY_NONE,
    *,
    unattributed_seen: bool = False,
    threaded_ticket_ids: Sequence[str] = (),
) -> LoopResult:
    """Grade one message.

    `outbound` is None when no sink is configured, which makes the reply check
    not checked rather than passed. `unattributed_seen` means outbound mail
    appeared that could not be tied to any message, so a message with no
    attributed reply reads "unattributed", never "no".
    `threaded_ticket_ids` are tickets the bait may be appended to without that
    counting as a new ticket (the precursor's).
    """
    if ticket_policy not in TICKET_POLICIES:
        raise ValueError(
            f"ticket_policy must be one of {list(TICKET_POLICIES)}, got {ticket_policy!r}"
        )
    findings: List[str] = []
    notes: List[str] = []

    opened = [r for r in readbacks
              if getattr(r, "ticket_id", None) not in set(threaded_ticket_ids)]
    if len(opened) < len(readbacks):
        notes.append("filed on the ordinary message's existing ticket")
    ticked = bool(opened)
    if ticked and ticket_policy == TICKET_POLICY_ALLOWED:
        notes.append("a ticket was opened for machine mail, which --ticket-policy allowed")
    elif ticked:
        findings.append(f"a ticket was opened for machine mail marked {message.marker!r}")

    if outbound is None:
        replied: Optional[bool] = None
        state = REPLY_NOT_CHECKED
        notes.append("no [loop.outbound] sink configured, so auto-replies were not checked")
    elif outbound:
        replied, state = True, REPLY_YES
        if message.is_loop_bait:
            findings.append("the loop bait was answered, which is a live mail loop")
        else:
            findings.append(
                f"an outbound message was emitted for machine mail marked {message.marker!r}"
            )
    elif unattributed_seen:
        replied, state = None, REPLY_UNATTRIBUTED
    else:
        replied, state = False, REPLY_NO

    return LoopResult(
        tag=message.tag, kind=message.kind, marker=message.marker, ticked=ticked,
        replied=replied, is_loop_bait=message.is_loop_bait,
        findings=tuple(findings), notes=tuple(notes), reply_state=state,
    )


def judge(
    messages_: Sequence[MachineMail],
    readbacks: Dict[str, List[Any]],
    outbound: Optional[Dict[str, List[Any]]],
    ticket_policy: str = TICKET_POLICY_NONE,
    *,
    unattributed_seen: bool = False,
    threaded_ticket_ids: Sequence[str] = (),
) -> List[LoopResult]:
    return [
        judge_one(
            message,
            readbacks.get(message.tag, []),
            None if outbound is None else outbound.get(message.tag, []),
            ticket_policy,
            unattributed_seen=unattributed_seen,
            threaded_ticket_ids=threaded_ticket_ids if message.is_loop_bait else (),
        )
        for message in messages_
    ]


# ---------------------------------------------------------------------------
# Outbound attribution
# ---------------------------------------------------------------------------


def _record_key(record: Any) -> str:
    to_json = getattr(record, "to_json", None)
    payload = to_json() if callable(to_json) else repr(record)
    return json.dumps(payload, sort_keys=True, default=str)


def new_records(after: Sequence[Any], before: Sequence[Any]) -> List[Any]:
    """Records in `after` beyond those already in `before`, as a multiset."""
    remaining = Counter(_record_key(r) for r in before)
    out = []
    for record in after:
        key = _record_key(record)
        if remaining[key]:
            remaining[key] -= 1
        else:
            out.append(record)
    return out


def attribute(record: Any, tags: Sequence[str], message_ids: Dict[str, str]) -> Optional[str]:
    """The tag `record` belongs to: its own tag, else the message its
    In-Reply-To or References names. None when it belongs to nothing sent."""
    tag = getattr(record, "tag", None)
    if tag and tag in tags:
        return tag
    for candidate in [getattr(record, "in_reply_to", None), *(getattr(record, "references", ()) or ())]:
        found = message_ids.get(_bare(candidate))
        if found:
            return found
    return None


def merge_outbound(
    by_tag: Dict[str, List[Any]],
    enumerated_new: Sequence[Any],
    tags: Sequence[str],
    message_ids: Dict[str, str],
) -> Tuple[Dict[str, List[Any]], List[Any]]:
    """Per-tag outbound from both the tag lookups and the enumeration, and the
    records nothing claims."""
    merged = {tag: list(by_tag.get(tag, [])) for tag in tags}
    seen = {tag: {_record_key(r) for r in records} for tag, records in merged.items()}
    unattributed = []
    for record in enumerated_new:
        tag = attribute(record, tags, message_ids)
        if tag is None:
            unattributed.append(record)
            continue
        key = _record_key(record)
        if key not in seen[tag]:
            merged[tag].append(record)
            seen[tag].add(key)
    return merged, unattributed


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_SECTION = "loop"


class LoopConfigError(ToolConfigError):
    """A malformed `[loop]` table. A caller error, never a pipeline finding."""


def load_loop_config(path: str, *, required: bool = True) -> Dict[str, Any]:
    """The validated `[loop]` table. With `required=False` a config file that
    does not exist reads as an empty table, which is what a dry run needs."""
    reader = read_tool_table if required else read_tool_table_if_present
    try:
        section = reader(path, CONFIG_SECTION)
    except ToolConfigError as exc:
        raise LoopConfigError(str(exc)) from exc
    reply = section.get("reply_address")
    if reply is not None and not isinstance(reply, str):
        raise LoopConfigError(
            f"[{CONFIG_SECTION}].reply_address must be a string, got {type(reply).__name__}"
        )
    policy = section.get("ticket_policy")
    if policy is not None and policy not in TICKET_POLICIES:
        raise LoopConfigError(
            f"[{CONFIG_SECTION}].ticket_policy must be one of "
            f"{list(TICKET_POLICIES)}, got {policy!r}"
        )
    return section


def effective_settings(
    section: Dict[str, Any], *, flag_policy: Optional[str], flag_reply: Optional[str]
) -> Tuple[str, str]:
    """(ticket_policy, reply_address): flag, then `[loop]`, then the default."""
    policy = flag_policy or section.get("ticket_policy") or TICKET_POLICY_NONE
    reply = flag_reply or section.get("reply_address") or DEFAULT_REPLY_ADDRESS
    return policy, reply


def build_outbound_config(section: Dict[str, Any], base_path: str) -> Optional[ReadbackConfig]:
    """The `[loop.outbound]` table as a readback config, or None when absent.
    A relative mailbox path resolves against the config file's directory."""
    outbound = section.get("outbound")
    if outbound is None:
        return None
    if not isinstance(outbound, dict):
        raise LoopConfigError(
            f"[{CONFIG_SECTION}.outbound] must be a table, got {type(outbound).__name__}"
        )
    config = parse_readback_config(outbound)
    if config.kind == "mailbox" and config.path and not Path(config.path).is_absolute():
        config = dataclasses.replace(
            config, path=str(Path(base_path).resolve().parent / config.path)
        )
    return config


def run_config(
    seed: int,
    count: int,
    tag_prefix: str,
    target_name: Optional[str],
    readback: ReadbackConfig,
    outbound: Optional[ReadbackConfig],
    ticket_policy: str,
    reply_address: str,
    poll: Dict[str, float],
) -> Dict[str, Any]:
    """Everything needed to reproduce the run. No latency, no wall clock."""
    return {
        "tool": "loop",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "outbound": outbound.to_json() if outbound is not None else None,
        "ticket_policy": ticket_policy,
        "reply_address": reply_address,
        "readback_poll": dict(poll),
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(
    results: Sequence[LoopResult],
    sent: Sequence[Any],
    ticket_policy: str,
    *,
    unattributed: Sequence[Any] = (),
    outbound_enumerated: Optional[bool] = None,
    outbound_partial: bool = False,
    bait_parent: Optional[Dict[str, Any]] = None,
    unsettled: bool = False,
    readback_failed: bool = False,
) -> Dict[str, Any]:
    """Counts, and the numbers that decide whether the run proved anything.

    `readback_failed` means every ticket lookup was a 404, so "no ticket was
    opened" is unknown rather than a pass."""
    failing = [r for r in results if not r.passed]
    run_findings: List[str] = []
    if unattributed:
        run_findings.append(
            f"outbound, unattributed: {len(unattributed)} outbound message(s) appeared "
            "that carry no tag of this run and reply to none of its messages. An "
            "auto-reply that quotes nothing looks exactly like this, so the "
            "auto-reply check cannot pass"
        )
    if readback_failed:
        run_findings.append(
            f"{READBACK_FAILED}: every lookup was 404: check the readback url. "
            "Whether a ticket was opened for any message is unknown"
        )
    elif unsettled:
        run_findings.append(
            "UNSETTLED: the readback was still changing at --max-wait, so this run "
            "cannot say what the pipeline finally did"
        )
    summary = {
        "sent": len(sent),
        "checked": len(results),
        "message_findings": len(failing),
        "findings": len(failing) + len(run_findings),
        "run_findings": run_findings,
        "ticket_policy": ticket_policy,
        "tickets_opened": sum(1 for r in results if r.ticked),
        "auto_replies_emitted": sum(1 for r in results if r.replied),
        "auto_reply_checked": sum(1 for r in results if r.reply_state in (REPLY_YES, REPLY_NO)),
        "auto_reply_skipped": sum(1 for r in results if r.reply_state == REPLY_NOT_CHECKED),
        "auto_reply_unattributed": len(unattributed),
        "outbound_enumerated": outbound_enumerated,
        "outbound_partial": outbound_partial,
        "loop_bait_answered": sum(1 for r in results if r.is_loop_bait and r.replied),
        "bait_parent": bait_parent,
        "unsettled": unsettled,
        "readback_failed": readback_failed,
    }
    summary["verdict"] = headline(summary)
    return summary


def headline(summary: Dict[str, Any]) -> str:
    """The verdict and its qualifiers. Recorded in the artifact, so `report`
    reads it rather than deriving it again."""
    if summary.get("aborted"):
        return "ABORTED"
    if summary.get("readback_failed"):
        verdict = READBACK_FAILED
    elif summary.get("unsettled"):
        verdict = "UNSETTLED"
    else:
        verdict = "LOOP-SAFE" if summary.get("findings", 0) == 0 else "LOOPS-DETECTED"
    if summary.get("auto_reply_skipped"):
        verdict = f"{verdict} (auto-reply NOT CHECKED)"
    elif summary.get("outbound_enumerated") is False:
        verdict = f"{verdict} (untagged auto-replies NOT CHECKED)"
    return verdict


def format_results(results: Sequence[LoopResult], summary: Dict[str, Any]) -> str:
    """Tag, the header that marked it, ticket, reply, verdict."""
    lines = [
        f"loop: {headline(summary)}  ({summary['checked']} machine-generated message(s) judged)",
        "",
        f"  sent:                {summary['sent']} (including the ordinary precursor)",
        *([f"  readback:            {summary['readback_headline']}"]
          if summary.get("readback_headline") else []),
        f"  tickets opened:      {summary['tickets_opened']}",
        f"  auto-replies sent:   {summary['auto_replies_emitted']}",
        f"  loop bait answered:  {summary['loop_bait_answered']}",
    ]
    if summary.get("auto_reply_unattributed"):
        lines.append(f"  outbound, unattributed: {summary['auto_reply_unattributed']}")

    parent = summary.get("bait_parent") or {}
    if parent:
        lines.append("")
        if parent.get("source") == "pipeline-outbound":
            lines.append(
                f"  loop bait: In-Reply-To <{parent.get('message_id')}>, the pipeline's "
                "own reply to the ordinary message"
            )
        else:
            why = parent.get("reason") or "no reply to the ordinary message was seen"
            lines.append(f"  loop bait: In-Reply-To is fabricated, because {why}.")
            lines.append("  The bait does not reproduce a live loop.")

    if summary["auto_reply_skipped"]:
        lines.append("")
        lines.append(f"  NOT CHECKED ({summary['auto_reply_skipped']} message(s)):")
        lines.append("    no sink configured: without [loop.outbound] whether an auto-reply")
        lines.append("    was sent is unknown. This run does NOT establish that the pipeline")
        lines.append("    stayed quiet. Add [loop.outbound] to check it.")
    elif summary.get("outbound_enumerated") is False:
        lines.append("")
        lines.append("  the outbound sink cannot be enumerated, so only replies that carry a")
        lines.append("  tag were seen. A reply that quotes nothing would be missed.")
    if summary.get("outbound_partial"):
        lines.append("")
        lines.append("  the outbound enumeration stopped at its page limit, so an untagged")
        lines.append("  reply on a later page would be missed.")

    lines.append("")
    lines.append(f"  {'tag':22} {'marked by':44} {'ticket':7} {'reply':13} verdict")
    for result in results:
        mark = "OK" if result.passed else "FINDING"
        lines.append(
            f"  {result.tag:22} {result.marker[:44]:44} "
            f"{('yes' if result.ticked else 'no'):7} {result.reply_state:13} {mark}"
        )

    findings = [r for r in results if not r.passed]
    if findings or summary.get("run_findings"):
        lines.append("")
        lines.append(f"  findings ({summary['findings']}):")
        for finding in summary.get("run_findings") or []:
            lines.append(f"    run: {finding}")
        for result in findings:
            for finding in result.findings:
                lines.append(f"    {result.tag}: {finding}")
    return "\n".join(lines)


def format_dry_run(
    messages_: Sequence[MachineMail], readback: Optional[ReadbackConfig], ticket_policy: str,
    reply_address: str = DEFAULT_REPLY_ADDRESS,
    outbound: Optional[ReadbackConfig] = None,
) -> str:
    """What a real run would send and read. `readback` None means none is
    configured, which a real run refuses."""
    lines = [
        f"dry-run preview: {len(messages_)} machine-generated message(s) plus one "
        "ordinary precursor",
    ]
    if readback is None:
        lines.append("  readback adapter: none configured (a real run refuses)")
    else:
        lines.append(f"  readback adapter: kind {readback.kind!r}")
        if readback.kind == "http":
            lines.append(f"  readback url:    {readback.url}")
        elif readback.kind == "mailbox":
            lines.append(f"  readback path:   {readback.path}")
    if outbound is None:
        lines.append("  outbound sink:   none configured, so auto-replies will NOT be checked")
    else:
        lines.append(f"  outbound sink:   kind {outbound.kind!r}")
    lines.append(f"  ticket policy:   {ticket_policy}")
    lines.append(f"  reply address:   {reply_address}")
    lines.append("  first an ordinary message, whose reply the loop bait answers; then")
    lines.append("  every message here is one a correct pipeline must not act on:")
    for message in messages_:
        bait = "  [loop bait]" if message.is_loop_bait else ""
        lines.append(f"    {message.tag:22} {message.kind:32}{bait}".rstrip())
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    corpus: Sequence[MachineMail],
    results: Sequence[LoopResult],
    summary: Dict[str, Any],
    *,
    unattributed: Sequence[Any] = (),
    aborted: bool = False,
) -> Dict[str, Any]:
    by_tag = {r.tag: r for r in results}
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "loop",
        "aborted": aborted,
        "seed": seed,
        "config": config,
        "summary": summary,
        "messages": [
            {
                "tag": message.tag,
                "record_id": message.record_id,
                "kind": message.kind,
                "marker": message.marker,
                "subject": message.email.subject,
                "from_addr": message.email.from_addr,
                "message_id": message.message_id,
                "is_loop_bait": message.is_loop_bait,
                "result": by_tag[message.tag].to_json() if message.tag in by_tag else None,
            }
            for message in corpus
        ],
        "outbound_unattributed": [
            r.to_json() if callable(getattr(r, "to_json", None)) else repr(r)
            for r in unattributed[:200]
        ],
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def enumerable(adapter: Any) -> bool:
    """True when `adapter` can list every record it holds. An http adapter
    configured with `enumerate = false` cannot."""
    return adapter is not None and can_enumerate(adapter)


def _list_all(adapter: Any) -> Tuple[List[Any], bool]:
    """(records, partial). `partial` is True when the adapter says pagination
    stopped before the end."""
    result = adapter.list_all()
    return list(result or []), bool(getattr(result, "partial", False))


def _find_reply(
    adapter: Any, precursor: MachineMail, baseline: Sequence[Any], can_list: bool,
    *, sleep, clock, wait: float, poll_interval: float,
) -> Optional[Any]:
    """The pipeline's outbound reply to the precursor, by tag or by
    In-Reply-To, or None once `wait` seconds have passed without one."""
    started = clock()
    probe = precursor.probe()
    message_ids = {precursor.message_id: precursor.tag}
    while True:
        found = list(adapter.fetch(probe) or [])
        if not found and can_list:
            records, _partial = _list_all(adapter)
            found = [
                r for r in new_records(records, baseline)
                if attribute(r, (precursor.tag,), message_ids)
            ]
        if found:
            return found[0]
        if clock() - started >= wait:
            return None
        sleep(poll_interval)


def _write(out: Optional[str], artifact: Dict[str, Any]) -> None:
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: Any,
    outbound: Any,
    tag_prefix: str,
    rate: float,
    ticket_policy: str = TICKET_POLICY_NONE,
    reply_address: str = DEFAULT_REPLY_ADDRESS,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    ack_wait: float = DEFAULT_ACK_WAIT,
    allow_public_target: bool = False,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Send the precursor, read its reply, send the corpus and the bait, read
    both directions back, report. Every refusal lands before the first send.

    `readback` and `outbound` are `ReadbackConfig`s; `outbound` None means no
    sink, so the auto-reply check is not run. Both adapters are built and
    preflighted before the first send.
    """
    if ticket_policy not in TICKET_POLICIES:
        printer(f"refused: --ticket-policy must be one of {list(TICKET_POLICIES)}")
        return EXIT_REFUSED

    try:
        corpus = build_machine_corpus(seed, count, tag_prefix)
        precursor = build_precursor(seed, count, tag_prefix)
        provisional_bait = build_loop_bait(seed, count, tag_prefix, reply_address, None)
        require_synthetic([m.email for m in [*corpus, precursor, provisional_bait]])
        target = resolve_target(
            target_name, config_path, allow_public_target=allow_public_target
        )
    except Exception as exc:  # noqa: BLE001 - every pre-send failure is a refusal
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    config = run_config(
        seed, count, tag_prefix, target_name, readback, outbound, ticket_policy,
        reply_address,
        {"quiet_window": quiet_window, "max_wait": max_wait, "poll_interval": poll_interval},
    )
    config.update(target.artifact_config())
    wire = LoopWireFormat(target.wire_format)

    adapter = outbound_adapter = None
    sent: List[Any] = []
    bait = provisional_bait
    bait_parent: Dict[str, Any] = {
        "source": "fabricated", "message_id": None,
        "reason": (
            "no [loop.outbound] sink is configured" if outbound is None
            else f"no reply to the ordinary message was seen within {min(ack_wait, max_wait):g}s"
        ),
    }
    poll = dict(sleep=sleep, clock=clock, quiet_window=quiet_window,
                max_wait=max_wait, poll_interval=poll_interval, require_all_found=False)
    try:
        try:
            adapter = prepare_readback(readback, client=readback_client, config_path=config_path)
            if outbound is not None:
                outbound_adapter = prepare_readback(
                    outbound, client=readback_client, config_path=config_path
                )
            can_list = enumerable(outbound_adapter)
            baseline = _list_all(outbound_adapter)[0] if can_list else []
        except Exception as exc:  # noqa: BLE001 - nothing has been sent yet
            printer(f"refused: {exc}")
            return EXIT_REFUSED

        sent += send_all(build_items([precursor]), target.url, rate, client=client,
                         sleep=sleep, clock=clock, wire_format=wire)
        if outbound_adapter is not None:
            reply = _find_reply(
                outbound_adapter, precursor, baseline, can_list, sleep=sleep, clock=clock,
                wait=min(ack_wait, max_wait), poll_interval=poll_interval,
            )
            parent_id = _bare(getattr(reply, "message_id", None)) if reply is not None else ""
            if parent_id:
                bait = build_loop_bait(seed, count, tag_prefix, reply_address, parent_id)
                bait_parent = {"source": "pipeline-outbound", "message_id": parent_id,
                               "reason": None}
            if can_list:
                baseline = _list_all(outbound_adapter)[0]
        require_synthetic([bait.email])

        judged = [*corpus, bait]
        sent += send_all(build_items(judged), target.url, rate, client=client,
                         sleep=sleep, clock=clock, wire_format=wire)
        outcome = read_back_all(adapter, [m.probe() for m in [precursor, *judged]], **poll)
        outbound_by_tag = None
        unattributed: List[Any] = []
        partial = False
        unsettled = outcome.unsettled
        if outbound_adapter is not None:
            outbound_outcome = read_back_all(outbound_adapter, [m.probe() for m in judged], **poll)
            unsettled = unsettled or outbound_outcome.unsettled
            tags = [m.tag for m in judged]
            message_ids = {m.message_id: m.tag for m in judged}
            message_ids[precursor.message_id] = precursor.tag
            enumerated: List[Any] = []
            if can_list:
                records, partial = _list_all(outbound_adapter)
                enumerated = new_records(records, baseline)
            merged, unattributed = merge_outbound(
                outbound_outcome.readbacks, enumerated, [*tags, precursor.tag], message_ids
            )
            outbound_by_tag = {tag: merged.get(tag, []) for tag in tags}
    except KeyboardInterrupt:
        summary = {"sent": len(sent), "findings": 0, "checked": 0, "aborted": True}
        summary["verdict"] = headline(summary)
        _write(out, build_artifact(seed, config, [precursor, bait, *corpus], [], summary,
                                   aborted=True))
        printer("loop: ABORTED" + (", partial artifact written" if out else ""))
        return EXIT_MISMATCH
    except Exception as exc:  # noqa: BLE001 - a failed readback after send
        printer(f"loop: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(outbound_adapter)
        close_adapter(adapter)

    threaded = [r.ticket_id for r in outcome.readbacks.get(precursor.tag, [])]
    results = judge(
        judged, outcome.readbacks, outbound_by_tag, ticket_policy,
        unattributed_seen=bool(unattributed), threaded_ticket_ids=threaded,
    )
    summary = summarize(
        results, sent, ticket_policy, unattributed=unattributed,
        outbound_enumerated=None if outbound_adapter is None else can_list,
        outbound_partial=partial, bait_parent=bait_parent, unsettled=unsettled,
        readback_failed=outcome.failed,
    )
    summary["verdict_source"] = (
        VERDICT_SOURCE_READBACK if unsettled or outcome.failed else "tool"
    )
    summary["readback"] = outcome.to_json()
    summary["readback_headline"] = outcome.headline()
    artifact = build_artifact(seed, config, [precursor, *judged], results, summary,
                              unattributed=unattributed)
    printer(format_results(results, summary))
    _write(out, artifact)
    return EXIT_OK if summary["findings"] == 0 else EXIT_MISMATCH
