"""Shared plumbing for the five pipeline tools: verify, ledger, redeliver,
loop, steady.

All five do the same three things in the same order, and the order is a safety
property rather than a convenience:

  1. build the payloads, and check every address in them is synthetic
  2. resolve the target, and check it against the guardrails
  3. only then put anything on the wire

Doing (1) and (2) after the first request would mean a refusal lands halfway
through a run, with the operator's endpoint already having taken half a corpus
of synthetic mail.

The readback is a separate phase, and it polls rather than looking once. A
pipeline that acknowledges a POST and creates the ticket on a queue consumer is
the normal case, not the exotic one, so a single lookup after the last send
would report a healthy pipeline as having lost everything it had not finished
processing yet. `SETTLING_RULE` below is the rule the poll ends by.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from ..core import guardrails
from ..core.formats import WireFormat
from ..core.ratelimit import TokenBucket
from ..core.targets import resolve_target
from ..core.transport import TransportResult, post
from .adapters import BudgetExhausted, ReadbackConfig, build_adapter, parse_readback_config
from .messages import Probe
from .readback import Readback, ReadbackAdapter, ReadbackError

#: Exit codes, re-exported from `testinghq.core.exit_codes` where the whole
#: package now defines them. verify, ledger, redeliver and loop import these
#: names, and they keep working, but the numbers themselves are written down in
#: exactly one module so that a tool cannot drift onto a different convention
#: and a script cannot have to special-case which tool it is calling.
from ..core.exit_codes import (  # noqa: F401
    EXIT_DRY_RUN,
    EXIT_FINDING,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
)

#: How long the observed state must hold steady before a read is believed.
#: Long enough that a queue consumer has had a chance to write a second copy of
#: something, because the late duplicate is the case a single lookup misses and
#: the reason this polls rather than looks once.
DEFAULT_QUIET_WINDOW = 5.0

#: How long to keep polling before giving up on a store that is still changing.
DEFAULT_MAX_WAIT = 60.0

#: When the readback ends and what its end means. The `--quiet-window` and
#: `--max-wait` help prints it, and the [readback] notes in
#: `core/config_schema.py` (and so docs/CONFIG.md) carry the same paragraph.
SETTLING_RULE = (
    "The readback is SETTLED once what it sees (which tags are found, how many "
    "records each, and the field values the checks read) has not changed for "
    "--quiet-window seconds; a tag still not found then is MISSING and is "
    "reported as lost. It is UNSETTLED only when --max-wait runs out while that "
    "state is still changing, which exits 3 with an UNSETTLED headline and "
    "keeps every finding already proven. For a slow or async pipeline, raise "
    "--quiet-window above the pipeline's worst processing latency (a "
    "search-indexed store such as Zendesk or Freshdesk can take minutes to "
    "index a new ticket), and keep --max-wait well above --quiet-window."
)

#: How often to look while waiting.
DEFAULT_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class SentMessage:
    """One payload that went out, and everything needed to ask about it later."""

    index: int
    record_id: str
    tag: str
    email: InboundEmail
    probe: Probe
    result: TransportResult

    def response_json(self) -> Dict[str, Any]:
        return {
            "status": self.result.status,
            "latency_ms": self.result.latency_ms,
            "body_snippet": self.result.body_snippet,
        }


def require_synthetic(emails: Sequence[InboundEmail]) -> None:
    """Every address in every payload must look synthetic, checked over the
    whole set before the first request. Delegates to the canonical guardrail,
    which is the security lane's file and is never reimplemented here."""
    fields: List[str] = []
    for email in emails:
        fields.append(email.to)
        fields.append(email.from_addr)
        fields.append(email.envelope.from_addr)
        fields.extend(email.envelope.to)
    guardrails.require_synthetic_content(fields)


def resolve_target_url(
    target_name: Optional[str], config_path: str, *, allow_public_target: bool = False
) -> str:
    """Deprecated: use `testinghq.core.targets.resolve_target`, which also
    returns the target's wire format. Kept as a wrapper for one release."""
    return resolve_target(
        target_name, config_path, allow_public_target=allow_public_target
    ).url


