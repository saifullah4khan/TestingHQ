"""loop: auto-reply and mail-loop detection.

Two auto-responders answering each other create thousands of tickets overnight.
This builds a corpus a correct pipeline must recognise as machine-originated and
must not act on, sends it, and reports what the pipeline did with each message.

It reproduces the single-hop shape of a loop, not a live two-party loop: a real
loop needs two systems replying to each other, and this points one message at
the pipeline's own reply address instead. That is enough to catch a pipeline
that answers its own auto-replies, which is the bug.

`machine_mail_marker` is the single definition of what makes a message machine
mail, and the test double calls it rather than repeating the rules. Two copies
of that predicate would drift, and the drift would surface as a test passing
against a pipeline the tool considers broken.
"""
from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import Envelope, GroundTruth, InboundEmail
from .messages import Probe, build_chain_message_id, make_tag, stamp

#: The sender locals a bulk pipeline should never reply to.
NO_REPLY_LOCALS: Tuple[str, ...] = ("noreply", "no-reply", "donotreply", "do-not-reply")

#: Subjects an out-of-office auto-responder uses. A pipeline keying on the
#: subject instead of on `Auto-Submitted` misses the RFC 3834 route, so a
#: subject-only shape is sent as well.
OOO_SUBJECTS: Tuple[str, ...] = (
    "Automatic reply: your message",
    "Out of Office",
    "Automatic reply: away until Monday",
)

_SYNTHETIC_DOMAIN = "example.com"
MAILBOX = "intake@example.com"
DEFAULT_REPLY_ADDRESS = f"no-reply@{_SYNTHETIC_DOMAIN}"

#: The machine-mail shapes this sends, as data.
#:
#: A table rather than a builder per shape, because a shape is a set of headers
#: and a subject, and a table is auditable in one pass where ten near-identical
#: functions are not. Each entry is:
#:
#:     (kind, subject, body, from_addr, headers)
#:
#: `from_addr` may be a `NO_REPLY_LOCALS` local, in which case the builder
#: picks one deterministically from the seed.
MACHINE_MAIL_SHAPES: Tuple[Tuple[str, str, str, str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "auto-submitted-auto-replied",
        "Re: your refund request",
        "This is an automatic acknowledgement. No action is needed.",
        "robot@example.com",
        (("Auto-Submitted", "auto-replied"),),
    ),
    (
        "auto-submitted-auto-generated",
        "Your order has shipped",
        "This message was generated automatically.",
        "robot@example.com",
        (("Auto-Submitted", "auto-generated"),),
    ),
    (
        "precedence-bulk",
        "Monthly product bulletin",
        "Here is this month's bulletin. You are receiving it as a list member.",
        "newsletter@example.com",
        (("Precedence", "bulk"),),
    ),
    (
        "precedence-list",
        "[users] Re: meeting time",
        "Replying to the list, as the footer asks.",
        "users@example.com",
        (("Precedence", "list"),),
    ),
    (
        "precedence-junk",
        "Special offer for you",
        "Unsubscribe at your peril.",
        "deals@example.com",
        (("Precedence", "junk"),),
    ),
    (
        "out-of-office",
        OOO_SUBJECTS[0],
        "I am away until Monday and will reply then.",
        "colleague@example.com",
        (("Auto-Submitted", "auto-replied"), ("X-Original-To", MAILBOX)),
    ),
    (
        "out-of-office-alternate",
        OOO_SUBJECTS[1],
        "On leave. Messages are queued for my return.",
        "colleague@example.com",
        (("X-Original-To", MAILBOX),),
    ),
    (
        "bounce-dsn",
        "Undelivered Mail Returned to Sender",
        "The recipient bounced. Reporting-MTA: dns; example.com",
        "MAILER-DAEMON@example.com",
        (
            ("Content-Type", "multipart/report; report-type=delivery-status"),
            # The bounce signal, kept separate from the envelope: the guardrail
            # refuses a message with an empty address anywhere, because a field
            # with nothing in it cannot be verified as synthetic, and a real
            # bounce carries the empty Return-Path rather than an empty To.
            ("Return-Path", ""),
            ("Reporting-MTA", "dns; example.com"),
        ),
    ),
    (
        "noreply-sender",
        "Your payment was received",
        "We have received your payment. Do not reply to this message.",
        "noreply@billing.example.com",
        (),
    ),
    (
        "noreply-sender-alternate",
        "Your invoice is attached",
        "This is a receipt. The sender cannot receive replies.",
        "no-reply@billing.example.com",
        (),
    ),
    (
        "list-headers",
        "[users] Re: meeting time",
        "Please stop replying to this list address.",
        "users@example.com",
        (("List-Unsubscribe", "<mailto:unsubscribe@example.com>"),),
    ),
    (
        "list-headers-id",
        "Weekly digest",
        "The digest, delivered to a list.",
        "digest@example.com",
        (("List-Id", "<users-digest.example.com>"),),
    ),
)

