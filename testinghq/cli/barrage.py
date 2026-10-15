from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from ..barrage import fire as barrage_fire
from ..barrage.payloads import DEFAULT_POOL_SIZE
from ..barrage.runner import RateCeilingError
from ..compare import load as compare_load
from ..compare import runs as compare_runs
from ..core import guardrails
from ..core.config import ConfigError
from ..core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_REFUSED
from .common import DEFAULT_TARGET_CONFIG, add_send_args, positive_number


def _add_send_shape_args(parser) -> None:
    """How each request is sent and when the run gives up. Recorded in the
    artifact, so `replay` sends the same way."""
    parser.add_argument(
        "--timeout", type=float, default=barrage_fire.DEFAULT_TIMEOUT,
        help="per-request timeout in seconds (default %(default)s)",
    )
    parser.add_argument(
        "--grace", type=float, default=barrage_fire.DEFAULT_GRACE_SECONDS,
        help=(
            "seconds after --duration that in-flight requests may finish; the run "
            "ends then and anything still out is recorded as abandoned "
            "(default %(default)s)"
        ),
    )
    parser.add_argument(
        "--max-queue-seconds", type=float, default=barrage_fire.DEFAULT_MAX_QUEUE_SECONDS,
        help=(
            "open loop: a request that waited this long for a free worker is "
            "recorded as dropped_backlog and not sent (default %(default)s)"
        ),
    )
    parser.add_argument(
        "--no-keepalive", action="store_true",
        help="open a new connection per request instead of one kept-alive connection per worker",
    )


def _add_barrage_parser(sub) -> None:
    """The `barrage` subcommand: load-test a configured target.

    Barrage fires clean, provider-shaped payloads at an endpoint the
    operator controls, at a high but controlled rate, and reports how the
    pipeline held. It is NOT an email sender, NOT a flooding tool, and NOT
    for endpoints you do not own. Dry-run is the default here exactly as it
    is for blast: --send is required to put anything on the wire.
    """
    barrage = sub.add_parser(
        "barrage",
        help="load-test a configured target with clean payloads at a controlled rate",
    )
    barrage_sub = barrage.add_subparsers(dest="command", required=True)

    b_fire = barrage_sub.add_parser("fire", help="run a load test against a configured target")
    b_fire.add_argument("--target", help="name of a [targets.<name>] table in --config")
    b_fire.add_argument(
        "--seed", type=int, default=barrage_fire.DEFAULT_SEED,
        help="payload pool seed (default %(default)s)",
    )
    b_fire.add_argument(
        "--rate", type=positive_number, default=barrage_fire.DEFAULT_RATE,
        help="target requests per second (default %(default)s)",
    )
    b_fire.add_argument(
        "--duration", type=float, default=barrage_fire.DEFAULT_DURATION,
        help="total run duration in seconds, including the warmup ramp (default %(default)s)",
    )
    b_fire.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "worker threads, so requests in flight at once (default "
            f"{barrage_fire.DEFAULT_CONCURRENCY}). Capped at 64 unless "
            "--allow-high-rate is passed"
        ),
    )
    b_fire.add_argument(
        "--mode", choices=["open", "closed"], default=barrage_fire.DEFAULT_MODE,
        help=(
            "open: arrivals on a fixed schedule whatever the target does. "
            "closed: each worker sends its next request only after the previous "
            "one completes, so offered load is min(rate, concurrency / latency) "
            "(default %(default)s)"
        ),
    )
    b_fire.add_argument(
        "--warmup", type=float, default=barrage_fire.DEFAULT_WARMUP,
        help="seconds of ramp before steady state, taken out of --duration (default %(default)s)",
    )
    b_fire.add_argument(
        "--pool-size", type=int, default=DEFAULT_POOL_SIZE,
        help="how many distinct seeded payloads to cycle through (default %(default)s)",
    )
    _add_send_shape_args(b_fire)
    b_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_fire.add_argument(
        "--allow-high-rate", action="store_true",
        help=(
            "raise the hard safety ceiling on rate, duration and concurrency. "
            "Pass it only deliberately, for a target you own"
        ),
    )
    b_fire.add_argument("--out", help="path to write the run artifact JSON")
    b_fire.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )
    add_send_args(b_fire)

    b_replay = barrage_sub.add_parser(
        "replay",
        help="re-run a saved barrage run with the same schedule and send settings",
    )
    b_replay.add_argument("run", help="path to a previously written barrage artifact JSON")
    b_replay.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_replay.add_argument(
        "--allow-high-rate", action="store_true",
        help="raise the hard safety ceiling on rate, duration and concurrency",
    )
    b_replay.add_argument("--out", help="path to write the run artifact JSON")
    b_replay.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )
    add_send_args(b_replay)

    # compare. Note the absence of --target, --send and --config, which is the
    # point: it reads two files and compares them. There is no network path to
    # gate, so there is no gate. Unlike blast and barrage there is no
    # subcommand, because there is exactly one thing it does.
    compare = sub.add_parser(
        "compare",
        help="compare a baseline and a candidate run artifact and report what changed",
        description=(
            "Compare two blast runs payload by payload, or two barrage runs by "
            "load metrics. A barrage candidate regressed when "
            + compare_load.describe_thresholds() + "."
        ),
    )
    compare.add_argument(
        "--baseline", required=True, help="path to the baseline run artifact JSON"
    )
    compare.add_argument(
        "--candidate", required=True, help="path to the candidate run artifact JSON"
    )
    compare.add_argument("--out", help="path to write the difference report as JSON")
    compare.add_argument(
        "--fail-on-regression",
        action="store_true",
        help=(
            "exit non-zero when the candidate introduced a regression. Off by "
            "default so a plain comparison is just a comparison; turn it on in "
            "CI, where a non-zero exit should stop something"
        ),
    )


