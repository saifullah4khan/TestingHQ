from __future__ import annotations

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
    DEFAULT_TARGET_CONFIG,
    _add_readback_args,
    _add_readback_poll_args,
    _resolve_readback,
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
        "--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX,
        help=(
            "prefix for the per-message tag. Change it when two runs hit the "
            "same system at once, so neither reads the other's records"
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
    l_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    l_fire.add_argument("--out", help="path to write the ledger artifact JSON")
    l_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
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
    r_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    r_fire.add_argument("--out", help="path to write the redelivery artifact JSON")
    r_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(r_fire)
    _add_readback_poll_args(r_fire)


def _add_loop_parser(sub) -> None:
    """The `loop` subcommand.

    Reads two things back: what the pipeline produced, and what it tried to send.
    The second is a separate adapter configured in `[loop.outbound]`, and it is
    optional on purpose. Without it the auto-reply check reports SKIPPED rather
    than passing, because a tool that cannot see whether a reply was sent has no
    evidence that one was not.
    """
    loop = sub.add_parser(
        "loop",
        help="auto-reply and mail-loop detection",
    )
    loop_sub = loop.add_subparsers(dest="command", required=True)

    l_fire = loop_sub.add_parser(
        "fire",
        help="send machine-generated mail and check the pipeline ignored it",
    )
    l_fire.add_argument("--target")
    l_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    l_fire.add_argument(
        "--count", type=int, default=12,
        help="how many machine-generated messages to send",
    )
    l_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    l_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    l_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    l_fire.add_argument("--out", help="path to write the loop artifact JSON")
    l_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    l_fire.add_argument(
        "--ticket-policy", choices=list(pipeline_loop.TICKET_POLICIES),
        default=pipeline_loop.TICKET_POLICY_NONE,
        help=(
            "whether opening a ticket for machine mail is a finding. 'none' "
            "reports every one; 'allowed' makes them informational and keeps "
            "only the auto-reply check. The loop bait is a finding under both"
        ),
    )
    l_fire.add_argument(
        "--reply-address",
        default=pipeline_loop.DEFAULT_REPLY_ADDRESS,
        help=(
            "the pipeline's own reply address, so the loop bait aims at "
            "something real. Read from [loop].reply_address when set here"
        ),
    )
    _add_readback_args(l_fire)
    _add_readback_poll_args(l_fire)


def _add_steady_parser(sub) -> None:
    """The `steady` subcommand.

    `--max-flip-rate` is the gate, and it exists so the tool can sit in CI. The
    default is generous: a classifier that is 6% unstable is worth a
    conversation, not a red build.

    `--label-field` names the readback field that carries the label: `route`
    (the default), `category` or `priority`. The report says which field the
    number is about, because a flip rate with no field named is a number nobody
    can act on.

    Deliberately without `--allow-high-rate`. This sends a few hundred variants
    at a rate the operator chose; sustained load is Barrage's job, with the
    ceiling already in place.
    """
    steady = sub.add_parser(
        "steady",
        help="metamorphic stability testing for an AI triage classifier",
    )
    steady_sub = steady.add_subparsers(dest="command", required=True)

    s_fire = steady_sub.add_parser(
        "fire",
        help="send meaning-preserving variants and measure how often the label moves",
    )
    s_fire.add_argument("--target")
    s_fire.add_argument("--seed", type=int, default=pipeline_steady.DEFAULT_SEED)
    s_fire.add_argument(
        "--count", type=int, default=6,
        help="how many intent families to use from the reviewed fixture",
    )
    s_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    s_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    s_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    s_fire.add_argument(
        "--repeats", type=int, default=1,
        help=(
            "send the same payload this many times to measure model "
            "nondeterminism separately from sensitivity to wording"
        ),
    )
    s_fire.add_argument(
        "--label-field", choices=list(pipeline_steady.LABEL_FIELDS),
        default=pipeline_steady.LABEL_FIELD,
        help=(
            "which readback field carries the classifier's label. A run that "
            "cannot see the chosen field exits 3 with nothing measured"
        ),
    )
    s_fire.add_argument(
        "--transform", action="append",
        choices=list(pipeline_steady.TRANSFORM_NAMES),
        help="only apply this transform; repeatable. Default is all of them",
    )
    s_fire.add_argument(
        "--max-flip-rate", type=float, default=pipeline_steady.DEFAULT_MAX_FLIP_RATE,
        help="exit 3 above this flip rate, so the tool can gate CI",
    )
    s_fire.add_argument("--out", help="path to write the stability artifact JSON")
    s_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(s_fire)
    _add_readback_poll_args(s_fire)


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
    )


def _cmd_loop_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
        # `core/config.py` is owned elsewhere, so the `[loop]` table is read by
        # the tool. One read, reused for the outbound adapter and the reply
        # address, rather than parsing the file twice.
        section = pipeline_loop.load_loop_config(args.config)
        outbound = pipeline_loop.build_outbound_config(section, args.config)
        reply_address = section.get("reply_address") or args.reply_address
    except (AdapterError, pipeline_loop.LoopConfigError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    corpus = pipeline_loop.build_machine_corpus(
        args.seed, args.count, args.tag_prefix, reply_address
    )

    print(f"loop fire: {decision.reason}")
    if not decision.will_send:
        print(
            pipeline_loop.format_dry_run(corpus, readback, args.ticket_policy)
        )
        return EXIT_DRY_RUN

    return pipeline_loop.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        outbound=outbound,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        ticket_policy=args.ticket_policy,
        reply_address=reply_address,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )


def _cmd_steady_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        families = pipeline_steady.build_families(
            args.seed,
            intents=pipeline_steady.load_intents()[: max(args.count, 1)],
            transforms=args.transform,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"refused: the intent fixture could not be read: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"steady fire: {decision.reason}")
    if not decision.will_send:
        print(pipeline_steady.format_dry_run(families, readback, args.label_field))
        return EXIT_DRY_RUN

    return pipeline_steady.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=args.tag_prefix,
        label_field=args.label_field,
        transforms=args.transform,
        repeats=args.repeats,
        max_flip_rate=args.max_flip_rate,
        rate=args.rate,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
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
    )
