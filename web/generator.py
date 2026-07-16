"""Deterministic, fixture-backed stand-in for the Blast engine.

`testinghq/blast/generate.py`, `core/transport.py`, and `core/report.py` do
not exist yet on this branch (they are being built in a parallel lane). This
module produces run artifacts that match the documented RUN ARTIFACT SCHEMA
so the web UI has something real to render and test against, without ever
touching the network or importing engine code that isn't there.

Same seed plus same mix plus same count always yields the same artifact
(byte-identical after JSON serialization). No wall-clock time, no unseeded
randomness.

When the real engine lands, `web/adapter.py` is the only place that needs to
change: swap the call to `generate_run()` for a call into
`testinghq.blast.generate` / `testinghq.core.transport`.
"""
from __future__ import annotations

import hashlib

from . import expectations

CATEGORIES = expectations.CATEGORIES


class GeneratorError(ValueError):
    """Raised when the requested mix/count/seed is invalid."""


def _selected_categories(mix):
    if not mix:
        return list(CATEGORIES)
    unknown = [c for c in mix if c not in CATEGORIES]
    if unknown:
        raise GeneratorError(f"unknown categories in mix: {unknown!r}")
    # Preserve canonical order, not caller-provided order, so output is
    # deterministic regardless of how the mix list was assembled.
    return [c for c in CATEGORIES if c in mix]


def _status_for(category, n):
    """Deterministic status code for the n-th occurrence of a category.

    n already folds in the seed (see generate_run), so this function itself
    has no hidden state - same n always yields the same status.
    """
    if category == expectations.CLEAN:
        # 1 in 5 clean payloads fails to 2xx - a real parser bug, must FAIL.
        return 200 if (n % 5) != 4 else 422
    if category == expectations.MESSY_BUT_VALID:
        return 200 if (n % 7) != 6 else 422
    if category == expectations.MULTILINGUAL_GIBBERISH:
        return 200 if (n % 3) != 2 else 400
    if category == expectations.STRUCTURALLY_MALFORMED:
        return 400 if (n % 4) != 3 else 422
    if category == expectations.DEGENERATE:
        m = n % 9
        if m == 8:
            return 500  # the parser choked on garbage - FAIL
        if m == 7:
            return None  # hung - FAIL
        return 400  # cleanly rejected - PASS
    return 200


def _latency_for(status, n):
    if status is None:
        return 5000.0  # hit the timeout ceiling
    return float(20 + (n * 37) % 300)


def _body_snippet_for(status):
    if status is None:
        return ""
    if 200 <= status < 300:
        return "202 accepted" if status == 202 else "ok"
    if 400 <= status < 500:
        return f"rejected: HTTP {status}"
    return f"error: HTTP {status}"


def _record_id(category, seed, k):
    return f"{category}-{seed}-{k:04d}"


def _payload_sha256(category, seed, k):
    digest_input = f"{category}|{seed}|{k}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def _intended_for(category, seed, k):
    return {
        "from": f"sender{k}@example.com",
        "subject": f"[{category}] synthetic test subject {seed}-{k}",
        "body_core": f"Synthetic body for a {category} payload, record {k}.",
        "attachments": k % 3,
    }


def _record_for(category, seed, k, n):
    status = _status_for(category, n)
    latency = _latency_for(status, n)
    record = {
        "id": _record_id(category, seed, k),
        "category": category,
        "payload_sha256": _payload_sha256(category, seed, k),
        "intended": _intended_for(category, seed, k),
        "response": {
            "status": status,
            "latency_ms": latency,
            "body_snippet": _body_snippet_for(status),
        },
        "assertion": {"passed": True, "mismatches": []},
    }
    outcome = expectations.classify_record(record)
    if outcome != expectations.OK:
        record["assertion"] = {
            "passed": False,
            "mismatches": [expectations.flag_for_record(record)],
        }
    return record


def generate_run(mix, count, seed, *, target=None, dry_run=True):
    """Build a full run artifact matching the documented schema.

    mix: list of category names to include (falsy/empty means all five).
    count: total number of records to generate.
    seed: integer seed; same seed always yields the same artifact.
    target: configured target name, only meaningful when dry_run is False.
    dry_run: whether this run represents a dry-run (no send) or a fire.
    """
    if not isinstance(count, int) or count < 0:
        raise GeneratorError("count must be a non-negative integer")
    if not isinstance(seed, int):
        raise GeneratorError("seed must be an integer")

    selected = _selected_categories(mix)

    records = []
    per_category_counter = {c: 0 for c in selected}
    for i in range(count):
        category = selected[i % len(selected)]
        k = per_category_counter[category]
        per_category_counter[category] = k + 1
        n = k + seed
        records.append(_record_for(category, seed, k, n))

    config = {
        "mix": selected,
        "count": count,
        "seed": seed,
        "dry_run": dry_run,
        "target": target,
    }

    return {
        "seed": seed,
        "config": config,
        "summary": expectations.compute_summary(records, seed, config),
        "records": records,
    }
