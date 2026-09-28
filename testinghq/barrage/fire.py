"""Barrage's fire and replay orchestration.

Barrage is a load generator that fires provider-shaped payloads at an
endpoint the operator controls, at a high but controlled rate, and reports
throughput, latency distribution, and error behaviour under sustained
load. It is a load tester against your own infrastructure. It is NOT an
email sender, NOT a flooding tool, and NOT for endpoints you do not own.

Three safety controls make it a load tester rather than a weapon, and this
module is where all three are enforced on the way to the wire:

1. Dry-run is the DEFAULT. Sending requires an explicit --send, decided by
   guardrails.evaluate_send. A dry run makes ZERO network calls: it never
   resolves a client, never builds a request, never touches transport.
2. Configured targets ONLY. guardrails.require_configured_target gates
   both the target name (the allow-list check) and the resolved URL (the
   public-host check).
3. A hard rate, duration and concurrency ceiling, enforced by runner.check_rate_ceiling
   before any dispatch.

None of these may be weakened. If a test disagrees with a guardrail, the
code is wrong, not the guardrail.

This module imports the canonical guardrails and never reimplements them.
A second copy of a safety rule has already cost this project a real
incident: two copies disagreed within hours and a target the CLI refused
the UI would have fired at. See tests/test_repo_invariants.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..blast.payload import InboundEmail
from ..core import guardrails
from ..core.config import load_config
from ..core.transport import post
from . import report as barrage_report
from .payloads import DEFAULT_POOL_SIZE, build_payload_pool, payload_for_index
from .runner import (
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_RATE_PER_SEC,
    DispatchRecord,
    RunPlan,
    check_rate_ceiling,
    run,
)

DEFAULT_RATE = 10.0
DEFAULT_DURATION = 30.0
DEFAULT_CONCURRENCY = 4
DEFAULT_WARMUP = 5.0
DEFAULT_MODE = "open"
DEFAULT_SEED = 0

# Exit codes, re-exported from `testinghq.core.exit_codes` so every tool in the
# package answers a script the same way. barrage has no EXIT_FINDING of its own:
# a barrage run's result is in its report, and `execute` returns EXIT_OK for a
# completed run whatever the statuses were.
from ..core.exit_codes import (  # noqa: F401
    EXIT_DRY_RUN,
    EXIT_OK,
    EXIT_REFUSED,
)


class BarrageError(RuntimeError):
    """Raised for a malformed run request that no guardrail owns (a bad
    warmup/duration combination, an unusable saved artifact)."""


# ---------------------------------------------------------------------------
# Guardrail wiring
# ---------------------------------------------------------------------------


def resolve_target_url(target_name: Optional[str], config_path: str) -> str:
    """Load the target config and resolve `target_name` to a URL, enforcing
    the canonical guardrail TWICE: once on the configured name (the
    allow-list check) and once on the resolved URL (the public-host check).

    Both calls are positional, matching the blast fire path in
    testinghq/cli/blast.py and web/adapter.py.

    Checking only the name is the trap: a bare single-label target name
    like "local" has no dot, so guardrails' public-host hardening
    classifies it as an internal name and passes it unconditionally. The
    hardening then looks correct while being completely inert, and a real
    public URL hiding behind a friendly name would fire. Resolving the URL
    and running it through the same guardrail is what makes that hardening
    actually bite.
    """
    if not target_name:
        raise guardrails.GuardrailError("refusing to fire: --send requires --target")
    config = load_config(config_path)
    guardrails.require_configured_target(target_name, config.allowed_target_names())
    url = config.get(target_name).url
    guardrails.require_configured_target(url, (url,))
    return url


def _address_fields(email: InboundEmail) -> List[str]:
    return [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]


def require_synthetic_pool(pool: List[InboundEmail]) -> None:
    """Guardrail check before any network call: every address in every
    payload that could be fired must look synthetic. Checked over the whole
    pool up front, so a bad payload aborts the run before anything is sent
    rather than after some prefix of the run already fired."""
    fields: List[str] = []
    for email in pool:
        fields.extend(_address_fields(email))
    guardrails.require_synthetic_content(fields)


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def build_plan(
    mode: str,
    rate: float,
    duration: float,
    concurrency: Optional[int],
    warmup: float,
) -> RunPlan:
    """Turn CLI-shaped arguments into a RunPlan. `duration` is the TOTAL
    run length; `warmup` is the ramp portion of it, so the steady-state
    hold is `duration - warmup`.

    `concurrency` may be None, meaning "not specified", and then
    DEFAULT_CONCURRENCY applies in both modes.

    Open mode's concurrency is the maximum number of requests in flight, not a
    worker count: arrivals happen on an absolute schedule and the pool bounds
    how many of them are outstanding at once. It used to be coerced to 1 here,
    because the dispatcher was serial and any other number would have been
    written into the artifact and printed by the preview while doing nothing.
    That is fixed; the number now means what it says in both modes.

    Old artifacts are still replayable. One written before the executor records
    a concurrency of 1, and replaying it re-runs it exactly, which is the right
    answer: the run it describes did have one request in flight at a time. A
    run recorded with a higher number replays with that number, and this time
    it will mean something.
    """
    if duration <= warmup:
        raise BarrageError(
            f"duration ({duration}s) must be greater than warmup ({warmup}s): "
            "there would be no steady-state hold to measure"
        )
    if concurrency is None:
        concurrency = DEFAULT_CONCURRENCY
    return RunPlan(
        mode=mode,
        rate=rate,
        concurrency=concurrency,
        warmup_seconds=warmup,
        hold_seconds=duration - warmup,
    )


def run_config(
    plan: RunPlan,
    seed: int,
    pool_size: int,
    target_name: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    """The artifact's config block. Everything needed to reproduce the run
    via `replay`, and nothing that varies between runs of the same command
    (no latency, no wall clock), so the same seed and config always
    regenerate the same corpus."""
    return {
        "mode": plan.mode,
        "rate": plan.rate,
        "duration": plan.duration_seconds,
        "warmup": plan.warmup_seconds,
        "concurrency": plan.concurrency,
        "seed": seed,
        "pool_size": pool_size,
        "target": target_name,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Dispatch wiring
# ---------------------------------------------------------------------------


def make_send_fn(
    pool: List[InboundEmail],
    url: str,
    client=None,
    clock: Callable[[], float] = time.monotonic,
):
    """Build the runner's send hook: post the pool's payload for `index` at
    `url` and report how long it took, so closed-loop dispatch knows when
    the worker frees up. `client` is None in real use (transport.post then
    opens a real socket via UrllibHttpClient); tests inject a fake."""

    def send_fn(index: int):
        payload = payload_for_index(pool, index)
        result = post(payload, url, client=client, clock=clock)
        return (result, result.latency_ms / 1000.0)

    return send_fn


def samples_from_records(records: List[DispatchRecord]) -> List[barrage_report.Sample]:
    """Convert the runner's dispatch records into reporting samples,
    normalizing dispatch times so the run's series starts at t=0."""
    if not records:
        return []
    origin = min(r.dispatch_time for r in records)
    return [
        barrage_report.Sample(
            dispatch_time=r.dispatch_time - origin,
            latency_ms=r.result.latency_ms,
            status=r.result.status,
            target_rate=r.target_rate,
        )
        for r in records
    ]