def send_all(
    items: Sequence[Tuple[InboundEmail, str, str, Probe]],
    url: str,
    rate: float,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wire_format: Optional[WireFormat] = None,
) -> List[SentMessage]:
    """Fire every (email, tag, record_id, probe) in order, paced by a token
    bucket, and return what happened to each.

    Serial, and that is a deliberate constraint rather than a missing feature.
    The five tools in this package are about what the pipeline produced, not
    about throughput, and `barrage` is the tool that measures throughput.

    Barrage now dispatches through a thread pool, so there are two
    differently-shaped concurrency implementations in the package rather than
    one. The line is drawn at the question a tool answers, not at the mechanism:
    a correctness tool's answer does not depend on how fast the target replies,
    and adding a pool here would mean two dispatch behaviours to reason about
    when reading a run artifact, only one of which a reader would expect to be
    concurrent. A run artifact from any of these five is serial by construction,
    and nothing in a report implies otherwise.

    `sleep` AND `clock` are threaded into the rate limiter, and both are
    parameters for the same reason. Injecting a no-op `sleep` while the bucket
    kept the real clock turns a "hermetic, fast" test into one that busy-waits
    out the real rate in real seconds, which is how this suite's integration
    tests quietly grew a seventy-second run while every test still said
    nothing was left to do.
    """
    bucket = TokenBucket(
        rate_per_sec=rate, capacity=max(rate, 1.0), sleep=sleep, clock=clock
    )
    sent: List[SentMessage] = []
    for index, (email, tag, record_id, probe) in enumerate(items):
        bucket.acquire()
        result = post(email, url, client=client, wire_format=wire_format)
        sent.append(
            SentMessage(
                index=index,
                record_id=record_id,
                tag=tag,
                email=email,
                probe=probe,
                result=result,
            )
        )
    return sent


#: The verdict a run gets when its readback did not settle within --max-wait.
#: Distinct from loss: a message not seen yet is not a message the pipeline
#: lost, and saying it was is the false alarm that teaches people to ignore
#: the tool.
UNSETTLED = "UNSETTLED"

#: `verdict_source` for a verdict decided by the readback phase rather than by
#: the tool's own checks.
VERDICT_SOURCE_READBACK = "readback"

#: The headline line for a run whose every lookup was a 404.
ALL_404_HEADLINE = "every lookup was 404: check the readback url"


@dataclass(frozen=True)
class ReadbackOutcome:
    """What the readback phase found, and how long it took to settle.

    `stable` is True when the observed state held still for the quiet window.
    A tag in `missing` is then MISSING: the store went quiet without it, and
    the tool reports it as lost. `stable` is False when `max_wait` ran out
    while the state was still changing, which makes the run UNSETTLED: a run
    that never saw the store go quiet cannot claim a message it had not seen
    yet was lost, and `missing` then means "not seen yet". `budget_exhausted`
    says the budget ran out in the middle of a poll or a retry wait. `partial`
    names the tags whose lookup hit `max_pages`. `all_404` is True when every
    lookup of every poll got a 404, which is almost always a wrong url rather
    than a pipeline that lost everything.
    """

    readbacks: Dict[str, List[Readback]]
    elapsed: float
    polls: int
    stable: bool
    missing: Tuple[str, ...] = ()
    budget_exhausted: bool = False
    partial: Tuple[str, ...] = ()
    all_404: bool = False
    detail: Optional[str] = None

    @property
    def unsettled(self) -> bool:
        return not self.stable

    def headline(self) -> Optional[str]:
        """One line for the top of a tool's report, or None when the readback
        settled normally."""
        if self.all_404:
            return ALL_404_HEADLINE
        if self.unsettled:
            missing = (
                f", {len(self.missing)} tag(s) not seen yet" if self.missing else ""
            )
            return (
                f"{UNSETTLED}: the readback did not settle within --max-wait "
                f"({self.elapsed:.1f}s, {self.polls} poll(s){missing}): the "
                "store was still changing. Unseen is not lost: raise --max-wait "
                "above --quiet-window for a slow or async pipeline"
            )
        return None

    def to_json(self) -> Dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed, 3),
            "polls": self.polls,
            "stable": self.stable,
            "unsettled": self.unsettled,
            "verdict": UNSETTLED if self.unsettled else None,
            "budget_exhausted": self.budget_exhausted,
            "missing_tags": list(self.missing),
            "partial_tags": list(self.partial),
            "all_404": self.all_404,
            "headline": self.headline(),
            "detail": self.detail,
        }


