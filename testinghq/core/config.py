"""Configuration loading for TestingHQ.

Targets are declared explicitly in a TOML config file, one `[targets.<name>]`
table per target with a required `url`, an optional wire `format` (default
SendGrid Inbound Parse) and an optional `public` flag (default false, read only
by Barrage). Environment variables may override a declared target's url (but
never invent a new one), so a target config can be checked in while the real
URL stays out of source control.

Loading fails loud: a missing file, unparsable TOML, a malformed `targets`
table, a target missing its `url`, an unknown `format` or a non-boolean
`public` all raise ConfigError. Nothing falls back to a default for a bad value.

A key or table the schema in `config_schema.py` does not declare is refused
too, with the closest known name when one is close. Ignoring it would be the
worst failure a config file can have: `formt = "mailgun"` would load, look
fine, and send every message as SendGrid.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from . import config_schema
from .formats import format_names
from .transport import DEFAULT_FORMAT


try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed. Config loading
    fails loud; it never silently produces an empty or partial Config."""


class UnknownKeyError(ConfigError):
    """A config file holds a key or table the schema does not declare.

    Its own class because a caller that tolerates a config it cannot load
    (a dry run with `--readback` given as a flag, say) must still refuse
    this one: the file is there and says something, just not what was meant.
    """


@dataclass(frozen=True)
class Target:
    """A named, allowed firing target."""

    name: str
    url: str
    #: Which wire format to serialize this target's messages in. Defaults to
    #: SendGrid Inbound Parse, which is what every target was sent as before the
    #: format layer existed, so a config with no `format` key is unchanged.
    #: Validated here rather than at send time so a typo fails on load, with
    #: the list of valid names, instead of after a dry-run looked fine.
    wire_format: str = DEFAULT_FORMAT
    #: Barrage's half of the double opt-in for a public host: a load test at a
    #: public host needs `public = true` here as well as --allow-public-target.
    public: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(f"target name must be a non-empty string, got {self.name!r}")
        if not isinstance(self.url, str) or not self.url:
            raise ConfigError(
                f"target {self.name!r}: url must be a non-empty string, got {self.url!r}"
            )
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ConfigError(
                f"target {self.name!r}: url must start with http:// or https://, "
                f"got {self.url!r}"
            )
        if not isinstance(self.wire_format, str) or not self.wire_format:
            raise ConfigError(
                f"target {self.name!r}: format must be a non-empty string, got "
                f"{self.wire_format!r}"
            )
        if self.wire_format not in format_names():
            raise ConfigError(
                f"target {self.name!r}: unknown format {self.wire_format!r}; "
                f"known formats are {format_names()}"
            )
        if not isinstance(self.public, bool):
            raise ConfigError(
                f"target {self.name!r}: public must be true or false, got "
                f"{self.public!r}"
            )


