"""Safety guardrails for TestingHQ.

Blast is a fuzzer for endpoints you control, not an email sender. These
guardrails are first-class: dry-run is the default, firing is explicit, and
only configured targets may be hit.
"""
from __future__ import annotations

from dataclasses import dataclass


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


def require_configured_target(target, allowed_targets):
    """Refuse to fire at a target that is not declared in config."""
    allowed = set(allowed_targets or ())
    if target not in allowed:
        raise GuardrailError(
            "refusing to fire at an unconfigured target: "
            f"{target!r} is not in the configured target list"
        )
    return target
