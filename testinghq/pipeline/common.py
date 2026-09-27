"""Shared plumbing for the three pipeline tools: verify, ledger, redeliver.

All three do the same three things in the same order, and the order is a safety
property rather than a convenience:

  1. build the payloads, and check every address in them is synthetic
  2. resolve the target, and check it against the guardrails
  3. only then put anything on the wire

Doing (1) and (2) after the first request would mean a refusal lands halfway
through a run, with the operator's endpoint already having taken half a corpus
of synthetic mail. Blast and Barrage both already work this way; this module
keeps the three new tools from each reinventing it slightly differently.

Readback is deliberately NOT in that list, and it is fetched in its own phase
after every message has been sent. An intake pipeline is asynchronous far more
often than not: the POST is acknowledged and the ticket is created a moment
later, on a queue consumer. Reading back between sends would grade whichever
messages happened to have been processed and call the rest lost, which is a
false loss report on a perfectly healthy pipeline. `settle` is the delay between
the last send and the first read, and it defaults to zero because the
default pipeline under test is synchronous; `--settle` is for the ones that are
not.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
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


def read_back_all(
    adapter: ReadbackAdapter,
    probes: Sequence[Probe],
    sleep: Callable[[float], None] = time.sleep,
    settle: float = 0.0,
) -> Dict[str, List[Readback]]:
    """Ask the system what it holds for every probe, after everything has been
    sent.

    Returns a mapping keyed by tag. The mapping is keyed rather than positional
    on purpose: a pipeline that answered probe 3 and not probe 4 must not shift
    every later result by one, which is precisely how an accounting tool starts
    reporting confident nonsense.

    An adapter that raises is NOT swallowed. A readback that fails halfway is a
    broken run, and reporting the first half as "the pipeline produced nothing
    else" would be a lie with a number attached.
    """
    if settle and settle > 0:
        sleep(settle)
    found: Dict[str, List[Readback]] = {}
    asked: set = set()
    for probe in probes:
        if probe.tag in asked:
            # Asked once per tag, not once per delivery. A redelivery scenario
            # sends the same message twice on purpose, and asking the system
            # twice would either double-count the answer or return a different
            # one the second time, and both would corrupt the accounting this
            # exists to produce.
            continue
        asked.add(probe.tag)
        found[probe.tag] = list(adapter.fetch(probe) or [])
    return found


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
