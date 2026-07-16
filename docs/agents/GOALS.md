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

- Lane A (engine/core): `testinghq/core/{config,transport,ratelimit,report}.py`,
  `testinghq/blast/**`, `testinghq/cli.py`, and their unit tests under
  `tests/unit/**`.
- Lane B (UI, integration, examples): `web/**`, `examples/**`, and integration and
  end-to-end tests under `tests/integration/**`.
- Security lane: `testinghq/core/guardrails.py`, `tests/security/**`,
  `docs/SECURITY.md`, `.github/**`.

`testinghq/core/guardrails.py` belongs to the security lane and to nobody else.
Lane A imports it and never edits it. This is deliberate: the lane that has an
incentive to weaken a check in order to turn a test green must not be the lane
that can edit the check. Lane A previously read as owning all of
`testinghq/core/**`, which silently included guardrails.py and gave away that
separation.

Collision rule, part one (files): the lanes must never touch the same files.

Collision rule, part two (imports): a lane must never import a module that
another lane has not yet landed on `main`. Respecting part one is not enough. On
2026-07-16 Lane B wrote `tests/integration/fake_sink.py` importing
`testinghq.blast.serialize`, a Lane A module that existed only on an unmerged
branch. No file was shared, so part one was satisfied, and the branch was still
red on its own and could not pass CI until Lane A merged. If your next item needs
a module from another lane's unmerged branch, you are blocked. Record it in
HANDOFF.md and take the next item in your lane instead.

## Pinned by Nox

(none yet)

## Progress / drift

- 2026-07-16 (Thu, 07:00, planner): M1 on-track, not yet started. M0 scaffold is
  done and on main. No PRs merged toward M1 yet; PR #1 (prove-red gate) served
  its purpose and was closed unmerged as designed. Backlog seeded: Lane A 5
  not-done items, Lane B topped up from 2 to 4. First afternoon build window
  still ahead. No blockers, no open requests, no open issues.

- 2026-07-16 (Thu, 14:55, Nox): corrects the 07:00 entry, which went stale the
  moment the build window opened and would have stayed stale until Monday. The
  window ran. Both coders pushed. The fleet is now paused in full at Nox's
  direction while the assignment pack is run by hand.
  - Lane A pushed `agents/coder-a-2026-07-16`: `blast/payload.py`,
    `blast/serialize.py`, 293 lines of unit tests. Green standalone, 28 passed.
    Opened as PR #3.
  - Lane B pushed `agents/coder-b-2026-07-16`: the fake sink, the happy-path
    integration test, `examples/target.example.toml`, `examples/README.md`. Red
    standalone. See the import clause added to the collision rule above. Blocked
    on PR #3 and mergeable only after it lands.
  - Lesson recorded, not just the fix: the 07:00 note claimed a state and no
    agent was scheduled to check whether the claim survived contact with the
    afternoon. A drift note written before the work happens is a forecast. Only
    the digest reconciles it, and it reconciles overnight.