@dataclass(frozen=True)
class Config:
    """Loaded TestingHQ configuration."""

    targets: Dict[str, Target] = field(default_factory=dict)
    readback: Optional[Any] = None

    def target_urls(self):
        return [t.url for t in self.targets.values()]

    def allowed_target_names(self):
        """Target names, suitable as the `allowed_targets` argument to
        guardrails.require_configured_target."""
        return list(self.targets.keys())

    def readback_table(self):
        """The raw `[readback]` table, or None when the file declares none.

        Held raw rather than parsed, and typed `Any`, on purpose: the shape and
        the meaning of that table belong to testinghq/pipeline/adapters.py, and
        parsing it here would make the core config loader know about a tool
        that did not exist when it was written. The cast-free Optional[Any] is
        what lets that stay true without importing the pipeline package into
        core.
        """
        return self.readback

    def get(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(
                f"unconfigured target {name!r}; configured targets: "
                f"{sorted(self.targets)}"
            ) from None


def _env_key(name: str) -> str:
    """Map a target name to the environment variable suffix that can
    override its url: non-alphanumeric characters become underscores, and
    the result is upper-cased. e.g. "my-target" -> "MY_TARGET"."""
    return "".join(ch if ch.isalnum() else "_" for ch in name).upper()


def _is_custom_adapter(values: Mapping[str, Any]) -> bool:
    """True when a readback-style table names a `module:attribute` adapter,
    either as its `kind` or as `spec`."""
    kind = values.get("kind")
    if isinstance(kind, str) and ":" in kind:
        return True
    return values.get("spec") is not None


def _unknown(where: str, key: str, known: Sequence[str]) -> str:
    """One refusal line: where, which key, and the closest known name."""
    close = difflib.get_close_matches(key, list(known), n=1)
    if close:
        return f"{where} has unknown key {key!r} (did you mean {close[0]!r}?)"
    return f"{where} has unknown key {key!r} (known keys: {', '.join(known)})"


def _check_table(
    dotted: str, table: "config_schema.Table", values: Mapping[str, Any],
    problems: List[str],
) -> None:
    """Check one table's key names. `dotted` is its name as the file spells
    it (`targets.local`, `loop.outbound`), for the message."""
    if table.free_form:
        # `[readback.fields]` names readback fields and `[readback.headers]`
        # names request headers: data, not a fixed set. The adapter loader
        # checks field names itself, and header names are the operator's.
        return
    known = config_schema.known_keys(table)
    open_for_adapter = (
        config_schema.accepts_custom_adapter_keys(table) and _is_custom_adapter(values)
    )
    for key, value in values.items():
        child = config_schema.child_table(table, key)
        if child is not None:
            if isinstance(value, dict):
                _check_table(f"{dotted}.{key}", child, value, problems)
            continue
        if key in known or open_for_adapter:
            continue
        problems.append(_unknown(f"[{dotted}]", key, known))


def unknown_key_problems(raw: Mapping[str, Any]) -> List[str]:
    """Every key or table in a parsed config file that the schema does not
    declare, one line each, in file order. Empty for a clean file.

    Only names are checked here. Values, and the shape of a table (a
    `targets` that is not a table, say), are left to the loaders that read
    them, so a malformed value is reported by the code that would run it.
    """
    problems: List[str] = []
    top = config_schema.top_level_names()
    for key, value in raw.items():
        if key not in top:
            close = difflib.get_close_matches(key, top, n=1)
            what = f"unknown table [{key}]" if isinstance(value, dict) else (
                f"unknown top-level key {key!r}"
            )
            hint = (
                f"did you mean [{close[0]}]?" if close
                else f"known tables: {', '.join(f'[{t}]' for t in top)}"
            )
            problems.append(f"{what} ({hint})")
            continue
        if not isinstance(value, dict):
            continue
        if key == config_schema.TARGETS.name:
            for name, entry in value.items():
                if isinstance(entry, dict):
                    _check_table(
                        f"targets.{name}", config_schema.TARGETS, entry, problems
                    )
            continue
        table = config_schema.table_named(key)
        if table is not None:
            _check_table(key, table, value, problems)
    return problems


def check_known_keys(raw: Mapping[str, Any], source: Union[str, Path]) -> None:
    """Refuse a parsed config file holding a key or table the schema does not
    declare. `source` is the file, named in the message.

    Refused rather than warned about: a warning scrolls past, and the run it
    precedes has already sent with the default the typo fell back to. A custom
    readback adapter (`kind = "module:attribute"`) may carry keys of its own,
    which the schema cannot know, so those tables accept extra keys and only
    those.
    """
    problems = unknown_key_problems(raw)
    if not problems:
        return
    raise UnknownKeyError(
        f"config file {source}: " + "; ".join(problems) + ". An unknown key is "
        "refused rather than ignored, because an ignored typo runs with the "
        "default; docs/CONFIG.md lists every key"
    )


def load_config(
    path: Union[str, Path], env: Optional[Mapping[str, str]] = None
) -> Config:
    """Load targets from a TOML config file, with environment overrides.

    File shape::

        [targets.local]
        url = "http://localhost:8000/inbound"

        [targets.staging]
        url = "https://staging.example.test/inbound"

    Environment overrides: `TESTINGHQ_TARGET_<NAME>_URL` replaces the url of
    the file-declared target `<name>` (see `_env_key` for the name mapping).
    `env` defaults to `os.environ`; pass an explicit mapping in tests so
    loading never depends on the real process environment.

    An optional `[readback]` table is accepted and carried through untouched as
    `Config.readback`. It describes how the pipeline tools read a system's
    output, which only those tools interpret, so this loader validates only
    that it is a table and leaves its contents to
    `testinghq/pipeline/adapters.py`.

    Raises ConfigError for: a missing file, unparsable TOML, a key or table
    the schema does not declare (see `check_known_keys`), a `targets` key
    that is not a table, a target entry that is not a table, a target
    entry missing `url`, or a `readback` key that is not a table.
    `Target.__post_init__` further validates each url's shape.
    """
    if tomllib is None:  # pragma: no cover - depends on interpreter version
        raise ConfigError(
            "no TOML parser available: Python < 3.11 requires the 'tomli' package"
        )
    if env is None:
        env = os.environ

    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"config file {config_path} is not valid TOML: {exc}") from exc

    # First, so `[target.local]` (a typo for `targets`) is reported as the
    # typo it is, not as "no targets declared".
    check_known_keys(raw, config_path)

    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, dict):
        raise ConfigError(
            f"config file {config_path}: 'targets' must be a table, "
            f"got {type(targets_raw).__name__}"
        )
    if not targets_raw:
        raise ConfigError(f"config file {config_path}: no targets declared under [targets]")

    targets: Dict[str, Target] = {}
    for name, entry in targets_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config file {config_path}: targets.{name} must be a table, "
                f"got {type(entry).__name__}"
            )
        if "url" not in entry:
            raise ConfigError(
                f"config file {config_path}: targets.{name} is missing required key 'url'"
            )
        url = entry["url"]
        if not isinstance(url, str):
            raise ConfigError(
                f"config file {config_path}: targets.{name}.url must be a string, "
                f"got {type(url).__name__}"
            )

        # Absent means the default, checked in `Target.__post_init__` against the
        # live registry. A non-string is caught here, because the dataclass
        # validation would report it without the config file or the target name,
        # which are the two things that make the message useful.
        wire_format = entry.get("format", DEFAULT_FORMAT)
        if not isinstance(wire_format, str):
            raise ConfigError(
                f"config file {config_path}: targets.{name}.format must be a "
                f"string, got {type(wire_format).__name__}"
            )

        public = entry.get("public", False)
        if not isinstance(public, bool):
            raise ConfigError(
                f"config file {config_path}: targets.{name}.public must be true "
                f"or false, got {type(public).__name__}"
            )

        env_key = f"TESTINGHQ_TARGET_{_env_key(name)}_URL"
        if env_key in env:
            url = env[env_key]

        targets[name] = Target(
            name=name, url=url, wire_format=wire_format, public=public
        )

    readback = raw.get("readback")
    if readback is not None and not isinstance(readback, dict):
        raise ConfigError(
            f"config file {config_path}: 'readback' must be a table, "
            f"got {type(readback).__name__}"
        )

    return Config(targets=targets, readback=readback)
