"""Safety guardrails for TestingHQ.

Blast is a fuzzer for endpoints you control, not an email sender. These
guardrails are first-class: dry-run is the default, firing is explicit, only
configured targets may be hit, generated content must look synthetic, and
firing is paced by a rate limit.

This module owns the contract; it does not implement the token bucket. See
`RateLimitGate` below - the engine lane implements that interface in
`testinghq.core.ratelimit`. This module never imports from that package, or
from `testinghq.core.transport` or `testinghq.blast.*`.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable
from urllib.parse import urlsplit


class GuardrailError(RuntimeError):
    """Raised when a guardrail refuses an action."""


@dataclass(frozen=True)
class FireDecision:
    """The result of evaluating whether a run may actually send."""

    will_send: bool
    reason: str


def evaluate_send(send_flag):
    """Dry-run is the default. Sending requires an explicit send flag."""
    if send_flag:
        return FireDecision(will_send=True, reason="explicit --send flag set")
    return FireDecision(will_send=False, reason="dry-run default (no --send)")


# ---------------------------------------------------------------------------
# Target guardrails
# ---------------------------------------------------------------------------

# RFC 2606 / RFC 6761 reserved domains and TLDs. These are the only domains
# and TLDs Blast may ever generate synthetic addresses under, and they are
# always treated as safe firing targets regardless of the public-host check.
RESERVED_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "example.edu"}
)
RESERVED_TLDS = frozenset({"test", "invalid", "example", "localhost"})


def _extract_host(target):
    """Best-effort hostname extraction from a target string.

    Handles full URLs ("https://ok.example"), bare host[:port] strings, and
    short internal names ("local") the same way, by parsing as a netloc.
    """
    if not target:
        return ""
    candidate = target if "//" in target else "//" + target
    host = urlsplit(candidate).hostname
    if host:
        return host.lower()
    return target.strip().lower()


def _is_reserved_or_private_host(host):
    """True if `host` is a reserved test domain/TLD, a bare internal name,
    or a private/loopback/link-local IP literal. False for anything that
    looks like a real, publicly routable host.
    """
    if not host:
        return False
    host = host.rstrip(".")
    if host == "localhost":
        return True
    if "." not in host:
        # Bare single-label hostnames ("local", "staging", docker service
        # names) are not publicly routable. Treat as internal, not public.
        return True
    if host in RESERVED_DOMAINS or any(
        host.endswith("." + domain) for domain in RESERVED_DOMAINS
    ):
        return True
    tld = host.rsplit(".", 1)[-1]
    if tld in RESERVED_TLDS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_unspecified
    )


def require_configured_target(target, allowed_targets, *, allow_public_hosts=False):
    """Refuse to fire at a target that is not declared in config.

    Also refuses obvious non-reserved public hosts, even if present in
    `allowed_targets`, unless the caller explicitly passes
    `allow_public_hosts=True`. This keeps a misconfigured or copy-pasted
    real-world hostname from silently becoming a valid firing target: an
    operator who really means to fire at a public host must say so.
    """
    allowed = set(allowed_targets or ())
    if target not in allowed:
        raise GuardrailError(
            "refusing to fire at an unconfigured target: "
            f"{target!r} is not in the configured target list"
        )
    host = _extract_host(target)
    if not allow_public_hosts and not _is_reserved_or_private_host(host):
        raise GuardrailError(
            "refusing to fire at a non-reserved public host: "
            f"{host!r} (from target {target!r}) does not look like a "
            "reserved test domain, an internal name, or a private address. "
            "Pass allow_public_hosts=True to override explicitly."
        )
    return target


# ---------------------------------------------------------------------------
# Synthetic-content guardrails
# ---------------------------------------------------------------------------


def is_synthetic_address(address: str) -> bool:
    """True if `address` is an email address on a reserved domain or TLD.

    Reserved set (RFC 2606 / RFC 6761), matching what Blast is allowed to
    generate:
      domains: example.com, example.net, example.org, example.edu
      TLDs:    .test, .invalid, .example, .localhost
    Subdomains of a reserved domain also count (e.g. "a@mail.example.com").
    """
    if not isinstance(address, str):
        return False
    local, sep, domain = address.rpartition("@")
    if not sep or not local or not domain:
        return False
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return False
    if domain in RESERVED_DOMAINS or any(
        domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS
    ):
        return True
    tld = domain.rsplit(".", 1)[-1]
    return tld in RESERVED_TLDS


def require_synthetic_content(emails: Iterable[str]) -> None:
    """Refuse to fire if generated content does not look synthetic.

    `emails` is an iterable of email address strings pulled from generated
    payloads (envelope from/to, headers, body-embedded addresses, etc.).
    Raises GuardrailError on the first address that is not on a reserved
    domain/TLD, and on empty input (no content means nothing was verified
    as synthetic, so this fails closed rather than passing vacuously).
    """
    if isinstance(emails, (str, bytes)):
        emails = [emails]
    saw_any = False
    for address in emails:
        saw_any = True
        if not is_synthetic_address(address):
            raise GuardrailError(
                "refusing to fire: generated content is not synthetic - "
                f"{address!r} is not on a reserved domain or TLD"
            )
    if not saw_any:
        raise GuardrailError(
            "refusing to fire: no addresses were supplied to verify as "
            "synthetic"
        )


# ---------------------------------------------------------------------------
# Rate-limit gate contract
# ---------------------------------------------------------------------------


@runtime_checkable
class RateLimitGate(Protocol):
    """Interface a rate limiter must satisfy to gate firing.

    Implemented by the engine lane's token bucket in
    `testinghq.core.ratelimit`. Defined here so both lanes can build against
    a single, frozen contract without either importing the other's module.
    """

    def acquire(self, tokens: int = 1) -> float:
        """Block until `tokens` are available; return seconds waited."""
        ...

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking. Consume `tokens` and return True if available,
        otherwise return False and consume nothing."""
        ...
