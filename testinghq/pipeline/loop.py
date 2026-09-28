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
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

from ..blast.payload import Envelope, GroundTruth, InboundEmail
from .adapters import ReadbackConfig, build_adapter, parse_readback_config
from .common import (
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    close_adapter,
    read_back_all,
    require_synthetic,
    resolve_target_url,
    send_all,
)
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
    if ticked and ticket_policy == TICKET_POLICY_ALLOWED:
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
        # Unconditional, and about ANSWERING rather than about ticketing. The
        # brief makes answering the bait a finding under every policy, and makes
        # ticketing follow the policy like any other message. An earlier version
        # also exempted the bait's ticket, which was an invention beyond the
        # brief: `--ticket-policy allowed` means allowed, and a reader who sets
        # it and still gets a ticket finding has no way to act on either answer.
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Prefix for this tool's table. Separate from `[readback]` because the two
#: answer different questions: `[readback]` is what the pipeline produced,
#: `[loop.outbound]` is what the pipeline tried to send.
CONFIG_SECTION = "loop"


class LoopConfigError(ValueError):
    """A malformed `[loop]` table. A caller error, never a pipeline finding."""


def load_loop_config(path: str) -> Dict[str, Any]:
    """The `[loop]` table, read here rather than in `core/config.py`.

    That file is owned by the backlog agent and the brief says not to edit it,
    so this reader is deliberately local and deliberately temporary: when the
    central loader grows a `[loop]` section, this function should be deleted and
    replaced by a call. A second TOML reader in the tree is a real cost and it
    is only worth paying for as long as the file is owned elsewhere.
    """
    try:
        if tomllib is None:  # pragma: no cover - only on a build with no TOML
            raise LoopConfigError(
                "no TOML reader is available: this interpreter has neither "
                "tomllib (3.11+) nor tomli installed"
            )
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise LoopConfigError(f"no such config file: {path!r}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LoopConfigError(f"{path} is not valid TOML: {exc}") from exc

    section = raw.get(CONFIG_SECTION)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise LoopConfigError(
            f"[{CONFIG_SECTION}] must be a table, got {type(section).__name__}"
        )
    reply = section.get("reply_address")
    if reply is not None and not isinstance(reply, str):
        # Checked here rather than left to the guardrail. An integer reply
        # address produces "no addresses could be extracted", which points at the
        # content rather than at the config key that is wrong, and that is a
        # much worse thing to hand someone.
        raise LoopConfigError(
            f"[{CONFIG_SECTION}].reply_address must be a string, got "
            f"{type(reply).__name__}"
        )
    policy = section.get("ticket_policy")
    if policy is not None and policy not in TICKET_POLICIES:
        raise LoopConfigError(
            f"[{CONFIG_SECTION}].ticket_policy must be one of "
            f"{list(TICKET_POLICIES)}, got {policy!r}"
        )
    return section


def build_outbound_config(section: Dict[str, Any], base_path: str) -> Optional[ReadbackConfig]:
    """The `[loop.outbound]` table as a readback config, or None if absent.

    Takes the same keys as `[readback]`, so an operator points it at their
    outbound sink with the configuration they already know rather than a second
    dialect. A relative `path` resolves against the config file's directory,
    which is the same rule the main readback config follows.

    None, not an empty config, is the absent case, and the difference decides
    whether the auto-reply check is SKIPPED or genuinely answered.
    """
    outbound = section.get("outbound")
    if outbound is None:
        return None
    if not isinstance(outbound, dict):
        raise LoopConfigError(
            f"[{CONFIG_SECTION}.outbound] must be a table, got "
            f"{type(outbound).__name__}"
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
    """Everything needed to reproduce the run, and nothing that varies between
    two runs of the same command. No latency, no wall clock."""
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
    results: Sequence[LoopResult], sent: Sequence[Any], ticket_policy: str
) -> Dict[str, Any]:
    """Counts, and the two numbers that decide whether a run proved anything.

    `auto_reply_checked` is the important one. A run with no `[loop.outbound]`
    sink has not established that the pipeline stayed quiet, only that nobody was
    listening, and a report that reads the same as a clean run is the false
    green this package keeps refusing to ship.
    """
    ticked = [r for r in results if r.ticked]
    replied = [r for r in results if r.replied]
    failing = [r for r in results if not r.passed]
    checked = [r for r in results if r.replied is not None]
    return {
        "sent": len(sent),
        "checked": len(results),
        "findings": len(failing),
        "ticket_policy": ticket_policy,
        "tickets_opened": len(ticked),
        "auto_replies_emitted": len(replied),
        "auto_reply_checked": len(checked),
        "auto_reply_skipped": len(results) - len(checked),
        "loop_bait_answered": sum(
            1 for r in results if r.is_loop_bait and r.replied
        ),
    }


def format_results(results: Sequence[LoopResult], summary: Dict[str, Any]) -> str:
    """The per-message table the spec asks for: tag, the header that marked it
    as machine mail, ticket yes or no, reply yes, no, or not checked."""
    verdict = "LOOP-SAFE" if summary["findings"] == 0 else "LOOPS-DETECTED"
    if summary["auto_reply_skipped"]:
        # In the headline, not only in a block further down. A reader who sees
        # one line has to know that the most important check did not run, because
        # a run that skipped it and a run that passed it look identical on the
        # single line that most reports get reduced to.
        verdict = f"{verdict} (auto-reply NOT CHECKED)"
    lines = [
        f"loop: {verdict}  ({summary['checked']} machine-generated message(s) sent)",
        "",
        f"  sent:                {summary['sent']}",
        f"  tickets opened:      {summary['tickets_opened']}",
        f"  auto-replies sent:   {summary['auto_replies_emitted']}",
        f"  loop bait answered:  {summary['loop_bait_answered']}",
    ]

    if summary["auto_reply_skipped"]:
        lines.append("")
        lines.append(
            f"  NOT CHECKED, by why ({summary['auto_reply_skipped']} message):"
        )
        lines.append(
            "    adapter cannot see field: no [loop.outbound] sink is configured, so "
            "whether an"
        )
        lines.append(
            "    auto-reply was sent is unknown. This run does NOT establish that "
            "the pipeline"
        )
        lines.append("    stayed quiet. Add [loop.outbound] to check it.")

    lines.append("")
    lines.append(
        f"  {'tag':18} {'marked by':44} {'ticket':7} {'reply':12} verdict"
    )
    for result in results:
        reply = (
            "yes" if result.replied else "no" if result.replied is False else "not checked"
        )
        mark = "OK" if result.passed else "FINDING"
        lines.append(
            f"  {result.tag:18} {result.marker[:44]:44} "
            f"{('yes' if result.ticked else 'no'):7} {reply:12} {mark}"
        )

    findings = [r for r in results if not r.passed]
    if findings:
        lines.append("")
        lines.append(f"  findings ({len(findings)}):")
        for result in findings:
            for finding in result.findings:
                lines.append(f"    {result.tag}: {finding}")
    return "\n".join(lines)


def format_dry_run(
    messages_: Sequence[MachineMail], readback: ReadbackConfig, ticket_policy: str
) -> str:
    lines = [
        f"dry-run preview: {len(messages_)} machine-generated message(s), "
        f"tag prefix shown per row",
        f"  readback adapter: kind {readback.kind!r}",
        f"  ticket policy:   {ticket_policy}",
    ]
    if readback.kind == "http":
        lines.append(f"  readback url:    {readback.url}")
    elif readback.kind == "mailbox":
        lines.append(f"  readback path:   {readback.path}")
    lines.append("  every message here is one a correct pipeline must not act on:")
    for message in messages_:
        bait = "  [loop bait]" if message.is_loop_bait else ""
        lines.append(f"    {message.tag:18} {message.kind:32}{bait}")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    corpus: Sequence[MachineMail],
    results: Sequence[LoopResult],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """The run artifact: seed, config with header names only, and every message
    with every finding.

    Each message carries the payload fields a reader needs to recognise which
    message this was, next to the verdict, so an artifact is enough to act on
    without re-running anything.
    """
    return {
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
                "is_loop_bait": message.is_loop_bait,
                "result": result.to_json(),
            }
            for message, result in zip(corpus, results)
        ],
    }


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: ReadbackConfig,
    outbound: Optional[ReadbackConfig],
    tag_prefix: str,
    rate: float,
    ticket_policy: str = TICKET_POLICY_NONE,
    reply_address: str = DEFAULT_REPLY_ADDRESS,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Send the machine-mail corpus, read both directions back, report.

    Never called unless `guardrails.evaluate_send` already said yes. The
    guardrails run over the whole corpus before the first request, so a refusal
    lands before the operator's endpoint has taken any of it.
    """
    if ticket_policy not in TICKET_POLICIES:
        printer(f"refused: --ticket-policy must be one of {list(TICKET_POLICIES)}")
        return EXIT_REFUSED

    corpus = build_machine_corpus(seed, count, tag_prefix, reply_address)
    items = build_items(corpus)

    try:
        require_synthetic([m.email for m in corpus])
        url = resolve_target_url(target_name, config_path)
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    adapter = outbound_adapter = None
    try:
        adapter = build_adapter(readback, client=readback_client)
        if outbound is not None:
            outbound_adapter = build_adapter(outbound, client=readback_client)
        sent = send_all(items, url, rate, client=client, sleep=sleep, clock=clock)
        # `require_all_found=False`, and this is the whole reason. Under the
        # default ticket policy a CORRECT pipeline files no ticket for machine
        # mail and sends no auto-reply, so "every tag found" is a condition this
        # run can never satisfy. Left on, the readback polls until `max_wait`
        # in both directions and the report opens with a readback that gave up,
        # which says nothing about whether the pipeline auto-replied. The demo's
        # two `loop` runs took 121 and 62 seconds that way and reported GAVE UP
        # about runs that were in fact clean.
        poll = dict(
            sleep=sleep,
            clock=clock,
            quiet_window=quiet_window,
            max_wait=max_wait,
            poll_interval=poll_interval,
            require_all_found=False,
        )
        outcome = read_back_all(
            adapter, [probe for _e, _t, _r, probe in items], **poll
        )
        # The outbound sink is polled on the same schedule. It is a second
        # adapter rather than a second mechanism on purpose: the poll, the
        # late-duplicate behaviour and the SKIPPED reporting are the same code
        # in both directions, so a fix to one is a fix to both.
        outbound_outcome = None
        if outbound_adapter is not None:
            outbound_outcome = read_back_all(
                outbound_adapter, [probe for _e, _t, _r, probe in items], **poll
            )
    except Exception as exc:
        printer(f"loop: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(outbound_adapter)
        close_adapter(adapter)

    results = judge(
        corpus,
        outcome.readbacks,
        None if outbound_outcome is None else outbound_outcome.readbacks,
        ticket_policy,
    )
    summary = summarize(results, sent, ticket_policy)
    config = run_config(
        seed, count, tag_prefix, target_name, readback, outbound, ticket_policy,
        reply_address,
        {"quiet_window": quiet_window, "max_wait": max_wait, "poll_interval": poll_interval},
    )
    summary["readback"] = outcome.to_json()

    artifact = build_artifact(seed, config, corpus, results, summary)
    printer(format_results(results, summary))
    if out:
        Path(out).write_text(
            json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8"
        )
    return EXIT_OK if summary["findings"] == 0 else EXIT_MISMATCH
