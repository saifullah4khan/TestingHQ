# Fleet Comms Protocol

Read this before you write anything anywhere.

This describes the TestingHQ autonomous fleet's local message bus. GitHub is the
source of truth for the product and durable docs. The bus is the scratchpad for
today's run: run logs, dispatch orders, blockers, handoffs. The bus is NOT in
git.

## Bootstrapping the bus

The live bus lives locally at `Startup/fleet/` in this Cowork space and is not
committed. This file is the canonical copy. On your first run, if
`Startup/fleet/` does not exist, create it and seed `Startup/fleet/PROTOCOL.md`
from this file (`docs/fleet/PROTOCOL.md` in the repo), plus empty `RUNLOG.md`,
`BLOCKERS.md`, `HANDOFF.md`, `DISPATCH.md`, and `WATCHDOG.md`. Then proceed.

## The rule

A pull request is for a change to the product or to durable docs. It is not a
status update, not a way to tell another agent something, not a receipt proving
you ran. Ask: would a new engineer joining in six months need this? Yes goes to
GitHub in a real doc in a real PR. No, it is about today's run, goes here.

### Never open a PR for

- A run that changed nothing. Write one line in RUNLOG.md.
- A status report. Write to RUNLOG.md or STATUS.md.
- Telling another agent something. Write to HANDOFF.md.
- A dispatch or backlog top-up that changed no durable file. Write to DISPATCH.md.

## RUNLOG.md line format

`YYYY-MM-DD | agent | STATUS | note` (note <= 100 chars, no prose, no em-dashes)

STATUS: N/A (ran, nothing to do) | OK (work done, no PR) | PR#123 | MERGED#123 |
BLOCKED (+BLOCKERS.md) | FAIL (+BLOCKERS.md) | SKIP (did not run)

## Files

- PROTOCOL.md (Nox, permanent)
- RUNLOG.md (all, append-only, trimmed monthly)
- DISPATCH.md (planner, per run)
- STATUS.md (reviewer or status writer, per run)
- WATCHDOG.md (watchdog, per run)
- BLOCKERS.md (any, until cleared)
- HANDOFF.md (any, until read)
- notes/ (coders, pruned weekly)

Append-only means append-only. Never rewrite another agent's line. Mark blockers
`CLEARED <date>`, do not delete.

## Handoffs

`TO: <agent> | FROM: <agent> | <date> | <one line>`

The recipient deletes its own line once actioned and logs OK. `TO: nox` lines are
collected by the Daily Digest and never deleted by an agent.

## Why this exists

On HandleHQ a week of process-paperwork PRs buried the signal while the required
check was incapable of failing for 22 days and nobody noticed. Keep the PR list
meaningful. If it is worth a PR, it is worth reviewing.