#: Every kind this corpus can produce, including the bait built separately.
MACHINE_MAIL_KINDS: Tuple[str, ...] = tuple(
    [shape[0] for shape in MACHINE_MAIL_SHAPES] + ["loop-bait"]
)


def machine_mail_marker(
    headers: Dict[str, str], subject: str, from_addr: str
) -> Optional[str]:
    """What makes this message machine mail, or None if it is an ordinary one.

    Ordered by how load-bearing each signal is: `Auto-Submitted` first, because
    RFC 3834 is the header a correct pipeline is supposed to key on, then the
    `Precedence` values it defines, then the sender and list headers, then the
    subject phrases an out-of-office responder writes, then the bounce shape.

    Each test is anchored rather than a substring, because a substring test
    would treat half a pipeline's human mail as machine-generated and a
    pipeline that did that would be shipped.
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
    """One machine-generated message, and the header that marks it as one."""

    kind: str
    marker: str
    email: InboundEmail
    tag: str
    record_id: str
    is_loop_bait: bool = False

    def probe(self) -> Probe:
        from .messages import probe_for

        return probe_for(self.email, self.tag, self.record_id, "")


def _message(
    *,
    subject: str,
    body: str,
    from_addr: str,
    headers: Sequence[Tuple[str, str]],
    to: str = MAILBOX,
) -> InboundEmail:
    """One well-formed message carrying the given headers.

    Built from scratch rather than from `generate_corpus`, because every field
    here is dictated by the machine-mail shape being simulated. A generated
    complaint with a bounce sender on it would be a different test.
    """
    return InboundEmail(
        to=to,
        from_addr=from_addr,
        subject=subject,
        text=body + "\n",
        html=f"<html><body>{body}</body></html>",
        envelope=Envelope(to=(to,), from_addr=from_addr),
        ground_truth=GroundTruth(from_addr=from_addr, subject=subject, body_core=body),
        headers=dict(headers),
        charsets={},
        attachments=(),
    )


def _marked(email: InboundEmail) -> str:
    """The marker the report shows, taken from the detector itself.

    Computed rather than written out per shape, because a shape's own idea of
    what marks it and the detector's can disagree: an out-of-office reply is
    marked by its subject to the builder and by `Auto-Submitted` to anyone
    reading the message. The report should name the header a real pipeline
    would key on.
    """
    marker = machine_mail_marker(email.headers, email.subject, email.from_addr)
    if marker is None:
        raise ValueError(
            "built a machine-mail message the detector does not recognise: "
            f"kind subject={email.subject!r} from={email.from_addr!r}"
        )
    return marker


def _loop_bait(rng: random.Random, reply_address: str) -> MachineMail:
    """The step-two shape of a live loop: an auto-reply addressed to the
    pipeline's own reply address, carrying the In-Reply-To of a message the
    pipeline itself would have sent.

    The parent id is built with `build_chain_message_id` so it is on a reserved
    domain, the guardrail is satisfied, and it is reproducible from the seed.
    """
    parent_id = build_chain_message_id("loop-bait-parent")
    email = _message(
        subject="Re: we received your ticket",
        body="This is an auto-reply to a reply. Answering it starts a loop.",
        from_addr=reply_address,
        to=reply_address,
        headers=(("Auto-Submitted", "auto-replied"), ("In-Reply-To", f"<{parent_id}>")),
    )
    return MachineMail(
        kind="loop-bait",
        # The address is what makes this one a loop, so the marker says that
        # rather than repeating the header every other shape shares.
        marker="auto-reply addressed to the pipeline's own reply address",
        email=email,
        tag="",
        record_id="",
        is_loop_bait=True,
    )


def build_machine_corpus(
    seed: int,
    count: int,
    tag_prefix: str,
    reply_address: str = DEFAULT_REPLY_ADDRESS,
) -> List[MachineMail]:
    """`count` machine-generated messages, each stamped and tagged.

    Deterministic in (seed, count, tag_prefix, reply_address) and nothing else,
    so a run is reproducible and a replay asks about the same messages.

    The shapes cycle before they repeat and the bait appears on the first and
    every tenth message, so a small `count` still covers distinct header signals
    rather than four copies of the same one. A run that only ever exercised
    `Auto-Submitted` would pass against a pipeline keyed on that header alone.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")

    shapes = MACHINE_MAIL_SHAPES
    built: List[MachineMail] = []
    for index in range(count):
        rng = random.Random(f"testinghq:loop:{seed}:{index}")
        if index == 0 or (index + 1) % 10 == 0:
            message = _loop_bait(rng, reply_address)
        else:
            kind, subject, body, from_addr, headers = shapes[
                (index - 1) % len(shapes)
            ]
            if from_addr in NO_REPLY_LOCALS:
                from_addr = f"{rng.choice(NO_REPLY_LOCALS)}@{_SYNTHETIC_DOMAIN}"
            if kind == "out-of-office":
                subject = rng.choice(OOO_SUBJECTS)
            email = _message(
                subject=subject, body=body, from_addr=from_addr, headers=headers
            )
            message = MachineMail(
                kind=kind, marker=_marked(email), email=email, tag="", record_id=""
            )
        tag = make_tag(tag_prefix, seed, index)
        built.append(
            dataclasses.replace(
                message,
                email=stamp(message.email, tag),
                tag=tag,
                record_id=f"loop-{seed}-{index:04d}",
            )
        )
    return built


