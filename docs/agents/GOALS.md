# Goals

## Standing goal

Ship Blast v1: from one `pip install testinghq`, generate a deterministic, seeded
corpus of inbound-email payloads spanning clean through garbled, fire it (dry-run
by default, explicit and rate-limited when live) at a configured target, and
report category-versus-outcome so it points at real parser bugs. Ship the branded
web UI over the same engine inside v1. Full spec: the v1 build spec in the
founding materials, milestones M0 through M5.

## Current milestone: M1 (clean path end to end)

M0 (scaffold, packaging, guardrails, provable-red CI) is done and on main. The
squad now builds the clean path: the InboundEmail model and Inbound Parse
serialization, the clean generator, the transport, the fake sink, and the first
real tests. Everything laddered here.

## Squad

One build squad, two lanes, run in one shared window so the reviewer sees both
branches:

- Lane A (engine/core): `testinghq/core/**`, `testinghq/blast/**`, and their unit
  tests under `tests/unit/**`.
- Lane B (UI, integration, examples): `web/**`, `examples/**`, and integration and
  end-to-end tests under `tests/integration/**`.

Collision rule: the lanes must never touch the same files.

## Pinned by Nox

(none yet)

## Progress / drift

(filled by the planner each run)
