from __future__ import annotations

import os
import sys
from typing import Optional

from ..core import guardrails
from ..core.config import ConfigError
from ..pipeline import ledger as pipeline_ledger
from ..pipeline import loop as pipeline_loop
from ..pipeline import messages as pipeline_messages
from ..pipeline import redeliver as pipeline_redeliver
from ..pipeline import steady as pipeline_steady
from ..pipeline import verify as pipeline_verify
from ..pipeline.adapters import AdapterError
from ..pipeline.common import EXIT_DRY_RUN, EXIT_REFUSED
from .common import (
    _EXIT_CODE_HELP,
    DEFAULT_TARGET_CONFIG,
    _add_readback_args,
    _add_readback_poll_args,
    _resolve_readback,
    add_send_args,
)

def _add_verify_parser(sub) -> None:
    """The `verify` subcommand, in two forms.

    `fire` sends and reads back. `check` reads back a run that already
    happened, so it has no `--send` and no dry-run mode, because it cannot
    reach the network and there is nothing to gate. The same absence `compare`
    has, for the same reason.

    `--allow-high-rate` is deliberately absent. This is a correctness tool
    sending a few hundred messages at a rate the operator chose; the thing that
    needs a hard ceiling is sustained load, and that is Barrage's job, with the
    ceiling already in place.
    """
    verify = sub.add_parser(
        "verify",
        help="check what the pipeline actually produced, not what it answered",
    )
    verify_sub = verify.add_subparsers(dest="command", required=True)

    v_fire = verify_sub.add_parser(
        "fire", help="fire the clean corpus, then read back what the system made of it"
    )
    v_fire.add_argument("--target")
    v_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    v_fire.add_argument("--count", type=int, default=pipeline_verify.DEFAULT_COUNT)
    v_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    v_fire.add_argument(
        "--rate", type=float, default=pipeline_verify.DEFAULT_RATE,
        help="max requests per second",
    )
    v_fire.add_argument(
        "--tag-prefix", default=pipeline_messages.run_tag_prefix(),
        help=(
            "prefix for the per-message tag. The default is drawn per run "
            "(hq-<yymmddHHMM>-<4 hex>), so no two runs share tags; pass a "
            "fixed value to reproduce an earlier run's tags"
        ),
    )
    v_fire.add_argument(
        "--body-exact", action="store_true",
        help=(
            "require the body to match exactly rather than contain the "
            "substantive text. Off by default because a pipeline that reads the "
            "HTML part returns an equivalent body, not an identical one"
        ),
    )
    v_fire.add_argument("--out", help="path to write the verification artifact JSON")
    v_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    add_send_args(v_fire)
    _add_readback_args(v_fire)
    _add_readback_poll_args(v_fire)

    v_check = verify_sub.add_parser(
        "check",
        help="read back a run that already happened, sending nothing",
    )
    v_check.add_argument("run", help="path to a run artifact written by `verify fire`")
    v_check.add_argument(
        "--tag-prefix", default=None,
        help="override the tag prefix recorded in the artifact",
    )
    v_check.add_argument("--body-exact", action="store_true")
    v_check.add_argument("--out", help="path to write the verification artifact JSON")
    v_check.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(v_check)
    _add_readback_poll_args(v_check)


def _add_ledger_parser(sub) -> None:
    """The `ledger` subcommand.

    Deliberately without `--allow-high-rate`, for the same reason verify has
    none: this is a correctness tool sending a few hundred messages at a rate
    the operator chose. Sustained load is Barrage's job, with the ceiling
    already in place.
    """
    ledger = sub.add_parser(
        "ledger",
        help="exactly-once accounting: did we lose any messages?",
    )
    ledger_sub = ledger.add_subparsers(dest="command", required=True)

    l_fire = ledger_sub.add_parser(
        "fire", help="send N uniquely tagged messages and reconcile them"
    )
    l_fire.add_argument("--target")
    l_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    l_fire.add_argument(
        "--count", type=int, default=pipeline_ledger.DEFAULT_COUNT,
        help="how many uniquely tagged messages to send",
    )
    l_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    l_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    l_fire.add_argument("--tag-prefix", default=pipeline_messages.run_tag_prefix())
    l_fire.add_argument("--out", help="path to write the ledger artifact JSON")
    l_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    add_send_args(l_fire)
    _add_readback_args(l_fire)
    _add_readback_poll_args(l_fire)


