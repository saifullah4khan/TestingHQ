"""The one seam between the web UI and the Blast engine.

Every other module in `web/` calls into this file to get a run artifact, never
into the engine directly. Since 2026-09-27 that means the real engine:
`blast.generate` builds the corpus, `blast.corrupt` garbles it,
`core.transport` puts it on the wire, and `core.report` builds the records.
This module owns no generation logic of its own.

Before that, `dry_run()` and `fire()` were backed by `web/generator.py`, a
deterministic fixture stand-in written when the engine modules did not exist.
The swap was always the plan: the file was shaped as a single seam precisely
so nothing outside it would need to know. `web/generator.py` is gone as of
2026-09-27. Nothing imported it but the three places that needed an error
class and a category list, and both now come from here and from
`core.report`. The shipped run artifacts under `web/tests/fixtures/` are
unaffected; they are data, not a generator, and they remain the schema corpus
the test suite checks against.

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

`client` is injectable on `fire()` for the same reason the CLI's fire path
takes one: a test must be able to exercise the real transport without opening
a socket. It defaults to None, which is the real network path.
"""
from __future__ import annotations

from testinghq.blast.corrupt import DEFAULT_MIX, corrupt_corpus
from testinghq.blast.generate import generate_corpus
from testinghq.core import guardrails, report
from testinghq.core.transport import post

from . import config as config_module

# recipe name (underscored, blast/corrupt.py's own naming) -> schema label
_LABEL_TO_RECIPE = {
    label: recipe for recipe, label in report.CORRUPT_CATEGORY_LABELS.items()
}


class GeneratorError(ValueError):
    """The caller asked for something this seam cannot do.

    A `ValueError` subclass, which is what it always was: `web/server.py`
    maps it to HTTP 400, and a bad request body is exactly what it is. The
    name survives from `web/generator.py`, where it lived before this module
    ran the real engine and the "generator" was local. It is still accurate,
    since generating the corpus is this module's job, and keeping it means
    the server's error mapping did not have to change for a deletion.

    It is deliberately *not* a guardrail error. A guardrail error means "no"
    and maps to 403; this means "not like that" and maps to 400. Collapsing
    the two would let a malformed mix look like a safety refusal.
    """


def _validate(count, seed):
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise GeneratorError("count must be a non-negative integer")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise GeneratorError("seed must be an integer")


def _select_mix(mix):
    """Translate the UI's selected category labels into the weight mapping
    `corrupt_corpus` wants.

    The UI deals in schema labels ("messy-but-valid"); the corruptor deals in
    its own recipe names ("messy_but_valid"). `core.report` already owns that
    translation in one direction, so this uses the same table rather than
    hand-maintaining a second one.

    An empty selection means "all five", exactly as it did against the
    fixture generator. A partial selection keeps the engine's own default
    weights restricted to those categories, which is deliberately NOT the
    same as an even split: the weights encode how common each kind of mess
    is in the wild, and flattening them would quietly make the UI
    misrepresent the distribution the CLI fires.
    """
    if not mix:
        return dict(DEFAULT_MIX)
    unknown = [c for c in mix if c not in report.CATEGORIES]
    if unknown:
        raise GeneratorError(f"unknown categories in mix: {unknown!r}")
    return {
        _LABEL_TO_RECIPE[label]: DEFAULT_MIX[_LABEL_TO_RECIPE[label]]
        for label in report.CATEGORIES
        if label in mix
    }


def _build_corpus(mix, count, seed):
    """(InboundEmail, schema_label) pairs from the real engine, in order."""
    clean = generate_corpus(seed, count)
    corrupted = corrupt_corpus(clean, seed, _select_mix(mix))
    return [(email, report.category_label(recipe)) for email, recipe in corrupted]


def _category_tally(pairs):
    tally = {label: 0 for label in report.CATEGORIES}
    for _email, label in pairs:
        if label in tally:
            tally[label] += 1
    return tally


def _build_dry_run_artifact(pairs, seed, count, mix):
    """A run artifact for a run that deliberately sent nothing.

    The summary is built by hand rather than by `report.compute_summary`, and
    that is not a shortcut. A dry run has no responses, so every record's
    status is None, and compute_summary would faithfully report every
    payload as a timeout while `classify_record` marked every degenerate
    payload as a failure. Both would be true and both would be nonsense: the
    endpoint was never asked. So the assertions are cleared and the response
    classes are reported as zero, and the category tally is real because the
    corpus really was built. This mirrors the CLI's dry-run path, which
    reaches the same conclusion independently.
    """
    records = []
    for index, (email, label) in enumerate(pairs):
        response = {"status": None, "latency_ms": None, "body_snippet": ""}
        record = report.build_record(email, label, seed, index, response)
        record["assertion"] = {"passed": True, "mismatches": []}
        records.append(record)

    return {
        "seed": seed,
        "config": {
            "mix": _selected_labels(mix),
            "count": count,
            "seed": seed,
            "dry_run": True,
            "target": None,
        },
        "summary": {
            "by_status_class": {"2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0},
            "by_category": _category_tally(pairs),
            "flags": [],
        },
        "records": records,
    }


def _selected_labels(mix):
    if not mix:
        return list(report.CATEGORIES)
    return [label for label in report.CATEGORIES if label in mix]


def dry_run(mix, count, seed):
    """Return a run artifact without sending anything, ever.

    Dry-run is the default action and needs no target: there is nothing to
    refuse, because nothing is ever sent.
    """
    _validate(count, seed)
    pairs = _build_corpus(mix, count, seed)
    return _build_dry_run_artifact(pairs, seed, count, mix)


def fire(target, mix, count, seed, confirm, targets=None, client=None):
    """Return a run artifact for a fire request, after enforcing guardrails.

    Raises `guardrails.GuardrailError` (the canonical error) if the send was
    not explicitly confirmed, if `target` is not in the configured
    allow-list, or if the target's URL is not a host the canonical guardrail
    considers safe to fire at.

    `client` is the injectable HTTP client. None means a real network call
    through the default transport client; tests must pass a fake.
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

    _validate(count, seed)
    pairs = _build_corpus(mix, count, seed)

    records = []
    for index, (email, label) in enumerate(pairs):
        result = post(email, url, client=client)
        response = {
            "status": result.status,
            "latency_ms": result.latency_ms,
            "body_snippet": result.body_snippet,
        }
        records.append(report.build_record(email, label, seed, index, response))

    artifact = report.build_artifact(
        seed,
        {
            "mix": _selected_labels(mix),
            "count": count,
            "seed": seed,
            "dry_run": False,
            "target": target,
        },
        records,
    )
    return artifact
