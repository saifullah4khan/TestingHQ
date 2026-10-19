from __future__ import annotations

import base64
import getpass
import json
import re
import sys
from pathlib import Path

from .. import config_doc
from .. import report_html
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
        help="summarize a run artifact from any tool, in text, JSON or HTML",
        epilog=_EXIT_CODE_HELP,
    )
    report_cmd.add_argument(
        "artifact",
        help="path to a run artifact written by any testinghq tool",
    )
    report_cmd.add_argument(
        "--json",
        action="store_true",
        help="print the stable machine-readable summary instead of text. One "
             "shape for every tool, so a CI system has a single thing to parse.",
    )
    report_cmd.add_argument(
        "--html",
        metavar="OUT",
        help="also write a self-contained HTML report to OUT: verdict, counts, "
             "findings, records, Barrage charts and config, with no external "
             "resources. The text or JSON summary is still printed.",
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

    if args.html:
        try:
            # Read again for the parts the summary leaves out: records,
            # buckets and config. load() above already refused a bad file.
            raw = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
            report_html.write_html(Path(args.html), summary.to_json(), raw)
        except OSError as exc:
            print(f"report: could not write {args.html}: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"report: wrote {args.html}", file=sys.stderr)

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

    basic_cmd = config_sub.add_parser(
        "basic-auth",
        help="print an HTTP Basic Authorization value to put in .env",
        description=(
            "Builds the value of an Authorization header for HTTP Basic auth, "
            "'Basic <base64 of user:password>', for a target behind it: put "
            "the printed line in .env and name the variable in "
            "[targets.<name>.headers], as Authorization = \"env:NAME\". The "
            "password is read from the terminal without echo, twice, or from "
            "the first line of standard input when that is not a terminal. "
            "It is never an argument, because arguments end up in shell "
            "history and in the process list. Sends nothing."
        ),
        epilog=_EXIT_CODE_HELP,
    )
    basic_cmd.add_argument("user", help="the Basic auth user name")
    basic_cmd.add_argument(
        "--env",
        default=DEFAULT_AUTH_VARIABLE,
        metavar="NAME",
        help="the environment variable the printed line sets (default "
             "%(default)s)",
    )


#: The variable `config basic-auth` names when not told another.
DEFAULT_AUTH_VARIABLE = "TARGET_AUTH"

_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def basic_auth_value(user: str, password: str) -> str:
    """`Basic <base64 of user:password>`, the user and password UTF-8 encoded
    (RFC 7617). Raises ValueError for a user name holding a colon, which the
    scheme cannot carry: the first colon is where the password begins."""
    if ":" in user:
        raise ValueError(
            f"the user name {user!r} holds a colon, which Basic auth cannot "
            "carry: the first colon is where the password begins"
        )
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _read_password(stdin=None, prompt=getpass.getpass) -> str:
    """The password, from the terminal (asked twice, not echoed) or, when
    standard input is not a terminal, from its first line."""
    stdin = sys.stdin if stdin is None else stdin
    if not stdin.isatty():
        line = stdin.readline()
        if not line:
            raise ValueError("no password on standard input")
        return line.rstrip("\r\n")
    first = prompt("Password: ")
    second = prompt("Password (again): ")
    if first != second:
        raise ValueError("the two passwords do not match")
    return first


def _cmd_config_basic_auth(args, stdin=None, prompt=getpass.getpass) -> int:
    """Print `NAME=Basic <base64>` for .env, and on stderr how to use it."""
    if not _VARIABLE_NAME.match(args.env):
        print(
            f"config basic-auth: {args.env!r} is not an environment variable name",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        password = _read_password(stdin, prompt)
        if not password:
            raise ValueError("the password is empty")
        value = basic_auth_value(args.user, password)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        reason = str(exc) or "no password given"
        print(f"config basic-auth: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    # Quoted, because .env.example loads .env with `set -a && . ./.env`, and
    # unquoted the shell reads `NAME=Basic <token>` as an assignment of
    # `Basic` followed by a command named after the token. Base64 holds no
    # character that a double quote would let the shell expand.
    print(f'{args.env}="{value}"')
    print(
        f"config basic-auth: add the line above to .env, and in the target's "
        f"config:\n  [targets.<name>.headers]\n  Authorization = \"env:{args.env}\"",
        file=sys.stderr,
    )
    return EXIT_OK


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
        print(f"  {config_doc.describe_target(name, target)}")

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
