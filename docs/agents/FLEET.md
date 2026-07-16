# Fleet roster

Seven scheduled tasks. GitHub is the source of truth; coordination is the local
bus at `Startup/fleet/` governed by `docs/fleet/PROTOCOL.md`. Single-responsibility
and dependency-ordered: coders push branches, the reviewer opens PRs, the
integrator is the single merge point, the watchdog detects, the digest is the one
thing Nox reads.

| Agent | Role | Model | Schedule (local, tunable) | Lane / notes |
|---|---|---|---|---|
| planner | Weekly goal + backlog top-up + request triage + drift flag | Opus | Mon and Thu 7:00am | Light at this size; folds strategist and dispatch |
| coder-a | Engine/core coder | Sonnet | 1:15pm | Lane A: `testinghq/core/{config,transport,ratelimit,report}.py`, `testinghq/blast/**`, `testinghq/cli.py`, `tests/unit/**`. NOT `core/guardrails.py`. Push branch, never merge |
| coder-b | UI + integration + examples coder | Sonnet | 2:15pm | Lane B: `web/**`, `examples/**`, `tests/integration/**`. Push branch, never merge |
| reviewer | Quality gate, opens PRs | Sonnet | 3:15pm | Reviews both coder branches in-window; never merges |
| integrator | Single merge point | Sonnet | 4:40am | Merges green only, one flake-retry, never resolves conflicts |
| watchdog | Detect-and-report sentinel | Sonnet | 4:55am | Never fixes, never opens a PR. Bumped from Haiku: Haiku proved unreliable here in testing |
| digest | Single human report | Sonnet | 5:15am | Broken-first. The only report Nox reads |

Models are set in the sidebar UI, not via the create API. No Haiku anywhere:
Haiku flails on both the merge gate and the watchdog role.

## Windows

Build squad (coder-a, coder-b, reviewer) shares the afternoon window so the
reviewer sees both branches. The review pipeline (integrator, watchdog, digest)
clusters in the cheap early morning, ahead of the planner. Roughly two agents per
5-hour window, a negligible load on top of HandleHQ. All times are tunable.

## Non-negotiables (from day one)

- The PROTOCOL and its local bus; branch-per-agent; single merge point.
- TIME GUARD and bounded CI-fix: at most two fix attempts, never loop, a red PR
  left open for the integrator is the correct outcome.
- Prove the CI gate can go red before trusting green (done: PR #1). Zero
  quarantine list; if a skip is ever needed, track it and only ever shrink it.
- Never fix a test to match the code. Broken-first reporting. No em-dashes.
- `core/guardrails.py` is the security lane's and no coder's. A coder imports it
  and never edits it. The lane with a motive to weaken a check so a test passes
  must not be the lane able to edit the check. Lane A read as owning
  `testinghq/**` until 2026-07-16, which handed it the guardrails by accident.
- A lane may not import another lane's unmerged module. See the collision rule in
  GOALS.md. Sharing no files is not the same as having no dependency, and the
  difference is a branch that cannot go green on its own.
