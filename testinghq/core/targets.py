"""Target resolution for every send path.

Minimal version: the configured-name check and the host check from
`guardrails.require_configured_target`, plus the target's wire format. DNS
resolution of private names and the Barrage opt-in are not implemented here.
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
    """Resolve a configured target name to its URL and wire format, refusing
    an unconfigured name and, unless `allow_public_target`, a public host."""
    if not target_name:
        raise guardrails.GuardrailError("refusing to fire: --send requires --target")
    config = load_config(config_path)
    guardrails.require_configured_target(target_name, config.allowed_target_names())
    target = config.get(target_name)
    url = target.url
    public = False
    try:
        guardrails.require_configured_target(url, (url,))
    except guardrails.GuardrailError:
        if not allow_public_target:
            raise
        public = True
    return ResolvedTarget(
        name=target_name,
        url=url,
        wire_format=get_format(target.wire_format, DEFAULT_BOUNDARY),
        format_name=target.wire_format,
        public=public,
    )