def write_artifact(path: Optional[str], artifact: Dict[str, Any]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")


def format_dry_run_preview(plan: RunPlan, seed: int, pool_size: int) -> str:
    """What a dry run prints instead of firing. Describes exactly what
    WOULD be sent, so an operator can check the plan before committing to
    it, and states plainly that nothing was sent.

    The dispatch note is not decoration, in either direction. It used to say
    "SERIAL, one request in flight at a time (no executor yet)" because that
    was true and the preview existed to stop an operator believing otherwise. It
    now says what will actually happen: a pool of `plan.concurrency` workers.
    A preview that describes the previous build is worse than no preview, so the
    line is derived from the plan rather than written as a standing apology.
    """
    total = round(plan.rate * plan.hold_seconds)
    if plan.concurrency == 1:
        dispatch = [
            "  dispatch: one worker, one request in flight at a time",
            "    a target slower than the arrival interval will cap the achieved",
            "    rate below the target above; raise --concurrency to keep the",
            "    schedule",
        ]
    else:
        dispatch = [
            f"  dispatch: a pool of {plan.concurrency} workers, so up to",
            f"    {plan.concurrency} request(s) in flight at once",
            "    each worker holds one interpreter thread, so this costs memory",
            "    in proportion to the number",
        ]
    lines = [
        f"dry-run preview: seed={seed}, {pool_size} distinct payload(s) in the pool",
        f"  mode: {plan.mode}-loop",
        f"  target rate: {plan.rate:g}/s",
        f"  warmup ramp: {plan.warmup_seconds:g}s, steady-state hold: {plan.hold_seconds:g}s",
        f"  concurrency: {plan.concurrency}",
        f"  approx requests at steady state: {total}",
        *dispatch,
        "no network calls were made (pass --send to fire for real)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The send path
# ---------------------------------------------------------------------------


def execute(
    plan: RunPlan,
    seed: int,
    pool_size: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    allow_high_rate: bool = False,
    client=None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    printer: Callable[[str], None] = print,
) -> int:
    """The shared send path for `fire` and `replay`. Returns a process exit
    code. Never called unless guardrails.evaluate_send already said yes.

    Order matters and is a safety property: the payload pool is built and
    checked for synthetic content, and the target is resolved and checked
    against the guardrails, BEFORE anything is dispatched. A refusal must
    happen before the first request, not after some prefix of the run has
    already hit the endpoint.
    """
    pool = build_payload_pool(seed, pool_size)
    require_synthetic_pool(pool)
    url = resolve_target_url(target_name, config_path)

    records = run(
        plan,
        make_send_fn(pool, url, client=client, clock=clock),
        clock=clock,
        sleep=sleep,
        allow_high_rate=allow_high_rate,
    )

    artifact = barrage_report.build_artifact(
        seed,
        run_config(plan, seed, pool_size, target_name, dry_run=False),
        samples_from_records(records),
    )
    artifact["dispatch"] = dispatch_summary(records, plan.concurrency)
    printer(barrage_report.format_summary(artifact))
    note = format_dispatch_note(artifact["dispatch"])
    if note:
        printer(note)
    write_artifact(out, artifact)
    return EXIT_OK


#: A request held back longer than this for a free worker counts as queued.
#: Ten milliseconds: half the arrival interval at the 50 req/s ceiling, and well
#: above thread start-up jitter, which measured at up to 2.4ms on a healthy
#: 8-worker run and would otherwise print a saturation warning on every run.
QUEUED_THRESHOLD_SECONDS = 0.010


def dispatch_summary(records: List[DispatchRecord], concurrency: int) -> Dict[str, Any]:
    """How long requests waited for a free worker.

    Throughput already shows a saturated pool, because each record's
    `dispatch_time` is when the request actually went out. This names the
    cause, so a report that falls short of its target says whether the target
    was slow or the pool was too small to keep the schedule.
    """
    waits = [r.queued for r in records]
    queued = [w for w in waits if w > QUEUED_THRESHOLD_SECONDS]
    return {
        "concurrency": concurrency,
        "queued_requests": len(queued),
        "max_queue_wait_ms": round(max(waits) * 1000.0, 3) if waits else 0.0,
    }


def format_dispatch_note(dispatch: Dict[str, Any]) -> str:
    """One warning line when the pool was the bottleneck, and nothing when it
    was not. Silence is the normal case; a line on every run would teach an
    operator to skip it."""
    if not dispatch.get("queued_requests"):
        return ""
    return (
        f"dispatch: {dispatch['queued_requests']} request(s) waited for a free "
        f"worker, up to {dispatch['max_queue_wait_ms']:g}ms. All "
        f"{dispatch['concurrency']} workers were busy, so the schedule was not "
        "held and the shortfall above is partly this tool's. Raise --concurrency."
    )
