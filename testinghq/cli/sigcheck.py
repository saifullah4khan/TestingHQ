"""`testinghq sigcheck fire`: does the endpoint check the webhook signature?"""
from __future__ import annotations

import os
import sys

from ..core import guardrails
from ..pipeline import sigcheck as pipeline_sigcheck
from ..pipeline.common import EXIT_DRY_RUN, EXIT_REFUSED
from .common import _EXIT_CODE_HELP, DEFAULT_TARGET_CONFIG, add_send_args


def add_sigcheck_parser(sub) -> None:
    sigcheck = sub.add_parser(
        "sigcheck",
        help="check the endpoint enforces the provider's webhook authentication",
        description="Send one request per authentication variant (valid, "
                    "wrong-secret, unsigned, stale-timestamp, tampered-body, "
                    "replayed-token) and report which the endpoint accepted.",
        epilog=_EXIT_CODE_HELP,
    )
    sigcheck_sub = sigcheck.add_subparsers(dest="command", required=True)

    fire = sigcheck_sub.add_parser(
        "fire",
        help="send every variant once and judge each answer",
        description="Dry run unless --send. The secret is read from the "
                    "environment variable named by --secret env:NAME or "
                    "[sigcheck].secret; a literal secret is refused.",
        epilog=_EXIT_CODE_HELP,
    )
    fire.add_argument("--target", help="the configured target to send to")
    fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG,
                      help="target config file (default %(default)s)")
    fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    fire.add_argument(
        "--scheme", choices=list(pipeline_sigcheck.known_schemes()), default=None,
        help="the provider's authentication scheme. Overrides [sigcheck].scheme",
    )
    fire.add_argument(
        "--secret", default=None, metavar="env:NAME",
        help="where the secret is: the Mailgun signing key, the SendGrid ECDSA "
             "private key, or Postmark's user:password. Overrides [sigcheck].secret",
    )
    fire.add_argument(
        "--window", type=float, default=None,
        help="the timestamp window in seconds the endpoint is expected to enforce. "
             f"Overrides [sigcheck].timestamp_window_seconds (default "
             f"{pipeline_sigcheck.DEFAULT_WINDOW:g})",
    )
    fire.add_argument("--out", help="path to write the sigcheck artifact JSON")
    add_send_args(fire)


def _cmd_sigcheck_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)
    try:
        section = pipeline_sigcheck.load_sigcheck_config(
            args.config,
            required=decision.will_send or os.path.exists(args.config),
        )
        settings = pipeline_sigcheck.effective_settings(
            section, scheme=args.scheme, secret=args.secret, window=args.window
        )
    except pipeline_sigcheck.SigcheckError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        secret = pipeline_sigcheck.resolve_secret(settings)
        missing = None
    except pipeline_sigcheck.SigcheckError as exc:
        secret, missing = None, str(exc)

    print(f"sigcheck fire: {decision.reason}")
    if not decision.will_send:
        try:
            print(pipeline_sigcheck.preview(settings, secret, args.target, missing))
        except pipeline_sigcheck.SigcheckError as exc:
            print(f"refused: the secret in {settings.secret_ref}: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        return EXIT_DRY_RUN
    if missing is not None:
        print(f"refused: {missing}", file=sys.stderr)
        return EXIT_REFUSED

    return pipeline_sigcheck.execute(
        scheme_name=settings.scheme,
        secret=secret,
        secret_ref=settings.secret_ref,
        target_name=args.target,
        config_path=args.config,
        out=args.out,
        window=settings.window,
        allow_public_target=args.allow_public_target,
    )


def run(args) -> int:
    """Dispatch for `testinghq sigcheck`."""
    if args.command == "fire":
        return _cmd_sigcheck_fire(args)
    raise SystemExit(EXIT_REFUSED)  # pragma: no cover - argparse requires a command