def _add_redeliver_parser(sub) -> None:
    """The `redeliver` subcommand.

    Not an email sender and not a way to hammer an endpoint: it sends a couple
    of dozen payloads, paced, at infrastructure the operator owns, to find out
    what the pipeline does when a provider misbehaves.
    """
    redeliver = sub.add_parser(
        "redeliver",
        help="test delivery semantics: retries, duplicates, and out-of-order replies",
    )
    redeliver_sub = redeliver.add_subparsers(dest="command", required=True)

    r_fire = redeliver_sub.add_parser(
        "fire", help="run the redelivery scenarios and check for duplicate tickets"
    )
    r_fire.add_argument("--target")
    r_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    r_fire.add_argument(
        "--count", type=int, default=pipeline_redeliver.DEFAULT_MESSAGES,
        help="how many messages the duplicate scenarios redeliver",
    )
    r_fire.add_argument(
        "--scenario", action="append", choices=list(pipeline_redeliver.SCENARIOS),
        help="run only this scenario; repeatable. Default is all of them",
    )
    r_fire.add_argument(
        "--retry-after", type=float, default=pipeline_redeliver.DEFAULT_RETRY_AFTER,
        help=(
            "seconds before the slow-retry redelivery. Deduplication windows "
            "vary widely, so the default is a starting point to adjust rather "
            "than a value that suits every pipeline"
        ),
    )
    r_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    r_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    r_fire.add_argument("--tag-prefix", default=pipeline_messages.run_tag_prefix())
    r_fire.add_argument("--out", help="path to write the redelivery artifact JSON")
    r_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    add_send_args(r_fire)
    _add_readback_args(r_fire)
    _add_readback_poll_args(r_fire)


def _drop_option(parser, option: str) -> None:
    """Remove `option` from `parser` when a shared helper added it. loop and
    steady do not honour `--expect-route`, so they must not accept it."""
    action = parser._option_string_actions.get(option)
    if action is None:
        return
    parser._remove_action(action)
    for name in action.option_strings:
        parser._option_string_actions.pop(name, None)
    for group in parser._action_groups:
        if action in group._group_actions:
            group._group_actions.remove(action)


def _readback_for_dry_run(args):
    """(readback config or None, refusal message or None). A dry run sends
    nothing and reads nothing, so a missing readback is reported, not refused."""
    try:
        return _resolve_readback(args), None
    except (AdapterError, ConfigError) as exc:
        return None, str(exc)


def _add_loop_parser(sub) -> None:
    """The `loop` subcommand. Reads two things back: what the pipeline filed,
    and, through `[loop.outbound]`, what it sent. Without the second the
    auto-reply check is NOT CHECKED rather than passed."""
    loop = sub.add_parser(
        "loop",
        help="auto-reply and mail-loop detection",
        description="Send machine-generated mail and a loop bait, then check the "
                    "pipeline neither ticketed nor answered it.",
        epilog=_EXIT_CODE_HELP,
    )
    loop_sub = loop.add_subparsers(dest="command", required=True)

    l_fire = loop_sub.add_parser(
        "fire",
        help="send machine-generated mail and check the pipeline ignored it",
        description="Send an ordinary message, read the pipeline's reply to it, "
                    "then send machine mail and an auto-reply to that reply. "
                    "Dry run unless --send.",
        epilog=_EXIT_CODE_HELP,
    )
    l_fire.add_argument("--target", help="the configured target to send to")
    l_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED,
                        help="corpus seed (default %(default)s)")
    l_fire.add_argument(
        "--count", type=int, default=pipeline_loop.DEFAULT_COUNT,
        help="machine-generated messages to send; the default covers every "
             "shape once (default %(default)s)",
    )
    l_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    l_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE,
                        help="messages per second (default %(default)s)")
    l_fire.add_argument("--tag-prefix", default=pipeline_messages.run_tag_prefix(),
                        help="tag prefix (default: unique per run)")
    l_fire.add_argument("--out", help="path to write the loop artifact JSON")
    l_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG,
                        help="target config file (default %(default)s)")
    l_fire.add_argument(
        "--ticket-policy", choices=list(pipeline_loop.TICKET_POLICIES), default=None,
        help="whether a ticket for machine mail is a finding: 'none' (default) "
             "or 'allowed'. Overrides [loop].ticket_policy. The loop bait is a "
             "finding under both",
    )
    l_fire.add_argument(
        "--reply-address", default=None,
        help="the pipeline's own reply address, which the loop bait is sent to. "
             f"Overrides [loop].reply_address (default "
             f"{pipeline_loop.DEFAULT_REPLY_ADDRESS})",
    )
    l_fire.add_argument(
        "--ack-wait", type=float, default=pipeline_loop.DEFAULT_ACK_WAIT,
        help="seconds to wait for the pipeline's reply to the ordinary message "
             "before the bait falls back to a fabricated parent (default %(default)s)",
    )
    add_send_args(l_fire)
    _add_readback_args(l_fire)
    _add_readback_poll_args(l_fire)
    _drop_option(l_fire, "--expect-route")


