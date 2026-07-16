"""Target configuration for the web UI.

Targets are an explicit allow-list loaded from `web/targets.json`. The UI
never accepts a free-text target: every place that needs a target renders a
dropdown built from this list, and the server re-validates against it before
honoring a fire request.

Host safety is NOT decided here. It is delegated to the canonical guardrail
`testinghq.core.guardrails.require_configured_target`, which is imported and
never reimplemented, so there is exactly one definition of "a host we are
willing to fire at" in the codebase and this loader inherits any future
hardening of it for free. Loading fails loudly rather than silently
admitting a host the canonical guardrail would refuse.

The one check that genuinely belongs here is the URL scheme: the canonical
guardrail reasons about hosts, not schemes, and a config file is the right
place to reject a non-http(s) URL outright. That is additive, not a
duplicate of anything canonical.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlparse

from testinghq.core import guardrails

DEFAULT_TARGETS_PATH = Path(__file__).resolve().parent / "targets.json"


class ConfigError(ValueError):
    """Raised when targets.json is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str


def _validate_target(name, url):
    if not name or not isinstance(name, str):
        raise ConfigError(f"target entry has an invalid name: {name!r}")
    if not url or not isinstance(url, str):
        raise ConfigError(f"target {name!r} has an invalid url: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"target {name!r} url must be http(s): {url!r}")
    # Delegate host safety to the canonical guardrail. The singleton
    # allow-list makes the membership half of the check trivially true so
    # that the public-host half is what is actually being asked here.
    try:
        guardrails.require_configured_target(url, (url,))
    except guardrails.GuardrailError as exc:
        raise ConfigError(
            f"target {name!r} url {url!r} was refused by the canonical target "
            f"guardrail: {exc}"
        ) from exc
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
