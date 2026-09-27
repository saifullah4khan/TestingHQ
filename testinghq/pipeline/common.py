"""Shared plumbing for the three pipeline tools: verify, ledger, redeliver.

All three do the same three things in the same order, and the order is a safety
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
processing yet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from ..core import guardrails
from ..core.config import load_config
from ..core.ratelimit import TokenBucket
from ..core.transport import TransportResult, post
from .messages import Probe
from .readback import Readback, ReadbackAdapter, ReadbackError

#: Exit codes, shared by verify, ledger and redeliver so all three script the
#: same way. 0, 1 and 2 match blast and barrage exactly; 3 is new and means the
#: run completed and the answer was no.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_DRY_RUN = 2
EXIT_MISMATCH = 3

#: How long the record counts must hold steady before a read is believed.
#: Long enough that a queue consumer has had a chance to write a second copy of
#: something, because the late duplicate is the case a single lookup misses and
#: the reason this polls rather than looks once.
DEFAULT_QUIET_WINDOW = 5.0

#: How long to keep polling before giving up and reporting whatever was seen.
DEFAULT_MAX_WAIT = 60.0

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


def resolve_target_url(target_name: Optional[str], config_path: str) -> str:
    """Load the target config and resolve `target_name` to a URL, gating both
    the configured name and the resolved URL through the canonical guardrail.

    Two calls, on purpose, and the reason is the same one given in
    `barrage/fire.py`: a bare single-label name like "local" has no dot, so the
    public-host hardening classifies it as internal and passes it
    unconditionally. The hardening then looks correct while being inert, and a
    public URL hiding behind a friendly name fires. Checking the resolved URL
    too is what makes it bite.
    """
    if not target_name:
        raise guardrails.GuardrailError("refusing to fire: --send requires --target")
    config = load_config(config_path)
    guardrails.require_configured_target(target_name, config.allowed_target_names())
    url = config.get(target_name).url
    guardrails.require_configured_target(url, (url,))
    return url


def send_all(
    items: Sequence[Tuple[InboundEmail, str, str, Probe]],
    url: str,
    rate: float,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> List[SentMessage]:
    """Fire every (email, tag, record_id, probe) in order, paced by a token
    bucket, and return what happened to each.

    Serial, and that is a deliberate constraint rather than a missing feature.
    The three tools in this package are about what the pipeline produced, not
    about throughput, and `barrage` is the tool that measures throughput. Adding
    a second, differently-shaped concurrency implementation here would mean two
    dispatch behaviours to reason about when reading a run artifact, and the
    one that already exists is the one that is honest about being serial.

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
        result = post(email, url, client=client)
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


@dataclass(frozen=True)
class ReadbackOutcome:
    """What the readback phase found, and how long it took to settle.

    `stable` is False when the phase hit `max_wait` before the counts held
    steady, which is a different statement from "everything was found": a run
    that gave up early cannot claim its answer was quiet.
    """

    readbacks: Dict[str, List[Readback]]
    elapsed: float
    polls: int
    stable: bool
    missing: Tuple[str, ...] = ()

    def to_json(self) -> Dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed, 3),
            "polls": self.polls,
            "stable": self.stable,
            "missing_tags": list(self.missing),
        }


def read_back_all(
    adapter: ReadbackAdapter,
    probes: Sequence[Probe],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> ReadbackOutcome:
    """Ask the system what it holds for every probe, until it stops changing.

    Believed stable when every tag has been found and the per-tag counts have
    not moved for `quiet_window`. Waiting for the counts specifically is what
    catches a duplicate that appears late: a read at t=0 sees one ticket, and
    one that only required "all found" would finish there and report a clean
    run moments before the second ticket lands.

    An adapter that raises is not swallowed. A readback that fails halfway is a
    broken run, and reporting the first half as "the pipeline produced nothing
    else" would be a lie with a number attached.

    A tag is asked once per poll, not once per delivery: a redelivery scenario
    sends the same message twice on purpose, and asking twice would either
    double-count the answer or return a different one the second time.
    """
    if quiet_window < 0:
        raise ValueError(f"quiet_window must be >= 0, got {quiet_window}")
    if max_wait < 0:
        raise ValueError(f"max_wait must be >= 0, got {max_wait}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

    asked: List[str] = []
    for probe in probes:
        if probe.tag not in asked:
            asked.append(probe.tag)

    started = clock()
    found: Dict[str, List[Readback]] = {tag: [] for tag in asked}
    previous: Optional[Tuple[int, ...]] = None
    unchanged_since: Optional[float] = None
    polls = 0
    stable = False

    while True:
        polls += 1
        for tag in asked:
            found[tag] = list(adapter.fetch(_probe_by_tag(probes, tag)) or [])

        now = clock()
        counts = tuple(len(found[tag]) for tag in asked)
        all_found = all(found[tag] for tag in asked)

        if all_found and counts == previous:
            if unchanged_since is None:
                unchanged_since = now
            if now - unchanged_since >= quiet_window:
                stable = True
                break
        else:
            # Either something is still missing or a count moved. Both reset the
            # window: a count moving is a late duplicate arriving, and believing
            # the earlier sighting is the bug this exists to prevent.
            unchanged_since = None
        previous = counts

        if now - started >= max_wait:
            break
        sleep(poll_interval)

    missing = tuple(tag for tag in asked if not found[tag])
    return ReadbackOutcome(
        readbacks=found,
        elapsed=clock() - started,
        polls=polls,
        stable=stable,
        missing=missing,
    )


def _probe_by_tag(probes: Sequence[Probe], tag: str) -> Probe:
    for probe in probes:
        if probe.tag == tag:
            return probe
    raise ReadbackError(f"no probe carries tag {tag!r}")


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
    "EXIT_DRY_RUN",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EXIT_REFUSED",
    "ReadbackError",
    "SentMessage",
    "close_adapter",
    "read_back_all",
    "require_synthetic",
    "resolve_target_url",
    "send_all",
    "status_class",
]