def _add_steady_parser(sub) -> None:
    """The `steady` subcommand. `--max-dissent-rate` is the gate, so the tool
    can sit in CI; `--max-flip-rate` adds a second gate on the pairwise rate.
    Without `--allow-high-rate`: sustained load is Barrage's job."""
    steady = sub.add_parser(
        "steady",
        help="metamorphic stability testing for an AI triage classifier",
        description="Send meaning-preserving variants of reviewed intents and "
                    "measure how often the classifier's label moves.",
        epilog=_EXIT_CODE_HELP,
    )
    steady_sub = steady.add_subparsers(dest="command", required=True)

    s_fire = steady_sub.add_parser(
        "fire",
        help="send meaning-preserving variants and measure how often the label moves",
        description="Dry run unless --send.",
        epilog=_EXIT_CODE_HELP,
    )
    s_fire.add_argument("--target", help="the configured target to send to")
    s_fire.add_argument("--seed", type=int, default=pipeline_steady.DEFAULT_SEED,
                        help="seed (default %(default)s)")
    s_fire.add_argument(
        "--count", type=int, default=pipeline_steady.DEFAULT_COUNT,
        help="intent families to use from the fixture (default %(default)s)",
    )
    s_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    s_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE,
                        help="messages per second (default %(default)s)")
    s_fire.add_argument("--tag-prefix", default=pipeline_messages.run_tag_prefix(),
                        help="tag prefix (default: unique per run)")
    s_fire.add_argument(
        "--repeats", type=int, default=1,
        help="send one payload per family this many times, to measure "
             "nondeterminism apart from wording (default %(default)s)",
    )
    s_fire.add_argument(
        "--label-field", choices=list(pipeline_steady.LABEL_FIELDS), default=None,
        help="the readback field carrying the classifier's label. Overrides "
             "[steady].label_field; with neither, 'route' is read and a run "
             "where it cannot vary is refused",
    )
    s_fire.add_argument(
        "--transform", action="append",
        choices=list(pipeline_steady.TRANSFORM_NAMES),
        help="only apply this transform; repeatable. Default is all of them",
    )
    s_fire.add_argument(
        "--max-dissent-rate", type=float,
        default=pipeline_steady.DEFAULT_MAX_DISSENT_RATE,
        help="exit 3 above this majority-dissent rate (default %(default)s)",
    )
    s_fire.add_argument(
        "--max-flip-rate", type=float, default=None,
        help="also exit 3 above this pairwise flip rate (default: not gated)",
    )
    s_fire.add_argument("--out", help="path to write the stability artifact JSON")
    s_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG,
                        help="target config file (default %(default)s)")
    add_send_args(s_fire)
    _add_readback_args(s_fire)
    _add_readback_poll_args(s_fire)
    _drop_option(s_fire, "--expect-route")


def _cmd_verify_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    corpus = pipeline_verify.build_tagged_corpus(
        args.seed, args.count, args.tag_prefix
    )

    print(f"verify fire: {decision.reason}")
    if not decision.will_send:
        print(pipeline_verify.format_dry_run(corpus, readback, args.tag_prefix))
        return EXIT_DRY_RUN

    return pipeline_verify.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        route=args.expect_route,
        body_exact=args.body_exact,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        allow_public_target=args.allow_public_target,
    )


