"""The one seam between the web UI and the Blast engine.

Every other module in `web/` calls into this file to get a run artifact -
never into `web/generator.py` directly, and never (yet) into `testinghq/**`.
Today, `dry_run()` and `fire()` are both backed by the deterministic fixture
generator in `web/generator.py`, because `testinghq/blast/generate.py` and
`testinghq/core/transport.py` do not exist on this branch yet.

When the engine lane merges, swap the body of `dry_run()` and `fire()` to
call into the real engine. That is the one-line change the rest of this app
was built around; nothing outside this file should need to know.

Guardrails live here too, independent of `testinghq/core/guardrails.py`, so
this lane stays finishable and testable without depending on code owned by
the parallel engine lane:

- Dry-run never requires a target and never "sends" anything.
- Firing requires a target that is in the configured allow-list (see
  `web/config.py` / `web/targets.json`) AND an explicit confirm flag. There
  is no free-text target: the caller must pass a name that resolves through
  `config.load_targets()`.
"""
from __future__ import annotations

from . import config as config_module
from . import generator


class AdapterGuardrailError(RuntimeError):
    """Raised when a guardrail refuses a fire request."""


def dry_run(mix, count, seed):
    """Return a run artifact without sending anything, ever."""
    return generator.generate_run(mix, count, seed, target=None, dry_run=True)


def fire(target, mix, count, seed, confirm, targets=None):
    """Return a run artifact for a fire request, after enforcing guardrails.

    `confirm` must be exactly True (an explicit, unambiguous send decision -
    not just any truthy value the caller happened to pass). `target` must be
    a name present in the configured target allow-list; arbitrary URLs are
    never accepted.
    """
    if confirm is not True:
        raise AdapterGuardrailError(
            "refusing to fire: explicit confirm was not given"
        )

    allowed = targets if targets is not None else config_module.load_targets()
    if not target or target not in allowed:
        raise AdapterGuardrailError(
            f"refusing to fire at an unconfigured target: {target!r} is not "
            "in the configured target list"
        )

    # NOTE: this still does not make a network call. There is no transport
    # yet (testinghq/core/transport.py does not exist on this branch). Once
    # it lands, this is the call site that posts records instead of
    # synthesizing them, keeping the guardrail checks above unchanged.
    return generator.generate_run(mix, count, seed, target=target, dry_run=False)
