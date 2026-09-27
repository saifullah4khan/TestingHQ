"""ledger: exactly-once accounting for N uniquely tagged messages.

THE QUESTION. "Did we lose any customer emails during the spike?" Every intake
owner asks it and, as of this tool, nobody has anything that answers it. A run
artifact says the endpoint returned 200 forty times. It does not say whether
forty tickets were created, thirty-five, or eighty. Status codes are the thing
this whole package exists to stop trusting, and exactly-once delivery is the
failure they are worst at hiding: a queue that redelivers produces a duplicate
ticket and still answers 200, and a pipeline that drops a message under load
usually answers 200 as well.

WHAT IT DOES. Sends N messages, each carrying a tag no other run could have
produced, then asks the system what it holds for each one and reconciles the
two lists. The output is an accounting, not a score:

  missing     sent, and the system has nothing at all. A lost email.
  duplicated  the system holds more than one record for one message. Two
              tickets for one customer's email, which is what an intake owner
              actually feels.
  extra       the system holds records this run never sent. A stray ticket, a
              replay from something else, a message nobody tagged.
  wrong       exactly one record, but its content does not match what was
              sent. Counted separately from the above on purpose: "one ticket,
              and it is the right ticket with a mangled sender" is a different
              bug from "no ticket", and lumping them together would tell an
              operator to go and look in the wrong place.
  exactly_once  the one line that matters: messages accounted for, once each,
              with correct content.

WHY THE TAG IS THE WHOLE DESIGN. You cannot detect a duplicate by counting
what you sent, and you cannot detect a loss by counting responses. Both need
per-message identity that survives into the system under test, which is what
`pipeline/messages.py` stamps on. The tag is derived from (prefix, seed, index)
and nothing else, so two runs of the same command ask about the same messages
and a `--tag-prefix` keeps two concurrent runs from reading each other's
records, which is the failure that makes a ledger untrustworthy rather than
merely wrong.

STRAYS, AND WHEN THEY CANNOT BE LOOKED FOR. Finding a ticket that this run
never sent means enumerating everything the system holds, which not every
system can do through a lookup interface. When the adapter cannot enumerate,
`strays_searched` is null and the report says so in words. It is not zero.
A ledger that reported "extra: 0" for a question it never asked would be
worse than one that reports nothing, and the null is what tells the two apart.

RUNNING IT DURING A BARRAGE SPIKE, OR WHILE A DEPENDENCY IS DOWN, IS THE POINT.
Both are just conditions the pipeline is in while these N messages go out, and
neither needs anything special from this tool. What does need care is the tag
prefix: a barrage run cycles a small pool of payloads by design, so pointing a
ledger at a system that is simultaneously under barrage load is only meaningful
if the ledger's tags cannot collide with the pool's. They cannot, because
barrage payloads are not stamped at all.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import messages
from .adapters import ReadbackConfig, build_adapter
from .common import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    SentMessage,
    close_adapter,
    read_back_all,
    require_synthetic,
    resolve_target_url,
    send_all,
)
from .expectations import Expectations, evaluate_sequence
from .readback import Readback, can_enumerate
from .verify import DEFAULT_RATE, build_items, build_tagged_corpus

DEFAULT_COUNT = 50

#: Cap on listed details per section. The counts are always exact; only the
#: lists truncate, and each says so. Mirrors compare/runs.py and verify.py.
MAX_LISTED = 20


# ---------------------------------------------------------------------------
# The accounting
# ---------------------------------------------------------------------------


def account(
    sent: Sequence[SentMessage],
    readbacks: Dict[str, List[Readback]],
    strays: Optional[Sequence[Readback]],
) -> Dict[str, Any]:
    """Reconcile what was sent against what the system holds.

    Pure: takes the two lists, returns the accounting, and decides nothing about
    how either was obtained. That is what makes it testable without a socket and
    what makes the same function usable by a caller that gathered the
    readbacks some other way.

    A message with exactly one record that is also content-correct counts as
    `exactly_once`. A message with one record whose content is wrong counts as
    `wrong` and NOT as `exactly_once`, because "exactly once" that is wrong is
    not the property anyone means by it.
    """
    sent_tags = [message.tag for message in sent]

    missing: List[str] = []
    duplicated: List[Dict[str, Any]] = []
    wrong: List[Dict[str, Any]] = []
    exactly_once: List[str] = []
    produced = 0

    for message in sent:
        found = readbacks.get(message.tag, [])
        produced += len(found)
        if not found:
            missing.append(message.tag)
            continue
        if len(found) > 1:
            duplicated.append(
                {
                    "tag": message.tag,
                    "count": len(found),
                    "tickets": [str(r.ticket_id) for r in found],
                }
            )
            continue

        expectations = Expectations.from_email(message.email)
        verification = evaluate_sequence(
            message.email, found, expectations, record_id=message.record_id, tag=message.tag
        )
        if verification.passed:
            exactly_once.append(message.tag)
        else:
            wrong.append(
                {
                    "tag": message.tag,
                    "record_id": message.record_id,
                    "mismatches": verification.mismatch_strings(),
                }
            )

    extra: Optional[List[Dict[str, Any]]] = None
    if strays is not None:
        sent_set = set(sent_tags)
        extra = [
            {"tag": r.tag, "ticket": str(r.ticket_id)}
            for r in strays
            if not r.tag or r.tag not in sent_set
        ]

    return {
        "sent": len(sent_tags),
        "produced": produced,
        "exactly_once": len(exactly_once),
        "missing": missing,
        "duplicated": duplicated,
        "wrong": wrong,
        "extra": extra,
        "strays_searched": strays is not None,
        # Strays are part of the balance, and an unsearched stray hunt is not
        # a pass. The rule is deliberately the strict one: a ledger that exits
        # 0 while knowing it could not check for tickets it never sent is a
        # footgun in exactly the pipeline it was bought for, and the cost of
        # the strict rule is only that a lookup-only adapter cannot be used as
        # a CI gate until someone gives it a way to enumerate. The report says
        # so in words either way; this makes the exit code agree with the words.
        "balanced": (
            not missing
            and not duplicated
            and not wrong
            and strays is not None
            and not extra
        ),
    }


def verdict(accounting: Dict[str, Any]) -> str:
    """The one word an operator reads first.

    `UNACCOUNTED` when the system holds a number of records that does not match
    what was sent, which is the headline. `MISPARSED` when the count adds up
    but the content does not, which is a different investigation. `BALANCED`
    otherwise, and only when strays were actually searched for: a verdict of
    BALANCED from an adapter that could not enumerate would be claiming an
    answer nobody obtained.
    """
    if accounting["missing"] or accounting["duplicated"] or accounting["extra"]:
        return "UNACCOUNTED"
    if accounting["wrong"]:
        return "MISPARSED"
    if not accounting["strays_searched"]:
        return "UNVERIFIED STRAYS"
    return "BALANCED"


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def run_config(
    seed: int,
    count: int,
    tag_prefix: str,
    target_name: Optional[str],
    readback: ReadbackConfig,
    route: Optional[str],
    settle: float,
) -> Dict[str, Any]:
    return {
        "tool": "ledger",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "expect_route": route,
        "settle": settle,
        "dry_run": False,
    }


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    sent: Sequence[SentMessage],
    accounting: Dict[str, Any],
    readbacks: Dict[str, List[Readback]],
) -> Dict[str, Any]:
    records = []
    for message in sent:
        found = readbacks.get(message.tag, [])
        records.append(
            {
                "id": message.record_id,
                "category": "clean",
                "tag": message.tag,
                "message_id": message.probe.message_id,
                "payload_sha256": message.probe.payload_sha256,
                "intended": Expectations.from_email(message.email).to_json(),
                "response": message.response_json(),
                "tickets": [str(r.ticket_id) for r in found],
                "ticket_count": len(found),
            }
        )

    return {
        "seed": seed,
        "config": config,
        "summary": dict(accounting, verdict=verdict(accounting)),
        "records": records,
    }


def format_ledger(artifact: Dict[str, Any]) -> str:
    """Render the accounting for a terminal.

    Every count is exact and every section is always printed, including the
    empty ones. A report that only lists problems cannot be distinguished from
    a report that found none, and the question this tool exists to answer is
    as often "confirm we lost nothing" as it is "tell me what we lost".
    """
    summary = artifact.get("summary") or {}
    config = artifact.get("config") or {}
    sent = summary.get("sent", 0)
    produced = summary.get("produced", 0)

    lines = [
        f"ledger: {summary.get('verdict', 'UNKNOWN')}  ({sent} message(s) sent, "
        f"seed={artifact.get('seed')}, tag prefix {config.get('tag_prefix')!r})"
    ]
    lines.append("")
    lines.append(f"  sent:            {sent}")
    lines.append(f"  produced:        {produced}")
    lines.append(f"  exactly once:    {summary.get('exactly_once', 0)}")
    lines.append(f"  missing:         {len(summary.get('missing') or [])}")
    lines.append(f"  duplicated:      {len(summary.get('duplicated') or [])}")
    lines.append(f"  misparsed:       {len(summary.get('wrong') or [])}")
    extra = summary.get("extra")
    if summary.get("strays_searched"):
        lines.append(f"  extra:           {len(extra or [])}")
    else:
        lines.append(
            "  extra:           NOT SEARCHED (this adapter cannot enumerate its "
            "records, so a ticket this run never sent could have been missed)"
        )

    def _section(title: str, items: Sequence[Any], render) -> None:
        lines.append("")
        if not items:
            lines.append(f"  {title}: none")
            return
        lines.append(f"  {title} ({len(items)}):")
        for item in list(items)[:MAX_LISTED]:
            lines.append(f"    {render(item)}")
        if len(items) > MAX_LISTED:
            lines.append(f"    ... and {len(items) - MAX_LISTED} more, not shown")

    _section("missing", summary.get("missing") or [], lambda t: f"{t}: sent, and the system holds nothing")
    _section(
        "duplicated",
        summary.get("duplicated") or [],
        lambda d: f"{d['tag']}: {d['count']} tickets {d['tickets']}",
    )
    _section(
        "misparsed",
        summary.get("wrong") or [],
        lambda w: f"{w['tag']}: " + "; ".join(w["mismatches"]),
    )
    if extra:
        _section(
            "extra",
            extra,
            lambda e: f"ticket {e['ticket']} (tag {e['tag'] or 'none'!r}) was never sent by this run",
        )

    if summary.get("balanced"):
        lines.append("")
        lines.append(
            "  every message this run sent was accounted for exactly once, with "
            "correct content, and nothing else was found"
        )
    elif not summary.get("strays_searched") and summary.get("verdict") == "UNVERIFIED STRAYS":
        lines.append("")
        lines.append(
            "  every message this run sent was accounted for exactly once, with "
            "correct content, among the records this adapter can see. Strays "
            "were not searched, so this run does not count as balanced"
        )

    return "\n".join(lines)


def format_dry_run(
    count: int, seed: int, tag_prefix: str, readback: ReadbackConfig
) -> str:
    lines = [
        f"dry-run preview: {count} uniquely tagged message(s), "
        f"tag prefix {tag_prefix!r}, seed {seed}"
    ]
    first = messages.make_tag(tag_prefix, seed, 0) if count else None
    last = messages.make_tag(tag_prefix, seed, count - 1) if count else None
    lines.append(f"  tag range:        {first} .. {last}" if first else "  tag range:        (none)")
    lines.append(f"  readback adapter: kind {readback.kind!r}")
    if readback.url:
        lines.append(f"  readback url:     {readback.url}")
    if readback.path:
        lines.append(f"  readback path:    {readback.path}")
    if not can_enumerate_from(readback):
        lines.append(
            "  note: this adapter will report 'extra' as NOT SEARCHED unless it "
            "can enumerate its records"
        )
    lines.append("  point this at a system under barrage load, or with a "
                 "dependency failing, to answer")
    lines.append("  'did we lose anything?'")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def can_enumerate_from(readback: ReadbackConfig) -> bool:
    """Whether this kind of adapter can look for records it was not asked
    about. Used only for the dry-run note, which is why it works from the config
    rather than from a built adapter: a dry run must not construct an adapter,
    because constructing one may connect to something."""
    return readback.kind == "mailbox" or bool(readback.spec)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: ReadbackConfig,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    rate: float = DEFAULT_RATE,
    route: Optional[str] = None,
    settle: float = 0.0,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Send N tagged messages, reconcile, report. Never called unless
    `guardrails.evaluate_send` already said yes.

    The exit code is the accounting: 0 when every message was accounted for
    exactly once with correct content, 3 when one was not. Not opt-in behind a
    flag, because the question this tool answers is binary and a ledger that
    exits 0 having found a lost email is a footgun in exactly the pipeline it
    was bought for. 1 is a refusal, 2 is a dry run.
    """
    corpus = build_tagged_corpus(seed, count, tag_prefix)
    items = build_items(corpus)

    try:
        require_synthetic([email for email, _tag, _rid in corpus])
        url = resolve_target_url(target_name, config_path)
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    adapter = None
    try:
        # Inside the try, not before it: an adapter that cannot be built is as
        # much a refusal as a target that cannot be resolved, and an
        # unimportable spec left out here escaped as a traceback.
        adapter = build_adapter(readback, client=readback_client)
        sent = send_all(items, url, rate, client=client, sleep=sleep, clock=clock)
        readbacks = read_back_all(
            adapter, [probe for _e, _t, _r, probe in items], sleep=sleep, settle=settle
        )
        strays: Optional[List[Readback]] = None
        if can_enumerate(adapter):
            strays = list(adapter.list_all())
    except Exception as exc:
        printer(f"ledger: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    accounting = account(sent, readbacks, strays)
    artifact = build_artifact(
        seed, run_config(seed, count, tag_prefix, target_name, readback, route, settle), sent, accounting, readbacks
    )
    printer(format_ledger(artifact))
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")
    return EXIT_OK if accounting["balanced"] else EXIT_MISMATCH
