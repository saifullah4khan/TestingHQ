"""Validate a config file, and generate the configuration reference from the schema.

Two things live here because they are the same walk over the same schema.
`validate` loads a file with the REAL loaders and renders what those loaders made
of it. `render_reference` writes docs/CONFIG.md from the schema alone, with no
file to validate.

The split is the whole design. Validation is done by `load_config` and
`parse_readback_config` because those are what will run, so a config that
validates is a config the tools accept. A validator that checked a file against a
description of the file would happily pass a config the tools then refuse.

The schema is therefore used only for rendering, and
`tests/unit/test_config_schema.py` checks the schema against the dataclass
defaults, so the rendering cannot quietly describe a config the loaders would
reject.

Two safety properties, both of which a config tool has to have:

It sends nothing. There is no code path in this module that constructs an HTTP
client.

It never prints a secret. `[readback.headers]` is resolved to find out whether
the variables are set, and the render shows `env:NAME` rather than the value. A
`config validate` whose output is safe to paste into an issue is the only way it
is useful when something is wrong with your config.

Run directly to regenerate the reference:

    python -m testinghq.config_doc            # write docs/CONFIG.md
    python -m testinghq.config_doc --check    # exit 1 if it is out of date
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import config_schema
from .core.config import ConfigError, Target, load_config
from .pipeline.adapters import AdapterError, ReadbackConfig, parse_readback_config

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = Path("docs") / "CONFIG.md"


class ValidationError(RuntimeError):
    """Raised for a config that would not load."""


# ---------------------------------------------------------------------------
# Validating
# ---------------------------------------------------------------------------


def validate(path: Path) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], List[str]]:
    """Load a config file. Returns (targets, readback, notes).

    Refuses on the first loader error rather than accumulating, because
    `load_config` stops at the first malformed table anyway, and a partial
    validation is a claim about a file nobody can act on.

    The readback is resolved against the process environment, so a
    `[readback.headers]` naming an unset variable is caught here at validate
    time. That is the point: the alternative is discovering it mid-run, after
    having sent something.
    """
    path = Path(path)
    try:
        config = load_config(path)
    except ConfigError as exc:
        raise ValidationError(str(exc)) from None

    notes: List[str] = []

    readback_raw = config.readback_table()
    readback_effective: Optional[Dict[str, Any]] = None
    if readback_raw is None:
        notes.append(
            "no [readback] table. blast, barrage and compare do not need one. "
            "verify, ledger and redeliver refuse to run without it."
        )
    else:
        try:
            readback = parse_readback_config(readback_raw, env=os.environ)
        except AdapterError as exc:
            raise ValidationError(str(exc)) from None
        readback_effective = _render_readback(readback, readback_raw, notes)

    return (
        {name: _render_target(t) for name, t in config.targets.items()},
        readback_effective,
        notes,
    )


def _render_target(target: Target) -> Dict[str, Any]:
    """One target as the loaders resolved it.

    `wire_format` is read with getattr rather than as an attribute because it
    arrives with the wire-format layer, which is a separate stack. On a base
    without that layer the attribute is absent, and the fallback is the value
    the field would have defaulted to anyway, so the render is correct either
    way rather than merely not crashing.
    """
    return {
        "url": target.url,
        "format": getattr(target, "wire_format", "sendgrid"),
    }


def _render_readback(
    readback: ReadbackConfig,
    readback_raw: Dict[str, Any],
    notes: List[str],
) -> Dict[str, Any]:
    """The effective readback config, with headers reduced to env references.

    `ReadbackConfig.to_json` already reports header NAMES only, which is the safe
    direction. This goes one better and shows the variable each name is bound
    to, which is what an operator needs to debug a missing credential and is
    still not the value.

    The variable name has to come from the raw table, not from the resolved
    config. Once resolved, the config holds the secret and the mapping to the
    variable it came from is gone: `header_names()` returns `Authorization`, and
    rendering that as `env:Authorization` would be a confident lie, naming a
    variable that does not exist. The raw table is where `env:HQ_TOKEN` is
    still written down, and the resolved config is what proves it was set.
    """
    payload = readback.to_json()

    raw_headers = readback_raw.get("headers")
    bindings: Dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for name in readback.header_names():
            declared = raw_headers.get(name)
            if isinstance(declared, str) and declared.startswith("env:"):
                bindings[name] = declared
            else:  # pragma: no cover - the loader refuses anything else
                bindings[name] = f"env:<{name}>"
    payload["headers"] = bindings


    if readback.allow_public_hosts:
        notes.append(
            "allow_public_hosts is set, so this readback URL is permitted on a "
            "public host. On a real deployment that URL is often a ticket store "
            "holding other people's data. Consider leaving it off and passing "
            "--allow-public-readback per run instead, so the decision is "
            "re-made every time."
        )
    if not readback.enumerate_records:
        notes.append(
            "enumerate is false, so stray records will not be looked for. The "
            "report will say so rather than claiming there were none."
        )
    if readback.kind == "http" and readback.headers:
        notes.append(
            f"{len(readback.headers)} readback header(s) resolved from the "
            "environment. Values are not shown, here or in any artifact."
        )
    return payload


# ---------------------------------------------------------------------------
# Rendering the effective config as TOML
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{k} = {_toml_value(v)}" for k, v in sorted(value.items())
        ) + " }"
    return '"' + str(value).replace('"', '\\"') + '"'


def render_effective(
    targets: Dict[str, Dict[str, Any]],
    readback: Optional[Dict[str, Any]],
) -> str:
    """The effective config, as TOML an operator could paste back.

    Regenerated from what the loaders produced rather than from the file, so it
    shows what will actually run: defaults filled in, the environment URL
    override applied, and header values replaced by their `env:` references.
    """
    lines: List[str] = [
        "# Effective configuration, as the loaders resolved it.",
        "# Header values are shown as env:NAME and never as the value.",
        "",
    ]

    for name, target in targets.items():
        lines.append(f"[targets.{name}]")
        for key in ("url", "format"):
            if key in target:
                lines.append(f"{key} = {_toml_value(target[key])}")
        lines.append("")

    if readback is not None:
        lines.append("[readback]")
        for key, value in readback.items():
            if value in (None, {}, []):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The generated reference
# ---------------------------------------------------------------------------

_HEADER = """<!-- GENERATED FILE. Do not edit by hand. -->
<!-- Produced by `python -m testinghq.config_doc`, from the schema in -->
<!-- testinghq/core/config_schema.py. tests/unit/test_config_schema.py fails -->
<!-- when this file does not match what that script would write, so the only -->
<!-- way to change it is to change the schema, which is the point. -->

