from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path
from typing import Optional

from ..core import guardrails
from ..core.config import ConfigError, load_config
from ..core.exit_codes import EXIT_REFUSED, MEANINGS as _EXIT_MEANINGS
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


def _not_yet(command):
    """Fallback for a subcommand argparse accepted but main() has no handler
    for. Every current subcommand is implemented, so reaching this means the
    parser and the dispatcher disagree, which is a bug in this file rather than
    a missing feature. The message says so instead of describing a milestone
    that finished long ago.

    Returns EXIT_REFUSED, not EXIT_DRY_RUN. A dry run is a successful run that
    was asked to hold back, so a script reads 2 as "nothing was sent, all well".
    What actually happened is that the tool could not do what was asked because
    its own parser and dispatcher disagree, and that is the one code in this
    table that means "fix the tool". Returning 2 hid a bug behind a code that
    reads as a deliberate choice.
    """
    print(
        f"testinghq blast {command}: no handler is wired up for this "
        f"subcommand, which is a bug (argparse offered it anyway)",
        file=sys.stderr,
    )
    return EXIT_REFUSED


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
    for the slowest pipeline that ever was. Polling until the counts stop moving
    is the version that adapts, and the counts rather than the messages are what
    it waits on, so a duplicate that lands late is still caught.
    """
    parser.add_argument(
        "--quiet-window",
        type=float,
        default=common.DEFAULT_QUIET_WINDOW,
        help=(
            "seconds the records (their count and the field values the "
            "checks read) must hold steady before a read is believed "
            "(default %(default)s). For an async pipeline make it longer than "
            "the gap between a record being created and being filled in"
        ),
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=common.DEFAULT_MAX_WAIT,
        help=(
            "seconds to keep polling (default %(default)s). A run that has not "
            "settled by then is UNSETTLED, not lost. A search-indexed store "
            "such as Zendesk or Freshdesk needs about 300"
        ),
    )
    parser.add_argument(
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
    parser.add_argument(
        "--expect-route",
        help=(
            "the route every message should have been routed to, checked "
            "against what the system recorded. Without it, the expected route "
            "is the message's own recipient, and with no notion of routing the "
            "check reports as not-checked rather than passing"
        ),
    )


def _resolve_target_url(target_name: Optional[str], config_path: str) -> str:
    """Load the target config and resolve `target_name` to a URL, enforcing
    the canonical guardrail twice: once on the configured name (the
    allow-list check) and once on the resolved URL (the public-host check),
    mirroring web/adapter.py's pattern. A bare target name has no dot, so
    the public-host check on the name alone would pass vacuously; checking
    the resolved URL too is what makes that hardening actually bite.
    """
    if not target_name:
        raise guardrails.GuardrailError(
            "refusing to fire: --send requires --target"
        )
    config = load_config(config_path)
    allowed = config.allowed_target_names()
    guardrails.require_configured_target(target_name, allowed)
    url = config.get(target_name).url
    guardrails.require_configured_target(url, (url,))
    return url
