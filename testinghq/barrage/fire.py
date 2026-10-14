"""Barrage's fire and replay orchestration.

Barrage fires clean, provider-shaped payloads at an endpoint the operator
controls, at a controlled rate, and reports how the pipeline held. It is a load
tester for your own infrastructure: not an email sender, not a flooding tool,
not for endpoints you do not own.

Three controls are enforced here on the way to the wire, and none may be
weakened:

1. Dry run is the default. Sending needs --send (guardrails.evaluate_send), and
   a dry run makes zero network calls.
2. Configured targets only, resolved through core.targets.resolve_target. A
   public host needs --allow-public-target AND `public = true` on the target.
3. The hard rate, duration and concurrency ceiling (runner.check_rate_ceiling).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..blast.payload import InboundEmail
from ..core import guardrails
# Re-exported, as the shared objects, for callers that import them from here.
from ..core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED  # noqa: F401
from ..core.targets import BARRAGE, resolve_target
from ..core.transport import PersistentHttpClient, TransportResult, UrllibHttpClient, post
from . import report as barrage_report
from .executor import PoolExecutor
from .payloads import build_payload_pool, payload_for_index
from .runner import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MAX_QUEUE_SECONDS,
    RunPlan,
    RunResult,
    arrival_schedule,
    check_rate_ceiling,
    run,
)

DEFAULT_RATE = 10.0
DEFAULT_DURATION = 30.0
DEFAULT_CONCURRENCY = 4
DEFAULT_WARMUP = 5.0
DEFAULT_MODE = "open"
DEFAULT_SEED = 0
DEFAULT_TIMEOUT = 10.0


class BarrageError(RuntimeError):
    """A malformed run request that no guardrail owns (a bad warmup/duration
    combination, a non-positive timeout, an unusable saved artifact)."""


# ---------------------------------------------------------------------------
# Guardrail wiring
# ---------------------------------------------------------------------------


def _address_fields(email: InboundEmail) -> List[str]:
    return [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]


def require_synthetic_pool(pool: List[InboundEmail]) -> None:
    """Every address in every payload that could be fired must look synthetic.
    Checked over the whole pool before anything is sent."""
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
    *,
    grace: float = DEFAULT_GRACE_SECONDS,
    max_queue_seconds: float = DEFAULT_MAX_QUEUE_SECONDS,
) -> RunPlan:
    """CLI-shaped arguments to a RunPlan. `duration` is the total run length
    and `warmup` is part of it. A `concurrency` of None means the default."""
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
        grace_seconds=grace,
        max_queue_seconds=max_queue_seconds,
    )


def check_timeout(timeout: float) -> None:
    if not timeout or timeout <= 0:
        raise BarrageError(f"timeout must be > 0 seconds, got {timeout!r}")


def run_config(
    plan: RunPlan,
    seed: int,
    pool_size: int,
    target_name: Optional[str],
    dry_run: bool,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    keepalive: bool = True,
    format_name: Optional[str] = None,
    allow_public_target: bool = False,
) -> Dict[str, Any]:
    """The artifact's config block: everything `replay` needs to rebuild the
    same schedule and send it the same way, and nothing that varies by run."""
    return {
        "mode": plan.mode,
        "rate": plan.rate,
        "duration": plan.duration_seconds,
        "warmup": plan.warmup_seconds,
        "ramp_steps": plan.ramp_step_count,
        "concurrency": plan.concurrency,
        "grace": plan.grace_seconds,
        "max_queue_seconds": plan.max_queue_seconds,
        "timeout": timeout,
        "keepalive": keepalive,
        "seed": seed,
        "pool_size": pool_size,
        "target": target_name,
        "format": format_name,
        "allow_public_target": allow_public_target,
        "dry_run": dry_run,
    }


#: Config keys a saved artifact must carry for replay to rebuild the plan.
REPLAY_REQUIRED = ("mode", "rate", "duration", "warmup", "concurrency", "pool_size")


def plan_from_artifact(data: Dict[str, Any]) -> Tuple[RunPlan, int, int, Optional[str], Dict[str, Any]]:
    """Rebuild (plan, seed, pool_size, target, send options) from an artifact.

    Keys an older artifact lacks take today's defaults. Raises BarrageError
    for an artifact that cannot be reproduced."""
    seed = data.get("seed") if isinstance(data, dict) else None
    config = (data.get("config") or {}) if isinstance(data, dict) else {}
    version = data.get("schema_version", 0) if isinstance(data, dict) else 0
    if isinstance(version, int) and version > barrage_report.SCHEMA_VERSION:
        raise BarrageError(
            f"schema_version {version} is newer than this version of barrage "
            f"understands ({barrage_report.SCHEMA_VERSION}), cannot reproduce the run"
        )
    if seed is None or any(config.get(key) is None for key in REPLAY_REQUIRED):
        raise BarrageError(
            f"missing seed or config {REPLAY_REQUIRED}, cannot reproduce the run"
        )
    try:
        plan = RunPlan(
            mode=config["mode"],
            rate=config["rate"],
            concurrency=config["concurrency"],
            warmup_seconds=config["warmup"],
            hold_seconds=config["duration"] - config["warmup"],
            ramp_step_count=config.get("ramp_steps", 10),
            grace_seconds=config.get("grace", DEFAULT_GRACE_SECONDS),
            max_queue_seconds=config.get("max_queue_seconds", DEFAULT_MAX_QUEUE_SECONDS),
        )
    except (TypeError, ValueError) as exc:
        raise BarrageError(f"the saved plan is not valid: {exc}") from None
    options = {
        "timeout": config.get("timeout", DEFAULT_TIMEOUT),
        "keepalive": config.get("keepalive", True),
    }
    return plan, seed, config["pool_size"], config.get("target"), options


# ---------------------------------------------------------------------------
# Dispatch wiring
# ---------------------------------------------------------------------------


class _ConnectFailed(Exception):
    def __init__(self, error: Exception, elapsed_ms: float):
        super().__init__(str(error))
        self.elapsed_ms = elapsed_ms


class _Sender:
    """The runner's send hook: post payload `index` and return the
    TransportResult.

    With keep-alive, each worker thread gets its own PersistentHttpClient,
    connected once before its first request so the handshake is measured on
    its own and kept out of every service latency. If that connect fails, the
    request it was for is reported as a transport error rather than tried a
    second time, so one request never waits out two timeouts."""

    def __init__(self, pool, url, *, client, clock, timeout, wire_format, keepalive):
        self._pool = pool
        self._url = url
        self._fixed = client
        self._clock = clock
        self._timeout = timeout
        self._wire_format = wire_format
        self._keepalive = keepalive
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients: List[PersistentHttpClient] = []
        self._connect_failures = 0

    def _client(self):
        if self._fixed is not None:
            return self._fixed
        if not self._keepalive:
            return UrllibHttpClient()
        client = getattr(self._local, "client", None)
        if client is None:
            client = PersistentHttpClient()
            self._local.client = client
            with self._lock:
                self._clients.append(client)
            started = self._clock()
            try:
                client.connect(self._url, self._timeout)
            except Exception as exc:  # noqa: BLE001 - reported as this request's error
                with self._lock:
                    self._connect_failures += 1
                raise _ConnectFailed(exc, (self._clock() - started) * 1000.0) from None
        return client

    def __call__(self, index: int):
        try:
            client = self._client()
        except _ConnectFailed as failed:
            return TransportResult(
                status=None, latency_ms=failed.elapsed_ms, body_snippet="",
                error=f"connect failed: {failed}",
            )
        return post(
            payload_for_index(self._pool, index),
            self._url,
            client=client,
            timeout=self._timeout,
            clock=self._clock,
            wire_format=self._wire_format,
        )

    def connection_summary(self) -> Dict[str, Any]:
        if self._fixed is not None:
            return {"keepalive": self._keepalive, "client": "injected"}
        if not self._keepalive:
            return {
                "keepalive": False,
                "note": "one connection per request; setup time is inside every latency",
            }
        with self._lock:
            clients = list(self._clients)
            failures = self._connect_failures
        firsts = [c.connect_seconds[0] * 1000.0 for c in clients if c.connect_seconds]
        return {
            "keepalive": True,
            "workers_connected": len(firsts),
            "connections_opened": sum(len(c.connect_seconds) for c in clients),
            "reconnects": sum(c.reconnects for c in clients),
            "connect_failures": failures,
            "setup_ms": {
                "min": round(min(firsts), 3) if firsts else None,
                "p50": round(barrage_report.percentile(firsts, 0.5), 3) if firsts else None,
                "max": round(max(firsts), 3) if firsts else None,
            },
        }

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.close()


def make_send_fn(
    pool: List[InboundEmail],
    url: str,
    client=None,
    clock: Callable[[], float] = time.monotonic,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    wire_format=None,
    keepalive: bool = True,
) -> _Sender:
    """Build the runner's send hook. `client` is None in real use; tests
    inject a fake, which is then shared by every worker."""
    return _Sender(
        pool, url, client=client, clock=clock, timeout=timeout,
        wire_format=wire_format, keepalive=keepalive,
    )


def samples_from_records(result: RunResult, warmup: float) -> List[barrage_report.Sample]:
    """The runner's records as report samples, with times relative to the
    start of the run."""
    origin = result.started_at
    samples = []
    for r in result.records:
        completed = r.outcome == "completed"
        res = r.result if completed else None
        service = getattr(res, "latency_ms", None) if completed else None
        if completed and service is None and r.dispatched_at is not None:
            service = (r.completed_at - r.dispatched_at) * 1000.0
        samples.append(barrage_report.Sample(
            index=r.index,
            scheduled_at=r.scheduled_at - origin,
            dispatched_at=None if r.dispatched_at is None else r.dispatched_at - origin,
            status=getattr(res, "status", None) if completed else None,
            service_ms=service,
            offered_ms=(r.completed_at - r.scheduled_at) * 1000.0 if completed else None,
            target_rate=r.target_rate,
            outcome=r.outcome,
            in_hold=(r.scheduled_at - origin) >= warmup - 1e-9,
        ))
    return samples


def write_artifact(path: Optional[str], artifact: Dict[str, Any]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")


def format_dry_run_preview(
    plan: RunPlan,
    seed: int,
    pool_size: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    keepalive: bool = True,
) -> str:
    """What a dry run prints instead of firing: exactly what would be sent and
    how, and that nothing was."""
    scheduled = len(arrival_schedule(plan.stages))
    if plan.mode == "open":
        model = [
            "    arrivals follow a fixed schedule whatever the target does; a request",
            f"    that waits more than {plan.max_queue_seconds:g}s for a free worker is "
            "dropped, not sent",
        ]
        count = f"  scheduled requests: {scheduled}"
    else:
        model = [
            f"    each of the {plan.concurrency} worker(s) sends its next request only after",
            "    the previous one completes, so offered load is at most",
            f"    min({plan.rate:g}/s, {plan.concurrency} / latency)",
        ]
        count = f"  requests: at most about {scheduled}"
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
        *model,
        f"  target rate: {plan.rate:g}/s",
        f"  warmup ramp: {plan.warmup_seconds:g}s, steady-state hold: {plan.hold_seconds:g}s",
        f"  concurrency: {plan.concurrency}",
        count,
        f"  approx requests at steady state: {round(plan.rate * plan.hold_seconds)}",
        f"  run deadline: {plan.duration_seconds + plan.grace_seconds:g}s "
        f"(duration + {plan.grace_seconds:g}s grace); anything still in flight then is abandoned",
        f"  per-request timeout: {timeout:g}s; connections: "
        + ("kept alive, one per worker" if keepalive else "a new one per request"),
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
    *,
    timeout: float = DEFAULT_TIMEOUT,
    keepalive: bool = True,
    allow_public_target: bool = False,
    executor=None,
) -> int:
    """The shared send path for `fire` and `replay`. Returns an exit code:
    EXIT_OK for a run that completed, EXIT_FINDING for one cut short by Ctrl-C
    (the artifact is still written, with `aborted: true`).

    Everything that can refuse does so before the first request: the payload
    pool's synthetic check, the timeout, the target resolution, the ceiling.
    """
    check_timeout(timeout)
    pool = build_payload_pool(seed, pool_size)
    require_synthetic_pool(pool)
    resolved = resolve_target(
        target_name, config_path,
        allow_public_target=allow_public_target, load_tool=BARRAGE,
    )
    sender = make_send_fn(
        pool, resolved.url, client=client, clock=clock, timeout=timeout,
        wire_format=resolved.wire_format, keepalive=keepalive,
    )
    owned = None
    if executor is None and client is None:
        check_rate_ceiling(  # before starting any thread
            plan.rate, plan.duration_seconds, allow_high_rate=allow_high_rate,
            concurrency=plan.concurrency,
        )
        # Real sends always run on pool threads, even with one worker, so the
        # main thread never blocks on a socket: the run ends at its deadline
        # and Ctrl-C lands in the runner's wait, not inside a request.
        executor = owned = PoolExecutor(plan.concurrency)
    try:
        result = run(
            plan, sender, clock=clock, sleep=sleep,
            allow_high_rate=allow_high_rate, executor=executor,
        )
    finally:
        if owned is not None:
            owned.shutdown(cancel_pending=True)
        sender.close()

    artifact = barrage_report.build_artifact(
        seed,
        {
            **run_config(
                plan, seed, pool_size, target_name, dry_run=False, timeout=timeout,
                keepalive=keepalive,
            ),
            **resolved.artifact_config(),
        },
        samples_from_records(result, plan.warmup_seconds),
        stages=[(s.rate, s.duration) for s in plan.stages],
        hold_start=plan.warmup_seconds,
        origin=0.0,
        aborted=result.aborted,
    )
    artifact["dispatch"] = dispatch_summary(result, plan)
    artifact["connections"] = sender.connection_summary()
    printer(barrage_report.format_summary(artifact))
    note = format_dispatch_note(artifact["dispatch"])
    if note:
        printer(note)
    write_artifact(out, artifact)
    if result.aborted:
        printer(
            "barrage: interrupted; "
            + (f"wrote the partial run to {out} (aborted: true)" if out else
               "pass --out to keep a partial run")
        )
        return EXIT_FINDING
    return EXIT_OK


#: A request held back longer than this for a free worker counts as queued.
#: Above thread start-up jitter, which measured up to 2.4ms on a healthy run.
QUEUED_THRESHOLD_SECONDS = 0.010


def dispatch_summary(result: RunResult, plan: RunPlan) -> Dict[str, Any]:
    """How the run's workers kept up: queue waits for a free worker as a
    distribution, and what the deadline and the backlog bound cut."""
    waits = [r.queued for r in result.records if r.dispatched_at is not None]
    waits_ms = [w * 1000.0 for w in waits]

    def pct(fraction):
        value = barrage_report.percentile(waits_ms, fraction)
        return None if value is None else round(value, 3)

    return {
        "mode": plan.mode,
        "concurrency": plan.concurrency,
        "grace_seconds": plan.grace_seconds,
        "max_queue_seconds": plan.max_queue_seconds,
        "queued_requests": sum(1 for w in waits if w > QUEUED_THRESHOLD_SECONDS),
        "queue_wait_ms": {
            "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
            "max": round(max(waits_ms), 3) if waits_ms else None,
        },
        "dropped_backlog": result.count("dropped_backlog"),
        "not_dispatched": result.count("not_dispatched"),
        "abandoned": result.count("abandoned"),
    }


def format_dispatch_note(dispatch: Dict[str, Any]) -> str:
    """Warning lines when the workers or the deadline shaped the run, and
    nothing when they did not. Silence is the normal case."""
    lines = []
    queued = dispatch.get("queued_requests") or 0
    if queued:
        worst = (dispatch.get("queue_wait_ms") or {}).get("max")
        lines.append(
            f"dispatch: {queued} request(s) waited for a free worker, up to "
            f"{worst:g}ms. All {dispatch.get('concurrency')} workers were busy, so "
            "the schedule was not held and part of the shortfall is this tool's. "
            "Raise --concurrency."
        )
    if dispatch.get("dropped_backlog"):
        lines.append(
            f"dispatch: {dispatch['dropped_backlog']} request(s) waited longer than "
            f"{dispatch.get('max_queue_seconds'):g}s for a worker and were dropped, not sent."
        )
    if dispatch.get("abandoned") or dispatch.get("not_dispatched"):
        lines.append(
            f"dispatch: the run deadline cut {dispatch.get('abandoned', 0)} request(s) "
            f"still in flight (abandoned) and {dispatch.get('not_dispatched', 0)} never "
            f"sent (not dispatched). Raise --grace to wait longer."
        )
    return "\n".join(lines)
