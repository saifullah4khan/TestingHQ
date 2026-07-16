"""Target configuration for the web UI.

Targets are an explicit allow-list loaded from `web/targets.json`. The UI
never accepts a free-text target: every place that needs a target renders a
dropdown built from this list, and the server re-validates against it before
honoring a fire request. This is the same guardrail shape as
`testinghq/core/guardrails.require_configured_target`, kept independent here
because this lane cannot import from `testinghq/**` yet.

Defense in depth: target URLs are also required to live on a reserved,
non-routable-for-real-mail domain (example.com/.net/.org/.edu, or a
.test/.invalid/.example/.localhost TLD), same as the rest of the synthetic
content this tool generates. A malformed targets.json fails loudly rather
than silently allowing an unreserved host.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlparse

DEFAULT_TARGETS_PATH = Path(__file__).resolve().parent / "targets.json"

_RESERVED_DOMAIN_SUFFIXES = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".example.edu",
    "example.com",
    "example.net",
    "example.org",
    "example.edu",
    ".test",
    ".invalid",
    ".example",
    ".localhost",
    "localhost",
)


class ConfigError(ValueError):
    """Raised when targets.json is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str


def _is_reserved_host(host):
    if host is None:
        return False
    host = host.lower()
    return any(
        host == suffix.lstrip(".") or host.endswith(suffix)
        for suffix in _RESERVED_DOMAIN_SUFFIXES
    )


def _validate_target(name, url):
    if not name or not isinstance(name, str):
        raise ConfigError(f"target entry has an invalid name: {name!r}")
    if not url or not isinstance(url, str):
        raise ConfigError(f"target {name!r} has an invalid url: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"target {name!r} url must be http(s): {url!r}")
    if not _is_reserved_host(parsed.hostname):
        raise ConfigError(
            f"target {name!r} url {url!r} is not on a reserved demo domain "
            "(example.com/.net/.org/.edu, or .test/.invalid/.example/.localhost)"
        )
    return Target(name=name, url=url)


def load_targets(path=None) -> "Dict[str, Target]":
    """Load and validate the target allow-list. Fails loudly on problems."""
    path = Path(path) if path is not None else DEFAULT_TARGETS_PATH
    if not path.exists():
        raise ConfigError(f"targets file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"targets file is not valid JSON: {path}: {exc}") from exc

    entries = raw.get("targets") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"targets file has no targets list: {path}")

    targets: Dict[str, Target] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"malformed target entry: {entry!r}")
        target = _validate_target(entry.get("name"), entry.get("url"))
        targets[target.name] = target
    return targets


def target_names(targets) -> "Tuple[str, ...]":
    return tuple(targets.keys())
