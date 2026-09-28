from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config_doc
from .. import reporting
from ..core.exit_codes import EXIT_OK, EXIT_REFUSED
from .common import DEFAULT_TARGET_CONFIG, _EXIT_CODE_HELP

def _add_report_parser(sub) -> None:
    """`report`, which reads a run artifact and sends nothing.

    No `--send` and no dry-run mode, for the same reason `compare` has neither:
    it cannot reach the network, so there is nothing to gate. A command that
    looks like it might transmit should not exist, and this one demonstrably
    cannot.
    """
    report_cmd = sub.add_parser(
        "report",
        help="summarize a run artifact from any tool, in text or JSON",
        epilog=_EXIT_CODE_HELP,
    )
    report_cmd.add_argument(
        "artifact",
        help="path to a run artifact written by blast, barrage, verify, "
             "ledger, redeliver, loop or compare",
    )
    report_cmd.add_argument(
        "--json",
        action="store_true",
        help="print the stable machine-readable summary instead of text. One "
             "shape for every tool, so a CI system has a single thing to parse.",
    )


def _cmd_report(args) -> int:
    """Read one run artifact and print a summary. Sends nothing.

    An artifact this cannot identify is refused with a message saying what the
    top-level keys were, because the person holding a mystery JSON file is
    usually trying to establish whether it is a run artifact at all.
    """
    try:
        summary = reporting.load(Path(args.artifact))
    except reporting.ArtifactError as exc:
        print(f"report: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.json:
        print(json.dumps(summary.to_json(), indent=2, sort_keys=True))
    else:
        print(reporting.render(summary))
    return EXIT_OK


def _add_config_parser(sub) -> None:
    """`config validate`, which loads a file and sends nothing.

    No `--send`, no `--target`, no dry-run mode. It cannot reach the network, so
    a mode that suggests it might would be a lie, and a config tool that could
    transmit is a config tool nobody would paste an untrusted file into.
    """
    config_cmd = sub.add_parser(
        "config",
        help="work with configuration files",
    )
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)

    validate_cmd = config_sub.add_parser(
        "validate",
        help="check a config file and print the config the loaders resolved",
        description=(
            "Loads a config file with the real loaders, so a file that "
            "validates is a file the tools accept. Sends nothing. Header "
            "values are never printed: they are shown as env:NAME, so the "
            "output is safe to paste into an issue."
        ),
        epilog=_EXIT_CODE_HELP,
    )
    validate_cmd.add_argument(
        "config_file",
        nargs="?",
        default=DEFAULT_TARGET_CONFIG,
        help=f"path to the config file (default: {DEFAULT_TARGET_CONFIG})",
    )
    validate_cmd.add_argument(
        "--toml",
        action="store_true",
        help="print the effective config as TOML instead of as a report, so "
             "it can be diffed against the file it came from",
    )


def _cmd_config_validate(args) -> int:
    """Validate a config file. Sends nothing, prints no header value."""
    try:
        targets, readback, notes = config_doc.validate(Path(args.config_file))
    except config_doc.ValidationError as exc:
        print(f"config validate: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.toml:
        print(config_doc.render_effective(targets, readback), end="")
        return EXIT_OK

    print(f"config: {args.config_file}")
    print(f"targets: {len(targets)}")
    for name, target in targets.items():
        print(f"  {name}: {target['url']} (format {target['format']})")

    if readback is None:
        print("readback: none configured")
    else:
        print(f"readback: kind {readback['kind']}")
        if readback.get("url"):
            print(f"  url: {readback['url']}")
        if readback.get("path"):
            print(f"  path: {readback['path']}")
        for name, value in (readback.get("headers") or {}).items():
            print(f"  header {name}: {value} (value not shown)")

    for note in notes:
        print(f"note: {note}")

    return EXIT_OK