# Configuration reference

Every table TestingHQ reads from a config file, generated from the code so it
cannot drift. A key added to a loader without a line here fails the suite.

Validate a file and see what the loaders make of it:

```bash
testinghq config validate target.toml
```

`config validate` sends nothing and never prints a header value. Headers are
shown as `env:NAME`, so the output is safe to paste into an issue.

Regenerate this file after changing the schema:

```bash
python -m testinghq.config_doc
```
"""


def render_reference() -> str:
    """The whole of docs/CONFIG.md, as a string.

    A pure function of the schema, so the drift test is a string comparison and
    nothing else has to be kept in sync.
    """
    parts: List[str] = [_HEADER]

    for table in config_schema.TABLES:
        heading = (
            f"`[{table.name}.<name>]`" if table.patterned else f"`[{table.name}]`"
        )
        parts.append(f"## {heading}\n")
        parts.append(table.summary + "\n")

        if table.free_form:
            parts.append(
                "Keys are data rather than a fixed set. The fields or headers "
                "this table may name are:\n"
            )

        parts.append("| Key | Type | Default | Required | Meaning |")
        parts.append("| --- | --- | --- | --- | --- |")
        for key in table.keys:
            parts.append(
                f"| `{key.name}` | {key.type} | "
                f"{_default_cell(key)} | {key.required} | {_meaning_cell(key)} |"
            )
        parts.append("")

        for note in table.notes:
            parts.append(note + "\n")

    return "\n".join(parts).rstrip() + "\n"


def _default_cell(key) -> str:
    return "none" if key.default is None else f"`{key.default}`"


def _meaning_cell(key) -> str:
    # A pipe inside prose would break the generated markdown row. None of the
    # current meanings contain one, but the generator should not depend on that
    # staying true.
    return key.meaning.replace("|", "\\|")


def write_reference(path: Path) -> bool:
    """Write docs/CONFIG.md. Returns True if the file changed.

    The return value is what lets the drift test be a failure with a message
    rather than a silent rewrite: a test that fixed the file on the way past
    would leave the suite green with the wrong content in a release.
    """
    path = Path(path)
    wanted = render_reference()
    if path.is_file() and path.read_text(encoding="utf-8") == wanted:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wanted, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# The regeneration script
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="testinghq.config_doc",
        description="Regenerate the configuration reference from the schema.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the file is out of date",
    )
    parser.add_argument(
        "--path",
        default=str(REPO_ROOT / REFERENCE_PATH),
        help="where docs/CONFIG.md lives",
    )
    args = parser.parse_args(argv)
    path = Path(args.path)

    if args.check:
        wanted = render_reference()
        if not path.is_file():
            print(
                f"{path} does not exist; run: python -m testinghq.config_doc",
                file=sys.stderr,
            )
            return 1
        if path.read_text(encoding="utf-8") != wanted:
            print(
                f"{path} is out of date with testinghq/core/config_schema.py.\n"
                "Run: python -m testinghq.config_doc",
                file=sys.stderr,
            )
            return 1
        print(f"{path} is up to date")
        return 0

    changed = write_reference(path)
    print(f"{'wrote' if changed else 'left unchanged'} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
