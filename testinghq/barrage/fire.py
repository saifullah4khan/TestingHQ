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
3. A hard rate-and-duration ceiling, enforced by runner.check_rate_ceiling
   before any dispatch.

None of these may be weakened. If a test disagrees with a guardrail, the
code is wrong, not the guardrail.

This module imports the canonical guardrails and never reimplements them.
A second copy of a safety rule has already cost this project a real
incident: two copies disagreed within hours and a target the CLI refused
the UI would have fired at. See tests/test_lane_hygiene.py.
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

# Exit codes, matching the blast CLI's convention exactly so both tools
# script the same way: 0 ran, 1 refused, 2 dry-run (nothing was sent).
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_DRY_RUN = 2


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

    Both calls are positional, matching testinghq/cli.py's blast fire path
    and web/adapter.py.

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
    mode: str, rate: float, duration: float, concurrency: int, warmup: float
) -> RunPlan:
    """Turn CLI-shaped arguments into a RunPlan. `duration` is the TOTAL
    run length; `warmup` is the ramp portion of it, so the steady-state
    hold is `duration - warmup`."""
    if duration <= warmup:
        raise BarrageError(
            f"duration ({duration}s) must be greater than warmup ({warmup}s): "
            "there would be no steady-state hold to measure"
        )
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
    it, and states plainly that nothing was sent."""
    total = round(plan.rate * plan.hold_seconds)
    lines = [
        f"dry-run preview: seed={seed}, {pool_size} distinct payload(s) in the pool",
        f"  mode: {plan.mode}-loop",
        f"  target rate: {plan.rate:g}/s",
        f"  warmup ramp: {plan.warmup_seconds:g}s, steady-state hold: {plan.hold_seconds:g}s",
        f"  concurrency: {plan.concurrency}",
        f"  approx requests at steady state: {total}",
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
    printer(barrage_report.format_summary(artifact))
    write_artifact(out, artifact)
    return EXIT_OK
