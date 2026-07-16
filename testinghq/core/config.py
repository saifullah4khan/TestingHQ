"""Configuration loading for TestingHQ.

Targets are declared explicitly in a TOML config file, one `[targets.<name>]`
table per target with a required `url`. Environment variables may override a
declared target's url (but never invent a new one); this lets a target
config be checked in while keeping the real URL out of source control.

Loading fails loud: a missing file, unparsable TOML, a malformed `targets`
table, or a target missing its `url` all raise ConfigError. Nothing here
silently drops a bad value or falls back to an empty config.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

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


@dataclass(frozen=True)
class Target:
    """A named, allowed firing target."""

    name: str
    url: str

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


@dataclass(frozen=True)
class Config:
    """Loaded TestingHQ configuration."""

    targets: Dict[str, Target] = field(default_factory=dict)

    def target_urls(self):
        return [t.url for t in self.targets.values()]

    def allowed_target_names(self):
        """Target names, suitable as the `allowed_targets` argument to
        guardrails.require_configured_target."""
        return list(self.targets.keys())

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

    Raises ConfigError for: a missing file, unparsable TOML, a `targets` key
    that is not a table, a target entry that is not a table, or a target
    entry missing `url`. `Target.__post_init__` further validates each url's
    shape.
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

        env_key = f"TESTINGHQ_TARGET_{_env_key(name)}_URL"
        if env_key in env:
            url = env[env_key]

        targets[name] = Target(name=name, url=url)

    return Config(targets=targets)
