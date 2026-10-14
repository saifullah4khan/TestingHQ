"""Resolve a configured target for a send: its URL, wire format and publicness.

Every send path calls `resolve_target` once, before anything goes on the wire.
The rules, in order:

1. `--send` requires `--target`.
2. The name must be declared in the config file.
3. The URL's host is checked with `guardrails.is_private_host`, which adds a
   DNS lookup to the string check. A public host is refused unless
   `allow_public_target=True` (the `--allow-public-target` flag).
4. Barrage at a public host additionally needs `public = true` on the target's
   config table. Both are required; the refusal names whichever is missing.

Refusals raise `guardrails.GuardrailError`; a missing or malformed config file
raises `config.ConfigError`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import guardrails
from .config import Target, load_config
from .formats import WireFormat, get_format
from .transport import DEFAULT_BOUNDARY

#: The `load_tool` value that triggers the double opt-in rule.
BARRAGE = "barrage"


@dataclass(frozen=True)
class ResolvedTarget:
    """A target that passed every guardrail, ready to send to."""

    name: str
    url: str
    wire_format: WireFormat
    format_name: str
    public: bool

    def artifact_config(self) -> Dict[str, Any]:
        """The keys every artifact's `config` block records about its target.
        `allow_public_target` is true only when the run fired at a public host
        under the flag."""
        return {"format": self.format_name, "allow_public_target": self.public}


def wire_format_for(target: Target) -> WireFormat:
    """The encoder a configured target names, built with the default boundary."""
    return get_format(target.wire_format, DEFAULT_BOUNDARY)


def host_is_public(url: str) -> bool:
    """True when the URL's host would need `--allow-public-target`."""
    return not guardrails.is_private_host(guardrails.extract_host(url))


def resolve_target(
    target_name: Optional[str],
    config_path: str,
    *,
    allow_public_target: bool = False,
    load_tool: str = "",
) -> ResolvedTarget:
    """Resolve `target_name` from the config at `config_path`, or refuse."""
    if not target_name:
        raise guardrails.GuardrailError("refusing to fire: --send requires --target")

    config = load_config(config_path)
    if target_name not in config.targets:
        raise guardrails.GuardrailError(
            "refusing to fire at an unconfigured target: "
            f"{target_name!r} is not in the configured target list "
            f"{sorted(config.targets)}"
        )
    target = config.get(target_name)
    host = guardrails.extract_host(target.url)
    public = host_is_public(target.url)

    if public and load_tool == BARRAGE:
        missing = []
        if not allow_public_target:
            missing.append("--allow-public-target")
        if not target.public:
            missing.append(f"public = true in [targets.{target_name}]")
        if missing:
            raise guardrails.GuardrailError(
                f"refusing to load-test a public host: {host!r} (target "
                f"{target_name!r}) is public, and barrage at a public host needs "
                "both --allow-public-target and "
                f"public = true in [targets.{target_name}]. Missing: "
                f"{', '.join(missing)}."
            )
    elif public and not allow_public_target:
        raise guardrails.GuardrailError(
            f"refusing to fire at a public host: {host!r} (target "
            f"{target_name!r}) is not a reserved test domain, an internal name "
            "or a private address, and does not resolve only to private "
            "addresses. Pass --allow-public-target to fire at an endpoint you "
            "own."
        )

    return ResolvedTarget(
        name=target_name,
        url=target.url,
        wire_format=wire_format_for(target),
        format_name=target.wire_format,
        public=public,
    )
