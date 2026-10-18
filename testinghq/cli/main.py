"""`build_parser` and `main`: the assembly and the dispatch table.

Every other module in this package is one command's parser and its handlers.
This is the only module that knows which commands exist. `compare` is a
top-level command whose parser is built in barrage.py.
"""
from __future__ import annotations

from .. import __version__
from . import barrage as barrage_module
from . import blast as blast_module
from . import pipeline as pipeline_module
from . import sigcheck as sigcheck_module
from . import utility as utility_module
from .common import _EXIT_CODE_HELP, _Parser


def build_parser():
    parser = _Parser(
        prog="testinghq",
        description="Self-testing tools for intake pipelines.",
        epilog=_EXIT_CODE_HELP,
    )
    parser.add_argument(
        "--version", action="version", version=f"testinghq {__version__}"
    )
    sub = parser.add_subparsers(dest="tool", required=True)

    blast_module.add_blast_parser(sub)
    barrage_module._add_barrage_parser(sub)
    pipeline_module._add_verify_parser(sub)
    pipeline_module._add_ledger_parser(sub)
    pipeline_module._add_loop_parser(sub)
    pipeline_module._add_redeliver_parser(sub)
    pipeline_module._add_steady_parser(sub)
    sigcheck_module.add_sigcheck_parser(sub)
    utility_module._add_report_parser(sub)
    utility_module._add_config_parser(sub)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tool == "config":
        if args.config_command == "validate":
            return utility_module._cmd_config_validate(args)

    if args.tool == "report":
        return utility_module._cmd_report(args)

    if args.tool == "blast":
        if args.command == "generate":
            return blast_module._cmd_generate(args)
        if args.command == "fire":
            return blast_module._cmd_fire(args)
        if args.command == "replay":
            return blast_module._cmd_replay(args)

    if args.tool == "barrage":
        if args.command == "fire":
            return barrage_module._cmd_barrage_fire(args)
        if args.command == "replay":
            return barrage_module._cmd_barrage_replay(args)

    if args.tool == "compare":
        return barrage_module._cmd_compare(args)

    if args.tool == "steady":
        if args.command == "fire":
            return pipeline_module._cmd_steady_fire(args)

    if args.tool == "sigcheck":
        return sigcheck_module.run(args)

    if args.tool == "redeliver":
        if args.command == "fire":
            return pipeline_module._cmd_redeliver_fire(args)

    if args.tool == "loop":
        if args.command == "fire":
            return pipeline_module._cmd_loop_fire(args)

    if args.tool == "ledger":
        if args.command == "fire":
            return pipeline_module._cmd_ledger_fire(args)

    if args.tool == "verify":
        if args.command == "fire":
            return pipeline_module._cmd_verify_fire(args)
        if args.command == "check":
            return pipeline_module._cmd_verify_check(args)

    # Every subparser is required and every choice is handled above, so argparse
    # refuses an unknown command before this line.
    parser.error(f"no handler for {args.tool!r}")
