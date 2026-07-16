# Blast backlog

Priority-ordered, one-session-sized, lane-split, size-tagged. Coders self-direct
from this file. Lane A claims A items, Lane B claims B items. If your items are
done or missing, pick the highest-priority not-done item in your lane that
advances the current milestone.

Last reconciled against `main` on 2026-07-16 at 15:50 by Nox, after the
assignment pack was run by hand with the fleet paused. Everything ticked below
was verified present on `main`, not assumed. If you are a coder waking up to
this file, read the "Claim rules" section before you claim anything.

## How this squad runs

Deterministic given a seed is non-negotiable: same seed plus same config yields
byte-identical output. Never let wall-clock time or unseeded randomness leak into
generated output (seed attachment bytes too). Guardrails are first-class and
already exist in `core/guardrails.py`; extend, never weaken them. `guardrails.py`
belongs to the security lane: import it, never edit it. Tests must be real and
hermetic (no network; inject clocks and transports). Never fix a test to match
the code.

Run tests the way CI runs them: `pytest -q`, not `python -m pytest -q`. The two
disagreed until `pythonpath = ["."]` landed in pyproject.toml, and a check that
is more permissive than the gate manufactures false green.

## Claim rules

- Do not claim an item marked IN PROGRESS. Someone is already on it and you will
  collide.
- Do not claim an item marked BLOCKED. Read what it is blocked on first.
- Do not import a module another lane has not yet landed on `main`. Sharing no
  files is not the same as having no dependency. See the collision rule in
  GOALS.md.

## M1 - clean path end to end: DONE

- [x] [A][M] InboundEmail model in `blast/payload.py`. Landed #3.
- [x] [A][M] Inbound Parse multipart serialization in `blast/serialize.py`. Landed #3.
- [x] [A][M] Clean generator in `blast/generate.py`. Landed #10.
- [x] [A][M] Transport in `core/transport.py`. Landed #10.
- [x] [B][M] Fake in-process sink in `tests/integration/fake_sink.py`. Landed #5.
- [x] [B][S] `examples/demo.py`. Landed #10.
- [x] [B][M] Happy-path integration test. Landed #5.
- [x] [B][S] `examples/target.example.toml` plus `examples/README.md`. Landed #5.
- [x] [A][S] Determinism test. Landed #10. Verified byte-identical on a fixed seed.

## M2 - chaos: DONE except one Lane B item

- [x] [A] Messiness levels and the mutator pipeline in `blast/corrupt.py`. Landed #10.
  The five categories are weighted recipes over one mutator set, not separate code paths.
- [x] [A] Gibberish and encoding-sabotage mutators. Landed #10. Note: encoding
  sabotage was a silent no-op on ASCII content when first written, because UTF-8,
  Latin-1, cp1252 and Shift_JIS agree byte for byte below 0x80. Fixed by splicing
  a non-ASCII marker in. Verified genuinely corrupting, 40 of 40.
- [x] [A] Attachment generation in `blast/attachments.py`. Landed #10. Bytes seeded.
- [x] [A] Named edge-case catalog in `blast/catalog.py`. Landed #10. 20 cases.
- [ ] [B][M] Mutator and category-mix integration tests under `tests/integration/`.
  NOT DONE and NOT BLOCKED: `corrupt.py` is on `main`, claim this freely.
  `tests/unit/test_corrupt.py` covers the mutators in isolation (Lane A). What is
  missing is the Lane B integration view: a fixed-seed corrupted corpus posted to
  the fake sink, asserting the sink receives every category intact and that the
  mix ratios hold end to end.

## M3 - reporting and reproducibility: IN PROGRESS, DO NOT CLAIM [A] ITEMS

Nox is building all three [A] items right now on `feat/engine-m3`. They will be
on `main` before the next build window. Claiming them means writing a competing
implementation of code that is hours from landing.

- [ ] [A] IN PROGRESS (Nox, feat/engine-m3): run artifact and replay.
- [ ] [A] IN PROGRESS (Nox, feat/engine-m3): category-versus-outcome summary.
- [ ] [A] IN PROGRESS (Nox, feat/engine-m3): generic assertion hook and
  StatusOnlyMatcher.
- [ ] [B][M] BLOCKED on the three items above: reporting and replay integration
  tests. Unblocks the moment `core/report.py` is on `main`.

## UI v1: DONE, with one real follow-up

- [x] [B] Branded `web/` shell over the engine. Landed #11. Controls, streaming
  results table, category-versus-outcome panel, dry-run default, explicit confirm.
  Guardrails delegate to `core/guardrails.py`; do not reintroduce a local copy.
- [ ] [B][M] Swap `web/adapter.py` from the fixture stand-in to the real engine.
  NOT DONE and NOT BLOCKED. This is the highest-value Lane B item available.
  The adapter was deliberately built as a one-seam swap for exactly this, and the
  engine landed in #10. `web/adapter.py` still imports `web/generator.py`, the
  deterministic fixture stand-in written when `blast/generate.py` did not exist.
  Replace those calls with `blast.generate.generate_corpus`, `blast.corrupt.corrupt_corpus`,
  and `core/transport`. Keep the seam: the point is that this stays a one-file change.
  Keep `web/generator.py` and the fixtures; `tests/web` uses them and they are
  what let this lane ship without the engine.

## Next milestone: Barrage v1, Lane A, BLOCKED until M3 lands

Full spec in the assignment pack (05 spec, 06 handoff). Prerequisite is Blast v1,
meaning M3 merged, so `transport`, `config`, `guardrails`, `ratelimit`, and
`report` are stable. Barrage is defined entirely as reuse of those five. Four
existed only as stubs this morning. Do not start it before `core/report.py` is on
`main`: you would be inventing the interfaces you are supposed to consume, then
rewriting them.

- [ ] [A] BLOCKED on M3: `barrage/runner.py`. Concurrency and rate control,
  closed-loop fixed-concurrency and open-loop fixed-arrival-rate, a warmup ramp, a
  steady-state hold, and a hard rate-and-duration ceiling that needs an explicit
  flag to raise.
- [ ] [A] BLOCKED on M3: Barrage reporting. Throughput achieved versus target,
  latency p50/p90/p99, error rate over time, and the knee where the endpoint
  degrades. JSON artifact plus a human summary.
- [ ] [A] BLOCKED on M3: `testinghq barrage fire` CLI, dry-run default, plus replay.

Barrage reuses `blast/generate` for clean payloads. It does not re-garble: Blast
proves the parser is correct under messy input, Barrage proves the pipeline holds
under load. The rate ceiling, the configured-target rule, and the dry-run default
are what keep it a load tester against your own infrastructure and not a flooding
tool. That framing goes in every prompt, README, and doc.
