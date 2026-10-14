"""Target resolution for every send path.

Minimal implementation with the shared signature; the full version, with DNS
resolution of private names, replaces this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import guardrails
from .config import load_config
from .formats import WireFormat, get_format
from .transport import DEFAULT_BOUNDARY


@dataclass(frozen=True)
class ResolvedTarget:
    name: str
    url: str
    wire_format: WireFormat
    format_name: str
    public: bool


def resolve_target(
    target_name: Optional[str],
    config_path: str,
    *,
    allow_public_target: bool = False,
    load_tool: str = "",
) -> ResolvedTarget:
    """Resolve a configured target name, refusing what the guardrails refuse."""
    if not target_name:
        raise guardrails.GuardrailError("refusing to fire: --send requires --target")
    config = load_config(config_path)
    guardrails.require_configured_target(target_name, config.allowed_target_names())
    target = config.get(target_name)
    host = guardrails._extract_host(target.url)
    public = not guardrails._is_reserved_or_private_host(host)
    if public and not allow_public_target:
        raise guardrails.GuardrailError(
            f"refusing to fire at a public host: {host!r} (target {target_name!r}). "
            "Pass --allow-public-target to fire at a host you own."
        )
    if public and load_tool == "barrage" and not getattr(target, "public", False):
        raise guardrails.GuardrailError(
            f"refusing to load-test a public host: {host!r} (target {target_name!r}). "
            "Barrage needs both --allow-public-target and `public = true` on the "
            f"[targets.{target_name}] table."
        )
    return ResolvedTarget(
        name=target_name,
        url=target.url,
        wire_format=get_format(target.wire_format, DEFAULT_BOUNDARY),
        format_name=target.wire_format,
        public=public,
    )
