"""ledger: exactly-once accounting for N uniquely tagged messages.

The question: did we lose any customer emails? Every intake owner asks it and
as of this tool nothing answers it. A run artifact says the endpoint returned
200 forty times. It does not say whether forty tickets were created, thirty-five,
or eighty. Status codes are the thing this package exists to stop trusting, and
exactly-once is what they are worst at hiding: a queue that redelivers answers
200, and a pipeline that drops a message under load answers 200 too.

You cannot detect a duplicate by counting what you sent, and you cannot detect a
loss by counting responses. Both need per-message identity that survives into
the system under test, which is what `pipeline/messages.py` stamps on: derived
from (prefix, seed, index) and nothing else, so a replay asks about the same
messages, and a `--tag-prefix` keeps two concurrent runs from reading each
other's records.

Four failure classes, kept separate because they are four bugs in four
places:

  missing     sent, and the system holds nothing. A lost email.
  duplicated  more than one record for one message. Two tickets for one
              customer's email, which is what an intake owner actually feels.
  wrong       exactly one record, and its content does not match what was sent.
              A ticket that is the right ticket with a mangled sender is a
              different bug from no ticket, and lumping them together sends the
              reader somewhere else entirely.
  extra       a record carrying a tag of this run's prefix family that this
              run did not send.

`extra` is scoped. A record with no TestingHQ tag is not a stray: it is the
store's own traffic. A record with a TestingHQ tag from another prefix is
listed as "other runs", for information, and does not unbalance the ledger.

`extra` needs the system enumerated, which not every integration can do. When it
cannot (no `list_all`, `can_enumerate()` False, `[readback].enumerate = false`,
or the enumeration itself failed), `strays_searched` is false, `extra` is null
and the verdict is `UNVERIFIED STRAYS`: the run is reported, never refused
after sending.

Point this at a system under Barrage load, or with a dependency failing. Both
are just conditions the pipeline is in while these N messages go out. What needs
care is the tag: Barrage cycles a small pool of unstamped payloads, so a
ledger's tags cannot collide with them.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ..core.targets import resolve_target
from . import messages
from .adapters import ReadbackConfig, build_adapter
from .common import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    SentMessage,
    close_adapter,
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    ReadbackOutcome,
    read_back_all,
    require_synthetic,
)
from .expectations import (
    DEFAULT_ATTACHMENT_MATCH,
    DEFAULT_SUBJECT_MATCH,
    Expectations,
    evaluate_sequence,
    match_options,
    validate_match_modes,
)
from .readback import Readback, can_enumerate
from .verify import (
    DEFAULT_RATE,
    abort_run,
    artifact_header,
    build_items,
    build_tagged_corpus,
    open_adapter,
    readback_summary,
    readback_unsettled,
    send_messages,
    write_artifact,
)

DEFAULT_COUNT = 50

#: Cap on listed details per section. The counts are always exact; only the
#: lists truncate, and each says so. Mirrors compare/runs.py and verify.py.
MAX_LISTED = 20

#: Cap on the `extra` and `other_runs` lists in the artifact. The counts beside
#: them are exact.
MAX_ARTIFACT_STRAYS = 200

#: The shape of a tag `messages.make_tag` produces: `<prefix>-<seed>-<index>`.
_TAG_SHAPE = re.compile(r"^\S+-(-?\d+)-\d{4,}$")

#: What follows the prefix in such a tag: `<seed>-<index>`.
_TAG_TAIL = re.compile(r"-?\d+-\d{4,}")


# ---------------------------------------------------------------------------
# Strays
# ---------------------------------------------------------------------------


def stray_tag(record: Readback) -> Tuple[Optional[str], bool]:
    """(tag, is_testinghq) for an enumerated record.

    The adapter's `tag` field is used when it has the shape TestingHQ stamps.
    Otherwise the tag is recovered from the stored body or subject, which a
    store that does not expose a tag field still carries. A tag field holding
    something else (a helpdesk's own label) is not a TestingHQ tag.
    """
    tag = record.tag.strip() if isinstance(record.tag, str) else ""
    if tag and _TAG_SHAPE.match(tag):
        return tag, True
    harvested = messages.harvest_tag(record.body, record.subject)
    if harvested:
        return harvested, True
    return (tag or None), False


def _prefixes(family: Union[str, Sequence[str]]) -> Tuple[str, ...]:
    if isinstance(family, str):
        return (family,) if family else ()
    return tuple(p for p in family if p)


def minted_by(tag: str, family: Union[str, Sequence[str]]) -> Optional[str]:
    """The prefix in `family` that minted `tag`, or None.

    Exact: the tag must be `<prefix>-<seed>-<index>` for one of the prefixes,
    so a run with prefix `hq` does not claim `hq-2610141200-ab12-0-0001`, which
    another run minted under its own per-run prefix. The longest prefix wins.
    """
    for prefix in sorted(_prefixes(family), key=len, reverse=True):
        head = f"{prefix}-"
        if tag.startswith(head) and _TAG_TAIL.fullmatch(tag[len(head):]):
            return prefix
    return None


def in_family(tag: str, family: Union[str, Sequence[str]]) -> bool:
    """True when `tag` was minted under this run's prefix (or one of them),
    by this run or by an earlier run that reused the same prefix."""
    return minted_by(tag, family) is not None


def classify_strays(
    strays: Sequence[Readback],
    sent_tags: Sequence[str],
    own_tickets: Sequence[str],
    family: Union[str, Sequence[str]],
) -> Dict[str, Any]:
    """Sort enumerated records into this run's, extra, other runs' and untagged.

    `family` is the run's tag prefix as recorded in its artifact (redeliver
    passes its scenario prefixes too). A record is this run's when its tag was
    sent or its ticket id is one the per-tag lookups already found, which
    covers a store that returns no tag field at all. `extra` is a TestingHQ
    tag minted under `family` that this run did not send; `other_runs` is any
    other TestingHQ tag; a record with no TestingHQ tag is only counted.
    """
    sent = set(sent_tags)
    own = {str(t) for t in own_tickets}
    extra: List[Dict[str, Any]] = []
    other_runs: List[Dict[str, Any]] = []
    untagged = 0
    for record in strays:
        tag, is_ours = stray_tag(record)
        if (tag and tag in sent) or str(record.ticket_id) in own:
            continue
        if not is_ours or not tag:
            untagged += 1
            continue
        entry = {"tag": tag, "ticket": str(record.ticket_id)}
        if in_family(tag, family):
            extra.append(entry)
        else:
            other_runs.append(entry)
    return {"extra": extra, "other_runs": other_runs, "untagged": untagged}


# ---------------------------------------------------------------------------
# The accounting
# ---------------------------------------------------------------------------


def account(
    sent: Sequence[SentMessage],
    readbacks: Dict[str, List[Readback]],
    strays: Optional[Sequence[Readback]],
    *,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    route: Optional[str] = None,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    strays_reason: Optional[str] = None,
    strays_partial: bool = False,
    unsettled: bool = False,
) -> Dict[str, Any]:
    """Reconcile what was sent against what the system holds.

    Pure: takes the two lists, returns the accounting, and decides nothing about
    how either was obtained. That is what makes it testable without a socket and
    what makes the same function usable by a caller that gathered the
    readbacks some other way.

    A message with exactly one record that is also content-correct counts as
    `exactly_once`. A message with one record whose content is wrong counts as
    `wrong` and NOT as `exactly_once`, because "exactly once" that is wrong is
    not the property anyone means by it. Content is graded with the run's
    match modes and `--expect-route`.

    `strays` is None when the store could not be enumerated, and
    `strays_partial` is True when the enumeration stopped short (pagination
    truncated); `strays_reason` says why in either case. A partial search still
    reports the extras it found, but is not a searched store. `tag_prefix` is
    the prefix this run stamped, as recorded in its artifact. The `extra` and
    `other_runs` lists are capped at `MAX_ARTIFACT_STRAYS`, with exact counts
    beside them.
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

        expectations = Expectations.from_email(
            message.email,
            route=route,
            body_exact=body_exact,
            subject_match=subject_match,
            attachment_match=attachment_match,
        )
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

    searched = strays is not None and not strays_partial
    extra: Optional[List[Dict[str, Any]]] = None
    other_runs: Optional[List[Dict[str, Any]]] = None
    untagged: Optional[int] = None
    if strays is not None:
        own_tickets = [
            str(r.ticket_id) for found in readbacks.values() for r in found
        ]
        sorted_strays = classify_strays(strays, sent_tags, own_tickets, tag_prefix)
        extra = sorted_strays["extra"]
        other_runs = sorted_strays["other_runs"]
        untagged = sorted_strays["untagged"]

    return {
        "sent": len(sent_tags),
        "produced": produced,
        "exactly_once": len(exactly_once),
        "missing": missing,
        "duplicated": duplicated,
        "wrong": wrong,
        "extra": None if extra is None else extra[:MAX_ARTIFACT_STRAYS],
        "extra_count": None if extra is None else len(extra),
        "other_runs": None if other_runs is None else other_runs[:MAX_ARTIFACT_STRAYS],
        "other_runs_count": None if other_runs is None else len(other_runs),
        "untagged_count": untagged,
        "strays_searched": searched,
        "strays_partial": strays is not None and strays_partial,
        "strays_not_searched_reason": None if searched else (
            strays_reason or "this adapter cannot enumerate its records"
        ),
        "unsettled": unsettled,
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
            and searched
            and not extra
            and not unsettled
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
    if accounting["duplicated"] or accounting.get("extra"):
        return "UNACCOUNTED"
    if accounting.get("unsettled") and accounting["missing"]:
        # Not found yet, and the readback never settled: in flight, not lost.
        return "UNSETTLED"
    if accounting["missing"]:
        return "UNACCOUNTED"
    if accounting["wrong"]:
        return "MISPARSED"
    if accounting.get("unsettled"):
        return "UNSETTLED"
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
    readback_poll: Dict[str, float],
    *,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    wire_format: Optional[str] = None,
    allow_public_target: bool = False,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "tool": "ledger",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
    }
    config.update(match_options(route, body_exact, subject_match, attachment_match))
    config.update(
        {
            "readback_poll": dict(readback_poll),
            "format": wire_format,
            "allow_public_target": allow_public_target,
            "dry_run": False,
        }
    )
    return config


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    sent: Sequence[SentMessage],
    accounting: Dict[str, Any],
    readbacks: Dict[str, List[Readback]],
    readback_outcome: Optional["ReadbackOutcome"] = None,
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
                "intended": Expectations.from_email(
                    message.email,
                    route=config.get("expect_route"),
                    body_exact=bool(config.get("body_exact", False)),
                    subject_match=config.get("subject_match") or DEFAULT_SUBJECT_MATCH,
                    attachment_match=config.get("attachment_match")
                    or DEFAULT_ATTACHMENT_MATCH,
                ).to_json(),
                "response": message.response_json(),
                "tickets": [str(r.ticket_id) for r in found],
                "ticket_count": len(found),
            }
        )

    summary = dict(accounting, verdict=verdict(accounting))
    if readback_outcome is not None:
        # The accounting is only as settled as the read it came from. A run
        # that gave up before the counts held steady is reported as such rather
        # than as a balance it has not established.
        summary["readback"] = readback_summary(readback_outcome)
    artifact = artifact_header("ledger")
    artifact.update(
        {"seed": seed, "config": config, "summary": summary, "records": records}
    )
    return artifact


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

    readback = summary.get("readback")
    if readback:
        settled = "settled" if readback.get("stable") else "GAVE UP before settling"
        lines.append(
            f"  readback:        {readback.get('elapsed_s')}s over "
            f"{readback.get('polls')} poll(s), {settled}"
        )
    extra = summary.get("extra")
    extra_count = summary.get("extra_count")
    if extra_count is None:
        extra_count = len(extra or [])
    reason = (
        summary.get("strays_not_searched_reason")
        or "this adapter cannot enumerate its records"
    )
    if summary.get("strays_searched") or summary.get("strays_partial"):
        partial = (
            f" found in a PARTIAL search ({reason}); more may exist"
            if summary.get("strays_partial") else ""
        )
        lines.append(f"  extra:           {extra_count}{partial}")
        other = summary.get("other_runs_count") or 0
        untagged = summary.get("untagged_count") or 0
        if other or untagged:
            lines.append(
                f"  not counted:     {other} record(s) from other runs' tags, "
                f"{untagged} untagged record(s)"
            )
    else:
        lines.append(
            f"  extra:           NOT SEARCHED ({reason}, so a ticket this run "
            "never sent could have been missed)"
        )
    if summary.get("unsettled"):
        lines.append(
            "  the readback never settled, so a message not found yet may still "
            "be in flight; raise --max-wait or --quiet-window and rerun"
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
        if extra_count > len(extra):
            lines.append(
                f"    the artifact lists the first {len(extra)} of {extra_count}"
            )
    other_runs = summary.get("other_runs") or []
    if other_runs:
        _section(
            "other runs (informational)",
            other_runs,
            lambda e: f"ticket {e['ticket']} carries tag {e['tag']!r} from another run",
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
    count: int,
    seed: int,
    tag_prefix: str,
    readback: ReadbackConfig,
    *,
    route: Optional[str] = None,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
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
    lines.append(
        f"  checks:           subject match {subject_match}, attachments "
        f"{attachment_match}, "
        + (f"route {route!r}" if route else "routing skipped (no --expect-route)")
    )
    enumerable = can_enumerate_from(readback)
    if enumerable is False:
        lines.append(
            "  strays:           NOT SEARCHED. This adapter cannot enumerate its "
            "records, so the run will report UNVERIFIED STRAYS (exit 3) at best"
        )
    else:
        if enumerable is None:
            lines.append(
                "  strays:           searched only if the custom adapter defines "
                "list_all; otherwise reported as NOT SEARCHED"
            )
        else:
            lines.append(
                "  strays:           the store is enumerated after the readback"
            )
        lines.append(
            f"                    'extra' counts records tagged {tag_prefix}-* that "
            "this run did not send;"
        )
        lines.append(
            "                    other runs' tags are listed, untagged records "
            "are ignored"
        )
    lines.append("  point this at a system under barrage load, or with a "
                 "dependency failing, to answer")
    lines.append("  'did we lose anything?'")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def can_enumerate_from(readback: ReadbackConfig) -> Optional[bool]:
    """Whether this kind of adapter can look for records it was not asked
    about, from the config alone (a dry run must not build an adapter). None
    for a custom adapter, which is only known once built."""
    if readback.spec:
        return None
    if readback.kind == "mailbox":
        return True
    if readback.kind == "http":
        return bool(getattr(readback, "enumerate_records", True))
    return None


_CANNOT_ENUMERATE = "this adapter cannot enumerate its records"


def adapter_can_enumerate(adapter: Any) -> Tuple[bool, Optional[str]]:
    """(can, reason why not) for a built adapter.

    Asks, in order: `[readback].enumerate = false`; a `can_enumerate()`
    method (False, or raising, means no); a declared boolean `can_enumerate`
    attribute; and finally whether there is a `list_all` to call at all.
    """
    if getattr(adapter, "enumerate_records", True) is False:
        return False, "[readback].enumerate is false"
    declared = getattr(adapter, "can_enumerate", None)
    if callable(declared):
        try:
            if not declared():
                return False, _CANNOT_ENUMERATE
        except Exception as exc:
            return False, f"can_enumerate() failed: {exc}"
    elif not can_enumerate(adapter):
        return False, _CANNOT_ENUMERATE
    if not callable(getattr(adapter, "list_all", None)):
        return False, _CANNOT_ENUMERATE
    return True, None


def enumerate_store(
    adapter: Any,
) -> Tuple[Optional[List[Readback]], Optional[str], bool]:
    """(strays, reason, partial) after the readback.

    `strays` is None when the store cannot be enumerated, or when `list_all`
    raised: the messages have already been sent and their accounting stands,
    so a failure here becomes UNVERIFIED STRAYS, never a refusal. `partial` is
    True when the listing says pagination stopped short."""
    able, reason = adapter_can_enumerate(adapter)
    if not able:
        return None, reason, False
    try:
        listed = adapter.list_all()
        records = list(listed)
    except Exception as exc:
        return None, f"enumerating the store failed: {exc}", False
    if getattr(listed, "partial", False) is True:
        return records, "the enumeration was truncated (partial)", True
    return records, None, False


def enumerate_strays(adapter: Any) -> Tuple[Optional[List[Readback]], Optional[str]]:
    """(strays, reason). As `enumerate_store`, with a truncated listing
    treated as not searched at all."""
    records, reason, partial = enumerate_store(adapter)
    if partial:
        return None, reason
    return records, reason


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
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    allow_public_target: bool = False,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Send N tagged messages, reconcile, report. Never called unless
    `guardrails.evaluate_send` already said yes.

    0 when every message was accounted for exactly once with correct content
    and strays were searched; 3 otherwise, including UNVERIFIED STRAYS; 1 a
    refusal, which only happens before anything is sent or when the per-tag
    readback itself fails.
    """
    corpus = build_tagged_corpus(seed, count, tag_prefix)
    items = build_items(corpus)

    try:
        require_synthetic([email for email, _tag, _rid in corpus])
        validate_match_modes(subject_match, attachment_match)
        target = resolve_target(
            target_name, config_path, allow_public_target=allow_public_target
        )
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    try:
        adapter = open_adapter(
            readback, client=readback_client, config_path=config_path,
            builder=build_adapter,
        )
    except Exception as exc:
        printer(f"refused: the readback adapter could not be prepared: {exc}")
        return EXIT_REFUSED

    config = run_config(
        seed, count, tag_prefix, target_name, readback, route,
        {"quiet_window": quiet_window, "max_wait": max_wait,
         "poll_interval": poll_interval},
        body_exact=body_exact,
        subject_match=subject_match,
        attachment_match=attachment_match,
        wire_format=target.format_name,
        allow_public_target=bool(target.public),
    )
    sent: Optional[List[SentMessage]] = None
    phase = "send"
    try:
        sent = send_messages(
            items, target.url, rate, wire_format=target.wire_format, client=client,
            sleep=sleep, clock=clock,
        )
        phase = "readback"
        outcome = read_back_all(
            adapter,
            [probe for _e, _t, _r, probe in items],
            sleep=sleep,
            clock=clock,
            quiet_window=quiet_window,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )
        readbacks = outcome.readbacks
        phase = "stray search"
        strays, strays_reason, strays_partial = enumerate_store(adapter)
    except KeyboardInterrupt:
        return abort_run("ledger", seed, config, phase, sent, out, printer)
    except Exception as exc:
        printer(f"ledger: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    accounting = account(
        sent, readbacks, strays,
        tag_prefix=tag_prefix, route=route, body_exact=body_exact,
        subject_match=subject_match, attachment_match=attachment_match,
        strays_reason=strays_reason, strays_partial=strays_partial,
        unsettled=readback_unsettled(outcome),
    )
    artifact = build_artifact(
        seed,
        config,
        sent,
        accounting,
        readbacks,
        outcome,
    )
    printer(format_ledger(artifact))
    write_artifact(out, artifact)
    return EXIT_OK if accounting["balanced"] else EXIT_MISMATCH