def _barrage_execute(args, plan, seed: int, pool_size: int, target_name: Optional[str],
                     timeout: float, keepalive: bool) -> int:
    """Run the barrage send path, translating every refusal into an exit
    code rather than a traceback."""
    try:
        return barrage_fire.execute(
            plan,
            seed,
            pool_size,
            target_name,
            args.config,
            args.out,
            allow_high_rate=args.allow_high_rate,
            timeout=timeout,
            keepalive=keepalive,
            allow_public_target=args.allow_public_target,
        )
    except (
        guardrails.GuardrailError,
        ConfigError,
        RateCeilingError,
        barrage_fire.BarrageError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        # Only reachable before the run starts or after it ended; the run
        # itself turns Ctrl-C into a partial artifact.
        print("barrage: interrupted", file=sys.stderr)
        return EXIT_FINDING


def _cmd_barrage_fire(args) -> int:
    # Validated before anything is announced, so a refusal does not follow a
    # line describing what the command was about to do.
    try:
        plan = barrage_fire.build_plan(
            args.mode, args.rate, args.duration, args.concurrency, args.warmup,
            grace=args.grace, max_queue_seconds=args.max_queue_seconds,
        )
        barrage_fire.check_timeout(args.timeout)
        # Checked here too, not only inside run(), so a dry run reports an
        # over-limit plan as refused instead of previewing it.
        barrage_fire.check_rate_ceiling(
            args.rate, args.duration, allow_high_rate=args.allow_high_rate,
            concurrency=plan.concurrency,
        )
    except (RateCeilingError, barrage_fire.BarrageError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    keepalive = not args.no_keepalive
    decision = guardrails.evaluate_send(args.send)
    print(f"barrage fire: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(
            plan, args.seed, args.pool_size, timeout=args.timeout, keepalive=keepalive,
        ))
        return EXIT_DRY_RUN

    return _barrage_execute(
        args, plan, args.seed, args.pool_size, args.target, args.timeout, keepalive,
    )


def _cmd_barrage_replay(args) -> int:
    try:
        data = json.loads(Path(args.run).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"barrage replay: could not read {args.run!r}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except json.JSONDecodeError as exc:
        print(f"barrage replay: {args.run!r} is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        plan, seed, pool_size, target, options = barrage_fire.plan_from_artifact(data)
        barrage_fire.check_timeout(options["timeout"])
    except barrage_fire.BarrageError as exc:
        print(f"barrage replay: {args.run!r}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    decision = guardrails.evaluate_send(args.send)
    print(f"barrage replay: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(
            plan, seed, pool_size, timeout=options["timeout"], keepalive=options["keepalive"],
        ))
        return EXIT_DRY_RUN

    return _barrage_execute(
        args, plan, seed, pool_size, target, options["timeout"], options["keepalive"],
    )


def _cmd_compare(args) -> int:
    """Compare two saved run artifacts and report what changed.

    Kept thin on purpose, like the barrage handlers: all of the comparison
    logic lives in `testinghq/compare/runs.py`, so this is only argument
    plumbing, file reading, output, and the exit code.

    Note what is absent. There is no `evaluate_send` call, no target
    resolution, no config load and no transport. This function cannot put bytes
    on a wire, which is why it has no dry-run mode and no guardrail gate: the
    gate would have nothing to gate. Every refusal here is about a malformed
    input file, not about a request.
    """
    try:
        diff = compare_runs.compare_paths(args.baseline, args.candidate)
    except compare_runs.CompareError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return compare_runs.EXIT_USAGE

    print(compare_runs.format_any(diff))

    code = compare_runs.record_exit_code(diff, args.fail_on_regression)
    if args.out:
        Path(args.out).write_text(
            json.dumps(diff, indent=2, sort_keys=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")
    return code