def build_items(
    messages_: Sequence[MachineMail],
) -> List[Tuple[InboundEmail, str, str, Probe]]:
    """(email, tag, record_id, probe) tuples for `common.send_all`."""
    return [(m.email, m.tag, m.record_id, m.probe()) for m in messages_]


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

#: Whether opening a ticket for machine mail is a finding.
TICKET_POLICY_NONE = "none"
TICKET_POLICY_ALLOWED = "allowed"
TICKET_POLICIES = (TICKET_POLICY_NONE, TICKET_POLICY_ALLOWED)


@dataclass(frozen=True)
class LoopResult:
    """What the pipeline did with one machine-generated message."""

    tag: str
    kind: str
    marker: str
    ticked: Optional[bool]
    #: None means the outbound sink was not configured, so the check could not
    #: run. Never True or False by default, which is the property that matters.
    replied: Optional[bool]
    is_loop_bait: bool
    findings: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

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
            "is_loop_bait": self.is_loop_bait,
            "findings": list(self.findings),
            "notes": list(self.notes),
        }


def judge_one(
    message: MachineMail,
    readbacks: Sequence[Any],
    outbound: Optional[Sequence[Any]],
    ticket_policy: str = TICKET_POLICY_NONE,
) -> LoopResult:
    """Grade one message against what the pipeline did with it.

    `outbound` is None when no sink is configured, which is the whole reason the
    auto-reply check can report SKIPPED rather than passing: a tool with no way
    to see whether a reply was sent has no evidence that one was not, and
    reporting that as a pass is the failure this tool exists to avoid.
    """
    if ticket_policy not in TICKET_POLICIES:
        raise ValueError(
            f"ticket_policy must be one of {list(TICKET_POLICIES)}, got {ticket_policy!r}"
        )

    findings: List[str] = []
    notes: List[str] = []

    ticked = bool(readbacks)
    if ticked and ticket_policy == TICKET_POLICY_ALLOWED and not message.is_loop_bait:
        notes.append(
            "a ticket was opened for machine mail, which the current "
            "--ticket-policy allowed"
        )
    elif ticked:
        findings.append(
            f"a ticket was opened for machine mail marked {message.marker!r}"
        )

    if outbound is None:
        replied: Optional[bool] = None
        notes.append(
            "no [loop.outbound] sink configured, so whether an auto-reply was "
            "sent could not be checked"
        )
    else:
        replied = bool(outbound)
        if replied:
            findings.append(
                f"an outbound message was emitted for machine mail marked "
                f"{message.marker!r}"
            )

    if message.is_loop_bait and replied:
        # Always a finding, whatever the policy: answering a message addressed to
        # your own reply address is the loop, and no policy makes it acceptable.
        findings.append("the loop bait was answered, which is a live mail loop")

    return LoopResult(
        tag=message.tag,
        kind=message.kind,
        marker=message.marker,
        ticked=ticked,
        replied=replied,
        is_loop_bait=message.is_loop_bait,
        findings=tuple(findings),
        notes=tuple(notes),
    )


def judge(
    messages_: Sequence[MachineMail],
    readbacks: Dict[str, List[Any]],
    outbound: Optional[Dict[str, List[Any]]],
    ticket_policy: str = TICKET_POLICY_NONE,
) -> List[LoopResult]:
    """Grade the whole corpus, in the order it was sent."""
    return [
        judge_one(
            message,
            readbacks.get(message.tag, []),
            None if outbound is None else outbound.get(message.tag, []),
            ticket_policy,
        )
        for message in messages_
    ]
