"""The one seam between the web UI and the Blast engine.

Every other module in `web/` calls into this file to get a run artifact -
never into `web/generator.py` directly, and never (yet) into the engine
modules. Today, `dry_run()` and `fire()` are both backed by the
deterministic fixture generator in `web/generator.py`, because
`testinghq/blast/generate.py` and `testinghq/core/transport.py` still do
not exist on this branch.

When the engine lane merges, swap the body of `dry_run()` and `fire()` to
call into the real engine. That is the one-line change the rest of this app
was built around; nothing outside this file should need to know.

GUARDRAILS: this module owns no guardrail rules of its own. The canonical
rules live in `testinghq.core.guardrails` and are imported, never
reimplemented and never edited here. The UI inherits whatever that module
decides is safe, including future hardening, automatically.

Two things layered on top are genuine UI-layer concerns. Both are additive
and strictly narrowing; neither can relax a canonical rule:

1. Explicit confirm. The canonical gate is "sending requires an explicit
   flag". The UI additionally requires that flag to be an unambiguous
   boolean True, so a stray truthy value ("no", 0.1, "false") arriving in
   a JSON body can never read as consent.
2. Name-to-URL resolution. The UI's dropdown yields a configured target
   NAME, but the canonical public-host check parses a HOST out of the
   argument it is given. Handing it a bare name would make that check
   vacuous: a single-label name has no dot, so the canonical guard
   classifies it as an internal host and always passes it. So the resolved
   URL is passed through the canonical guard too. That second call is what
   makes the public-host hardening actually bite on the real destination.
"""
from __future__ import annotations

from testinghq.core import guardrails

from . import config as config_module
from . import generator


def dry_run(mix, count, seed):
    """Return a run artifact without sending anything, ever.

    Dry-run is the default action and needs no target: there is nothing to
    refuse, because nothing is ever sent.
    """
    return generator.generate_run(mix, count, seed, target=None, dry_run=True)


def fire(target, mix, count, seed, confirm, targets=None):
    """Return a run artifact for a fire request, after enforcing guardrails.

    Raises `guardrails.GuardrailError` (the canonical error) if the send was
    not explicitly confirmed, if `target` is not in the configured
    allow-list, or if the target's URL is not a host the canonical guardrail
    considers safe to fire at.
    """
    # Canonical send gate. `confirm is True` is the UI-layer narrowing
    # described above: only an actual boolean True counts as consent.
    decision = guardrails.evaluate_send(confirm is True)
    if not decision.will_send:
        raise guardrails.GuardrailError(
            f"refusing to fire: no explicit confirm was given ({decision.reason})"
        )

    allowed = targets if targets is not None else config_module.load_targets()

    # Canonical allow-list check, on the name the UI actually submits. There
    # is no free-text target: anything outside the configured list is refused
    # here, by the canonical guardrail rather than by a local copy of it.
    guardrails.require_configured_target(target, allowed)

    # Canonical public-host check, on the resolved destination URL. The
    # singleton allow-list mirrors how the security lane's own tests exercise
    # this function when the host check is the point.
    url = allowed[target].url
    guardrails.require_configured_target(url, (url,))

    # NOTE: this still does not make a network call. There is no transport
    # yet (testinghq/core/transport.py does not exist on this branch). Once
    # it lands, this is the call site that posts records instead of
    # synthesizing them, keeping the guardrail checks above unchanged.
    return generator.generate_run(mix, count, seed, target=target, dry_run=False)
