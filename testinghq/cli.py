"""TestingHQ command line interface.

M0 skeleton: the argument surface and subcommand dispatch exist and are tested.
The generate, fire, and replay commands are wired to placeholders that the fleet
fills in across M1 to M3.
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import guardrails


def build_parser():
    parser = argparse.ArgumentParser(
        prog="testinghq",
        description="Self-testing tools for intake pipelines.",
    )
    parser.add_argument(
        "--version", action="version", version=f"testinghq {__version__}"
    )
    sub = parser.add_subparsers(dest="tool", required=True)

    blast = sub.add_parser("blast", help="generate and fire inbound-email payloads")
    blast_sub = blast.add_subparsers(dest="command", required=True)

    gen = blast_sub.add_parser("generate", help="generate a corpus to disk, no network")
    gen.add_argument("--count", type=int, default=100)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--out", default="corpus")

    fire = blast_sub.add_parser("fire", help="generate and send to a configured target")
    fire.add_argument("--target")
    fire.add_argument("--seed", type=int, default=0)
    fire.add_argument("--count", type=int, default=100)
    fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )

    replay = blast_sub.add_parser("replay", help="re-fire a saved run exactly")
    replay.add_argument("run")

    return parser


def _not_yet(command):
    print(
        f"testinghq blast {command}: not implemented yet (M0 skeleton)",
        file=sys.stderr,
    )
    return 2


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tool == "blast":
        if args.command == "fire":
            decision = guardrails.evaluate_send(args.send)
            print(f"fire: {decision.reason}")
            return _not_yet("fire")
        return _not_yet(args.command)
    return _not_yet(args.tool)


if __name__ == "__main__":
    raise SystemExit(main())
