from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from ..barrage import fire as barrage_fire
from ..barrage.runner import RateCeilingError
from ..compare import runs as compare_runs
from ..core import guardrails
from ..core.config import ConfigError
from .common import DEFAULT_TARGET_CONFIG

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
    b_fire.add_argument("--target")
    b_fire.add_argument("--seed", type=int, default=barrage_fire.DEFAULT_SEED)
    b_fire.add_argument(
        "--rate", type=float, default=barrage_fire.DEFAULT_RATE,
        help="target requests per second",
    )
    b_fire.add_argument(
        "--duration", type=float, default=barrage_fire.DEFAULT_DURATION,
        help="total run duration in seconds, including the warmup ramp",
    )
    b_fire.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "requests in flight at once. Both modes dispatch through a pool of "
            "this many worker threads, so one is a serial send. Each worker is "
            "an interpreter thread, so a high value is a memory cost. Open "
            "mode's is a ceiling on what is outstanding rather than a worker "
            "count, because its arrivals are on a schedule"
        ),
    )
    b_fire.add_argument(
        "--mode", choices=["open", "closed"], default=barrage_fire.DEFAULT_MODE,
        help="open-loop (fixed arrival rate) or closed-loop (fixed concurrency)",
    )
    b_fire.add_argument(
        "--warmup", type=float, default=barrage_fire.DEFAULT_WARMUP,
        help="seconds of ramp before steady state, taken out of --duration",
    )
    b_fire.add_argument(
        "--pool-size", type=int, default=barrage_fire.DEFAULT_POOL_SIZE,
        help="how many distinct seeded payloads to cycle through",
    )
    b_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_fire.add_argument(
        "--allow-high-rate", action="store_true",
        help=(
            "raise the hard safety ceiling on rate and duration. This exists "
            "so a mistake cannot become a self-inflicted denial of service; "
            "pass it only deliberately, for a target you own"
        ),
    )
    b_fire.add_argument("--out", help="path to write the run artifact JSON")
    b_fire.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    b_replay = barrage_sub.add_parser("replay", help="re-run a saved barrage run")
    b_replay.add_argument("run", help="path to a previously written barrage artifact JSON")
    b_replay.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_replay.add_argument(
        "--allow-high-rate", action="store_true",
        help="raise the hard safety ceiling on rate and duration",
    )
    b_replay.add_argument("--out", help="path to write the run artifact JSON")
    b_replay.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    # compare. Note the absence of --target, --send and --config, which is the
    # point: it reads two files and compares them. There is no network path to
    # gate, so there is no gate. Unlike blast and barrage there is no
    # subcommand, because there is exactly one thing it does.
    compare = sub.add_parser(
        "compare",
        help="compare a baseline and a candidate run artifact and report what changed",
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


def _barrage_execute(args, plan, seed: int, pool_size: int, target_name: Optional[str]) -> int:
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
        )
    except (
        guardrails.GuardrailError,
        ConfigError,
        RateCeilingError,
        barrage_fire.BarrageError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED


def _cmd_barrage_fire(args) -> int:
    # There is no longer a refusal of --concurrency here. It was refused in both
    # modes because Barrage had no executor, so the flag could not mean
    # anything; that was issue #38 and it is fixed. A run now dispatches
    # through a pool of plan.concurrency workers, and the flag is the one
    # control that decides how much load is in flight at once.
    #
    # The cap that remains is the rate-and-duration ceiling, checked below,
    # and the number of workers is bounded by the thread cost rather than by a
    # separate rule: see testinghq/barrage/executor.py on why a high
    # concurrency is a memory cost and not a free setting.
    #
    # Arguments are validated before anything is announced, so a refusal does
    # not follow a line describing what the command was about to do.
    try:
        plan = barrage_fire.build_plan(
            args.mode, args.rate, args.duration, args.concurrency, args.warmup
        )
        # The ceiling is checked here too, not only inside run(), so a
        # dry run reports an over-limit plan as refused instead of
        # cheerfully previewing a run that would never be allowed.
        barrage_fire.check_rate_ceiling(
            args.rate, args.duration, allow_high_rate=args.allow_high_rate
        )
    except (RateCeilingError, barrage_fire.BarrageError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    # Announced only now that the plan is known to be legal. See the note at
    # the top of this function.
    decision = guardrails.evaluate_send(args.send)
    print(f"barrage fire: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(plan, args.seed, args.pool_size))
        return barrage_fire.EXIT_DRY_RUN

    return _barrage_execute(args, plan, args.seed, args.pool_size, args.target)


def _cmd_barrage_replay(args) -> int:
    try:
        data = json.loads(Path(args.run).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"barrage replay: could not read {args.run!r}: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED
    except json.JSONDecodeError as exc:
        print(f"barrage replay: {args.run!r} is not valid JSON: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    seed = data.get("seed")
    config = data.get("config") or {}
    required = ("mode", "rate", "duration", "warmup", "concurrency", "pool_size")
    if seed is None or any(config.get(key) is None for key in required):
        print(
            f"barrage replay: {args.run!r} is missing seed or config "
            f"{required}, cannot reproduce the run",
            file=sys.stderr,
        )
        return barrage_fire.EXIT_REFUSED

    try:
        plan = barrage_fire.build_plan(
            config["mode"],
            config["rate"],
            config["duration"],
            config["concurrency"],
            config["warmup"],
        )
    except (barrage_fire.BarrageError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    decision = guardrails.evaluate_send(args.send)
    print(f"barrage replay: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(plan, seed, config["pool_size"]))
        return barrage_fire.EXIT_DRY_RUN

    return _barrage_execute(args, plan, seed, config["pool_size"], config.get("target"))


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
        baseline = compare_runs.load_artifact(args.baseline)
        candidate = compare_runs.load_artifact(args.candidate)
    except compare_runs.CompareError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return compare_runs.EXIT_USAGE

    diff = compare_runs.compare(baseline, candidate)
    print(compare_runs.format_diff(diff))

    if args.out:
        Path(args.out).write_text(
            json.dumps(diff, indent=2, sort_keys=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")

    if args.fail_on_regression and diff["regressed"]:
        return compare_runs.EXIT_REGRESSION
    return compare_runs.EXIT_NO_REGRESSION
