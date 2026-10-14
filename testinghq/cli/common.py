from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from ..core.config import ConfigError, UnknownKeyError, load_config
from ..core.exit_codes import EXIT_REFUSED, MEANINGS as _EXIT_MEANINGS
from ..core.targets import ResolvedTarget, resolve_target
from ..pipeline import adapters as pipeline_adapters
from ..pipeline import common
from ..pipeline.adapters import AdapterError

DEFAULT_TARGET_CONFIG = 'target.toml'
DEFAULT_RATE = 5.0


#: Shown by every subcommand's --help, because the exit code is the answer a
#: script reads and there are ten commands. Generated from the shared table so it
#: cannot describe a convention the code no longer uses.
_EXIT_CODE_HELP = (
    "exit codes: "
    + "; ".join(
        f"{code} {text}" for code, text in sorted(_EXIT_MEANINGS.items())
    )
)

class _Parser(argparse.ArgumentParser):
    """Argparse, with one change: a usage error exits 1, not 2.

    Argparse exits 2 for a bad command line, and in this package 2 is a dry
    run. So `testinghq blast fire --typo` would report "ran, sent nothing" to
    any script checking whether the tool did something, which is exactly the
    misreading the shared exit-code convention exists to prevent. A command
    line the tool cannot parse is a refusal to do what was asked.

    Argparse itself is not modified: `--help` and `--version` still exit 0, and
    anything else in the ecosystem that uses argparse keeps its standard
    behaviour. Only this parser's usage errors change.
    """

    def error(self, message):  # pragma: no cover - exercised via parse_args
        self.print_usage(sys.stderr)
        self.exit(
            EXIT_REFUSED,
            f"{self.prog}: error: {message}\n"
            f"({_EXIT_CODE_HELP})\n",
        )


def _resolve_readback(args):
    """Build the ReadbackConfig for whichever pipeline command is running.

    Precedence is flag over config file. A custom adapter still gets the config
    file's `[readback]` table: the flag says HOW to read the system and the file
    says WHAT its url, field names and timeout are, and a factory handed only a
    spec has nothing to connect to.

    A missing adapter is a refusal, not a default. There is no sensible
    fallback, and silently proceeding without a readback would produce a
    verification run that verified nothing and said nothing, which is the exact
    false green this package was built to eliminate.
    """
    spec = getattr(args, "readback", None)
    override = bool(getattr(args, "allow_public_readback", False))
    config_path = getattr(args, "config", DEFAULT_TARGET_CONFIG)

    try:
        config = load_config(config_path)
    except UnknownKeyError:
        # A typo in the file is refused here too, rather than read as "no
        # config" and reported as a missing readback.
        raise
    except ConfigError:
        config = None
    raw = config.readback_table() if config is not None else None

    if spec:
        raw = dict(raw) if isinstance(raw, dict) else {}
        raw["kind"] = spec
    elif raw is None:
        raise AdapterError(
            "no readback adapter configured. Pass --readback http, --readback "
            "mailbox, or --readback module:attribute, or declare a [readback] "
            "table in the target config."
        )
    if override and isinstance(raw, dict):
        # The flag wins over the file in both directions it can be read: a
        # table that says false does not cancel a flag that says true.
        raw = dict(raw)
        raw["allow_public_hosts"] = True
    readback = pipeline_adapters.parse_readback_config(raw)

    changes = {}
    rate = getattr(args, "readback_rate", None)
    if rate is not None:
        changes["rate"] = rate
    if config is not None:
        changes["config_dir"] = str(Path(config_path).resolve().parent)
    return dataclasses.replace(readback, **changes) if changes else readback