def readback_verdict(verdict: str, outcome: Optional[ReadbackOutcome]) -> Tuple[str, str]:
    """(verdict, verdict_source) after the readback has had its say.

    An unsettled readback (the store was still changing at --max-wait)
    replaces the tool's own verdict with UNSETTLED, because every absence that
    verdict rests on is unproven. The findings the tool did make still stand
    and are still listed and counted; the exit code stays 3. A settled
    readback leaves the tool's verdict alone, so a message the store went
    quiet without is reported by the tool as the loss it is.
    """
    if outcome is not None and outcome.unsettled:
        return UNSETTLED, VERDICT_SOURCE_READBACK
    return verdict, "tool"


def _signature(readbacks: Sequence[Readback]) -> Tuple[Readback, ...]:
    """What the quiet window watches for one tag: every readback, every field
    value. Sorted so an API that returns the same records in a different order
    does not look like a change."""
    return tuple(sorted(readbacks, key=lambda r: (r.ticket_id or "", repr(r))))


def read_back_all(
    adapter: ReadbackAdapter,
    probes: Sequence[Probe],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    require_all_found: bool = True,
) -> ReadbackOutcome:
    """Ask the system what it holds for every probe, until it stops changing.

    THE RULE (`SETTLING_RULE`). The observed state is which tags are found,
    how many records each, and every field value the checks read. The read is
    SETTLED, and `stable` is True, once that state has not changed for
    `quiet_window` (measured from the first poll that confirms it). A tag
    still not found at that point is MISSING, and the tool reports it as
    lost. The run is UNSETTLED, `stable` False, only when `max_wait` runs out
    while the state is still changing: records still arriving, or a field
    value changed within the last quiet window. A store that never goes
    quiet for a whole window before `max_wait` (including a `max_wait`
    shorter than `quiet_window`) is UNSETTLED for the same reason.

    Absence does not hold the window open. It used to: "every tag found" was
    part of the condition, so a pipeline that genuinely dropped one message
    could never settle, and every loss surfaced as UNSETTLED after
    `max_wait`. A loss is the main thing `ledger` exists to report.

    Watching the values is what catches an async pipeline that creates a
    ticket first and fills its route or body a moment later; watching the
    counts is what catches the late duplicate.

    GUIDANCE FOR ASYNC PIPELINES. If the pipeline acknowledges the POST and
    does its work on a queue consumer, the quiet window has to be longer than
    the pipeline's worst processing latency (the gap between a send and its
    record appearing, and between a record appearing and its last field being
    filled), or a message still in the queue is reported as lost. `max_wait`
    has to be longer than the quiet window plus the backlog the pipeline can
    build. A search-indexed store (Zendesk, Freshdesk) can take minutes to
    index a new ticket: raise `--quiet-window` above that, and `--max-wait`
    well above it.

    `max_wait` is a hard budget: it is checked before every lookup, and an
    adapter that supports `bind_clock` is told the deadline, so its request
    timeouts, retry waits and rate-limit waits spend the same budget. A run
    that runs out is UNSETTLED, not lost.

    `require_all_found` is accepted for compatibility and has no effect: since
    absence no longer holds the window open, every caller now gets what
    `loop` asked for with `require_all_found=False`.

    An adapter that raises is not swallowed, with one exception: running out
    of budget inside the adapter ends the poll like running out of budget
    between lookups does.

    A tag is asked once per poll, not once per delivery.
    """
    if quiet_window < 0:
        raise ValueError(f"quiet_window must be >= 0, got {quiet_window}")
    if max_wait < 0:
        raise ValueError(f"max_wait must be >= 0, got {max_wait}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

    by_tag: Dict[str, Probe] = {}
    for probe in probes:
        by_tag.setdefault(probe.tag, probe)
    asked = list(by_tag)

    started = clock()
    deadline = started + max_wait
    binder = getattr(adapter, "bind_clock", None)
    if callable(binder):
        binder(clock, sleep, deadline)

    found: Dict[str, List[Readback]] = {tag: [] for tag in asked}
    partial: Dict[str, bool] = {tag: False for tag in asked}
    statuses: Dict[str, List[Optional[int]]] = {tag: [] for tag in asked}
    previous: Optional[Tuple[Tuple[Readback, ...], ...]] = None
    unchanged_since: Optional[float] = None
    polls = 0
    stable = False
    exhausted = False
    detail: Optional[str] = None
    lookups = 0

    try:
        while True:
            if polls and clock() >= deadline:
                break
            polls += 1
            for tag in asked:
                if lookups and clock() >= deadline:
                    exhausted = True
                    break
                lookups += 1
                try:
                    result = adapter.fetch(by_tag[tag])
                except BudgetExhausted as exc:
                    exhausted = True
                    detail = str(exc)
                    break
                partial[tag] = bool(getattr(result, "partial", False))
                statuses[tag].append(getattr(result, "status", None))
                found[tag] = list(result or [])
            if exhausted:
                break

            now = clock()
            # The whole observed state: an empty tuple for a tag not found yet,
            # so a tag appearing is a change like any other.
            signature = tuple(_signature(found[tag]) for tag in asked)

            if signature == previous:
                if unchanged_since is None:
                    unchanged_since = now
                if now - unchanged_since >= quiet_window:
                    stable = True
                    break
            else:
                # Something changed: a tag appeared, a count moved, or a field
                # value did. That resets the window, because believing the
                # earlier sighting is the bug this exists to prevent. A tag
                # that is merely still missing does not: a store that has gone
                # quiet without it has answered.
                unchanged_since = None
            previous = signature

            if now - started >= max_wait:
                break
            sleep(min(poll_interval, deadline - now))
    finally:
        if callable(binder):
            binder(clock, sleep, None)

    seen_statuses = [s for tag in asked for s in statuses[tag]]
    all_404 = bool(seen_statuses) and all(s == 404 for s in seen_statuses) and all(
        statuses[tag] for tag in asked
    )
    return ReadbackOutcome(
        readbacks=found,
        elapsed=clock() - started,
        polls=polls,
        stable=stable,
        missing=tuple(tag for tag in asked if not found[tag]),
        budget_exhausted=exhausted,
        partial=tuple(tag for tag in asked if partial[tag]),
        all_404=all_404,
        detail=detail,
    )


def prepare_readback(
    args_or_config: Any,
    *,
    client: Any = None,
    config_path: Optional[str] = None,
    config_dir: Optional[str] = None,
    preflight: bool = True,
) -> ReadbackAdapter:
    """Build the readback adapter and check it works, before anything is sent.

    The one call every tool makes in place of `build_adapter`, so the three
    things that must happen before the first send happen everywhere:

      1. the adapter is built, with a relative mailbox path resolved against
         the directory of the config file (`config_path`, or `config_dir`);
      2. a `module:attribute` that cannot be imported or built is refused;
      3. `preflight()` runs when the adapter has one: refused credentials, a
         404 list path, a response that is not JSON, or a sink directory that
         does not exist are refusals now, not a broken run later.

    `args_or_config` is a `ReadbackConfig`, a raw `[readback]` table, or a kind
    string. Raises `AdapterError`; the adapter is closed when preflight fails.
    """
    config = (
        args_or_config
        if isinstance(args_or_config, ReadbackConfig)
        else parse_readback_config(args_or_config)
    )
    if config_dir is None and config_path:
        config_dir = str(Path(config_path).resolve().parent)
    adapter = build_adapter(config, client=client, config_dir=config_dir)
    if preflight:
        check = getattr(adapter, "preflight", None)
        if callable(check):
            try:
                check()
            except Exception:
                close_adapter(adapter)
                raise
    return adapter


def close_adapter(adapter: Optional[ReadbackAdapter]) -> None:
    """Close an adapter if it can be closed, ignoring an adapter that does not
    care. A `ReadbackError` from `close` is deliberately not swallowed: an
    adapter holding a database connection that fails to close is a real problem
    and the operator should hear about it."""
    if adapter is None:
        return
    closer = getattr(adapter, "close", None)
    if callable(closer):
        closer()


def status_class(result: TransportResult) -> str:
    status = result.status
    if status is None:
        return "timeout"
    if 200 <= status < 300:
        return "2xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return f"other({status})"


__all__ = [
    "ALL_404_HEADLINE",
    "EXIT_DRY_RUN",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EXIT_REFUSED",
    "ReadbackError",
    "ReadbackOutcome",
    "SETTLING_RULE",
    "SentMessage",
    "UNSETTLED",
    "VERDICT_SOURCE_READBACK",
    "close_adapter",
    "prepare_readback",
    "read_back_all",
    "readback_verdict",
    "require_synthetic",
    "resolve_target_url",
    "send_all",
    "status_class",
]
