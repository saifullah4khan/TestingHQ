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
from email.utils import getaddresses
from typing import Iterable, List, Protocol, runtime_checkable
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


def _extract_addresses(values: Iterable[str]) -> List[str]:
    """Extract EVERY bare address from a sequence of address-header values.

    Accepts what the engine actually emits: a bare address
    ("a@example.com"), an RFC 5322 display-name form
    ("Ivan Jones <a@example.edu>"), or a header carrying several
    comma-separated recipients ("A <a@example.com>, B <b@example.net>").

    Uses email.utils.getaddresses, NEVER email.utils.parseaddr. parseaddr
    returns at most ONE address, so a guard built on it would check only
    the first recipient of a multi-recipient header and let every
    subsequent address through unchecked. For a safety check whose whole
    job is "refuse if ANY address is non-synthetic", that is a bypass, not
    an inconvenience. getaddresses returns all of them. See
    tests/security/test_synthetic_content.py, which demonstrates that
    bypass against a parseaddr-based extractor and pins this one against
    it.

    Note: `strict=` is deliberately not passed to getaddresses. It only
    exists on Python 3.13+ and on recent 3.9-3.12 patch releases, and this
    project supports Python >= 3.9. The default behavior extracts every
    address on all supported versions, which is what this guard needs.
    """
    fieldvalues = []
    for value in values:
        if not isinstance(value, str):
            # Never coerce. A non-string address-bearing field is not
            # something this guard can verify, so refuse via the
            # fail-closed path below rather than str()-ing it into
            # something that might parse.
            return []
        fieldvalues.append(value)
    return [address for _name, address in getaddresses(fieldvalues)]


def require_synthetic_content(emails: Iterable[str]) -> None:
    """Refuse to fire if generated content does not look synthetic.

    `emails` is an iterable of address-header values pulled from generated
    payloads (InboundEmail.to, InboundEmail.from_addr, envelope addresses,
    address headers, etc.). Each value may be a bare address, a display-name
    form, or a comma-separated multi-recipient header; a single string is
    accepted as a convenience for one value.

    EVERY address extracted from EVERY value must be synthetic. Raises
    GuardrailError if any one of them is not.

    Fails closed. Refuses when no addresses could be extracted at all
    (empty input, an unparseable header, an empty group like
    "undisclosed-recipients:;") and when any extracted address is empty.
    Nothing verified means nothing is trusted: an unparseable header must
    refuse, not pass vacuously.
    """
    if isinstance(emails, (str, bytes)):
        emails = [emails]

    addresses = _extract_addresses(emails)

    if not addresses:
        raise GuardrailError(
            "refusing to fire: no addresses could be extracted to verify as "
            "synthetic (empty or unparseable address content)"
        )

    for address in addresses:
        if not address:
            raise GuardrailError(
                "refusing to fire: an address-bearing field yielded an empty "
                "address, so it could not be verified as synthetic"
            )
        if not is_synthetic_address(address):
            raise GuardrailError(
                "refusing to fire: generated content is not synthetic - "
                f"{address!r} is not on a reserved domain or TLD"
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