def _cmd_verify_check(args) -> int:
    """Read back a finished run. No `--send`, because this path cannot put
    anything on a wire."""
    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    return pipeline_verify.check_saved_run(
        args.run,
        args.config,
        args.out,
        readback=readback,
        route=args.expect_route,
        body_exact=args.body_exact,
        tag_prefix=args.tag_prefix,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )


def _cmd_ledger_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"ledger fire: {decision.reason}")
    if not decision.will_send:
        print(
            pipeline_ledger.format_dry_run(
                args.count, args.seed, args.tag_prefix, readback
            )
        )
        return EXIT_DRY_RUN

    return pipeline_ledger.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        route=args.expect_route,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        allow_public_target=args.allow_public_target,
    )


def _cmd_loop_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)
    try:
        section = pipeline_loop.load_loop_config(args.config, required=decision.will_send)
        policy, reply_address = pipeline_loop.effective_settings(
            section, flag_policy=args.ticket_policy, flag_reply=args.reply_address
        )
        outbound = pipeline_loop.build_outbound_config(section, args.config)
        tag_prefix = args.tag_prefix
        corpus = pipeline_loop.build_machine_corpus(args.seed, args.count, tag_prefix)
    except (AdapterError, pipeline_loop.LoopConfigError, ConfigError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    readback, missing = _readback_for_dry_run(args)
    if missing is not None and decision.will_send:
        print(f"refused: {missing}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"loop fire: {decision.reason}")
    if not decision.will_send:
        bait = pipeline_loop.build_loop_bait(
            args.seed, args.count, tag_prefix, reply_address, None
        )
        print(pipeline_loop.format_dry_run(
            [*corpus, bait], readback, policy, reply_address, outbound
        ))
        return EXIT_DRY_RUN

    return pipeline_loop.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        outbound=outbound,
        tag_prefix=tag_prefix,
        rate=args.rate,
        ticket_policy=policy,
        reply_address=reply_address,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        ack_wait=args.ack_wait,
        allow_public_target=args.allow_public_target,
    )


def _cmd_steady_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)
    try:
        section = pipeline_steady.load_steady_config(
            args.config if decision.will_send or os.path.exists(args.config) else None
        )
    except pipeline_steady.SteadyConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    label_field = args.label_field or section.get("label_field")
    intents_path = section.get("intents_path")
    tag_prefix = args.tag_prefix

    readback, missing = _readback_for_dry_run(args)
    if missing is not None and decision.will_send:
        print(f"refused: {missing}", file=sys.stderr)
        return EXIT_REFUSED

    if not decision.will_send:
        if args.count < 1:
            print(f"refused: --count must be >= 1, got {args.count}", file=sys.stderr)
            return EXIT_REFUSED
        try:
            families = pipeline_steady.load_families(
                args.seed, args.count, args.transform, intents_path
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"refused: the intent fixture could not be read: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"steady fire: {decision.reason}")
        print(pipeline_steady.format_dry_run(families, readback, label_field, args.transform))
        return EXIT_DRY_RUN

    print(f"steady fire: {decision.reason}")
    return pipeline_steady.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=tag_prefix,
        label_field=label_field,
        transforms=args.transform,
        repeats=args.repeats,
        max_dissent_rate=args.max_dissent_rate,
        max_flip_rate=args.max_flip_rate,
        intents_path=intents_path,
        rate=args.rate,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        allow_public_target=args.allow_public_target,
    )


def _cmd_redeliver_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        scenarios = pipeline_redeliver.build_scenarios(
            args.seed, args.count, args.tag_prefix, args.retry_after, args.scenario
        )
    except pipeline_redeliver.ScenarioError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"redeliver fire: {decision.reason}")
    if not decision.will_send:
        print(pipeline_redeliver.format_dry_run(scenarios, readback, args.tag_prefix))
        return EXIT_DRY_RUN

    return pipeline_redeliver.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        scenarios=args.scenario,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        retry_after=args.retry_after,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        allow_public_target=args.allow_public_target,
    )