def _add_readback_poll_args(parser) -> None:
    """How long to keep asking the system what it produced.

    A fixed sleep before looking once is wrong in both directions: too short and
    a queue consumer's output is reported as a loss, too long and every run pays
    for the slowest pipeline that ever was. Polling until what it sees stops
    changing is the version that adapts, and it watches the record counts and
    field values rather than "did it create anything", so a duplicate that
    lands late is still caught. The flags sit in their own help group whose
    description is `common.SETTLING_RULE`, the one statement of when the poll
    ends and what a tag still missing then means.
    """
    group = parser.add_argument_group("readback polling", common.SETTLING_RULE)
    group.add_argument(
        "--quiet-window",
        type=float,
        default=common.DEFAULT_QUIET_WINDOW,
        help=(
            "seconds what the readback sees (which tags are found, their "
            "record counts and the field values the checks read) must hold "
            "steady before the read is settled; a tag still not found then is "
            "reported as lost (default %(default)s). For a slow or async "
            "pipeline, raise it above the pipeline's worst processing latency"
        ),
    )
    group.add_argument(
        "--max-wait",
        type=float,
        default=common.DEFAULT_MAX_WAIT,
        help=(
            "seconds to keep polling (default %(default)s). A run whose "
            "readback is still changing then is UNSETTLED, not lost. Keep it "
            "well above --quiet-window"
        ),
    )
    group.add_argument(
        "--poll-interval",
        type=float,
        default=common.DEFAULT_POLL_INTERVAL,
        help="seconds between lookups while waiting",
    )


def _add_readback_args(parser) -> None:
    """The arguments every pipeline tool needs to find a system to read.

    `--readback` is how the adapter is chosen, and it has three shapes because
    there are three honest options: a built-in kind, an import path for a
    custom adapter, or nothing, in which case the `[readback]` table in the
    target config is used. The precedence is flag over config, and a flag that
    names an import path is the only way to bring your own, because a Python
    object cannot be spelled on a command line.
    """

    def positive_rate(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not value > 0:
            raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
        return value

    parser.add_argument(
        "--readback",
        help=(
            "how to read the system under test: 'http', 'mailbox', or "
            "'module:attribute' for a custom adapter. Overrides the "
            "[readback] table in the target config."
        ),
    )
    parser.add_argument(
        "--allow-public-readback",
        action="store_true",
        help=(
            "allow the readback URL to be a public host. Refused by default, "
            "because on a real deployment the readback is a ticket store or a "
            "mail sink that may hold other people's data"
        ),
    )
    parser.add_argument(
        "--readback-rate",
        type=positive_rate,
        default=pipeline_adapters.DEFAULT_READBACK_RATE,
        help=(
            "most readback requests per second, across every lookup, page and "
            "retry (default %(default)s). Vendor APIs allow 40 to 700 requests "
            "a minute, and an unpaced poll over many tags exceeds that"
        ),
    )


#: Default for `resolve_for_send(target_name=...)`: read `--target` from args.
_FROM_ARGS = object()


def add_send_args(parser) -> None:
    """Add `--allow-public-target` to a command that can send."""
    parser.add_argument(
        "--allow-public-target",
        action="store_true",
        help=(
            "allow firing at a public host: one that is not a reserved test "
            "domain, an internal name or a private address, and does not "
            "resolve only to private addresses. Refused by default. Only for "
            "an endpoint you own. barrage also needs public = true on the "
            "target's config table"
        ),
    )


def resolve_for_send(args, tool: str, *, target_name=_FROM_ARGS) -> ResolvedTarget:
    """Resolve the target a sending command will fire at, or raise.

    Reads `--target`, `--config` and `--allow-public-target` from `args`.
    `target_name` overrides `--target` (replay takes it from the artifact).
    `tool` is the command name; "barrage" applies the double opt-in rule.
    Raises `GuardrailError` or `ConfigError`.
    """
    name = getattr(args, "target", None) if target_name is _FROM_ARGS else target_name
    return resolve_target(
        name,
        getattr(args, "config", DEFAULT_TARGET_CONFIG),
        allow_public_target=bool(getattr(args, "allow_public_target", False)),
        load_tool=tool,
    )
