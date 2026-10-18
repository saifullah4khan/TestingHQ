"""Resolve a configured target for a send: its transport, where it goes, its
wire format, its credentials and its publicness.

Every send path calls `resolve_target` once, before anything goes on the wire,
and hands the result to `transport.send`. The rules, in order:

1. `--send` requires `--target`.
2. The name must be declared in the config file.
3. The host (the URL's, or an SMTP target's `host`) is checked with
   `guardrails.is_private_host`, which adds a DNS lookup to the string check.
   A public host is refused unless `allow_public_target=True` (the
   `--allow-public-target` flag). The rule is the same for both transports.
4. Barrage at a public host additionally needs `public = true` on the target's
   config table. Both are required; the refusal names whichever is missing.
5. An SMTP target's `envelope_from` and `envelope_to`, when set, must be
   synthetic addresses, like every address in a payload: they decide who the
   message is delivered to.
6. The `env:` references (`[targets.<name>.headers]`, an SMTP `username` and
   `password`) are read from the environment. One that is not set is refused.

Refusals raise `guardrails.GuardrailError`; a missing or malformed config file,
or an unset variable, raises `config.ConfigError`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from . import guardrails
from .config import ConfigError, Target, load_config, resolve_env_references
from .formats import WireFormat, get_format
from .transport import DEFAULT_BOUNDARY, DEFAULT_TRANSPORT, TRANSPORT_SMTP, SmtpEndpoint

#: The `load_tool` value that triggers the double opt-in rule.
BARRAGE = "barrage"


@dataclass(frozen=True)
class ResolvedTarget:
    """A target that passed every guardrail, ready to send to."""

    name: str
    #: The HTTP URL, or `smtp://host:port` for an SMTP target.
    url: str
    wire_format: WireFormat
    format_name: str
    public: bool
    #: "http" or "smtp": which branch of `transport.send` this target takes.
    transport: str = DEFAULT_TRANSPORT
    #: `[targets.<name>.headers]` with the values read from the environment.
    #: Added to every HTTP request. Kept out of the repr, and never written to
    #: an artifact.
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, hash=False)
    #: Where and how to deliver, for an SMTP target; None for HTTP.
    smtp: Optional[SmtpEndpoint] = None

    def artifact_config(self) -> Dict[str, Any]:
        """The keys every artifact's `config` block records about its target.
        `allow_public_target` is true only when the run fired at a public host
        under the flag."""
        return {
            "format": self.format_name,
            "allow_public_target": self.public,
            "transport": self.transport,
        }


def wire_format_for(target: Target) -> WireFormat:
    """The encoder a configured target names, built with the default boundary."""
    return get_format(target.wire_format, DEFAULT_BOUNDARY)


def host_is_public(url: str) -> bool:
    """True when the URL's host would need `--allow-public-target`. Takes an
    SMTP target's `smtp://host:port` as well as an HTTP URL."""
    return not guardrails.is_private_host(guardrails.extract_host(url))


def resolve_secrets(
    target: Target, env: Optional[Mapping[str, str]] = None
) -> Dict[str, Dict[str, str]]:
    """Read a target's `env:` references from the environment:
    {"headers": {...}, "smtp": {"username": ..., "password": ...}}.

    Raises ConfigError naming the first reference whose variable is not set.
    `config validate` calls this too, so an unset variable is found there and
    not at send time.
    """
    environ = os.environ if env is None else env
    headers = resolve_env_references(
        f"[targets.{target.name}.headers]", dict(target.headers), environ
    )
    for name, value in headers.items():
        if "\r" in value or "\n" in value:
            raise ConfigError(
                f"[targets.{target.name}.headers].{name}: the variable "
                f"{target.headers[name][len('env:'):].strip()!r} holds a line "
                "break, which would end the header early"
            )
    smtp: Dict[str, str] = {}
    if target.smtp is not None:
        smtp = resolve_env_references(
            f"[targets.{target.name}]", target.smtp.secret_references(), environ
        )
    return {"headers": headers, "smtp": smtp}


def _smtp_endpoint(target: Target, secrets: Mapping[str, str]) -> SmtpEndpoint:
    settings = target.smtp
    assert settings is not None
    overrides = [
        value for value in (settings.envelope_from, *settings.envelope_to) if value
    ]
    if overrides:
        try:
            guardrails.require_synthetic_content(overrides)
        except guardrails.GuardrailError as exc:
            raise guardrails.GuardrailError(
                f"{exc} (envelope_from / envelope_to in [targets.{target.name}]). "
                "The envelope decides where a message is delivered, so it is held "
                "to the same rule as every address in a payload"
            ) from None
    return SmtpEndpoint(
        host=settings.host,
        port=settings.port,
        starttls=settings.starttls,
        username=secrets.get("username"),
        password=secrets.get("password"),
        envelope_from=settings.envelope_from,
        envelope_to=tuple(settings.envelope_to),
    )


def resolve_target(
    target_name: Optional[str],
    config_path: str,
    *,
    allow_public_target: bool = False,
    load_tool: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> ResolvedTarget:
    """Resolve `target_name` from the config at `config_path`, or refuse.
    `env` is where `env:` references are read from (default `os.environ`)."""
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

    secrets = resolve_secrets(target, env)
    smtp = (
        _smtp_endpoint(target, secrets["smtp"])
        if target.transport == TRANSPORT_SMTP else None
    )
    return ResolvedTarget(
        name=target_name,
        url=target.url,
        wire_format=wire_format_for(target),
        format_name=target.wire_format,
        public=public,
        transport=target.transport,
        headers=secrets["headers"],
        smtp=smtp,
    )
