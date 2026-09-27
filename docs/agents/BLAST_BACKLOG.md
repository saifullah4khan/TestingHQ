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

## Staleness markers, and why this file has them

This file has gone stale twice in one day. The 07:00 planner note said M1 was not
yet started while 1,235 lines of it sat on a branch. It was hand-corrected at
15:50, and by 17:07 it was lying again: it still said M3 was IN PROGRESS and
Barrage was BLOCKED, 40 minutes after M3 merged and unblocked Barrage. Both times
the file was true when written. Both times nothing was watching it.

A backlog encodes state that `main` already knows. Hand-syncing it will always
rot, and a rotted backlog is worse than an empty one: a coder reads "do not claim
this" and improvises instead.

So an item that will become false when some file appears declares that inline, as
an HTML comment of this shape (spelled here with the marker word split, so this
example does not arm itself):

    < !-- stale-if-exists: testinghq/some/future_module.py -- >

Write it as a normal HTML comment with no spaces after the angle brackets, and
name a path that does not exist yet.

`tests/test_backlog_freshness.py` fails the build if that path exists. The claim
above it is then provably stale and someone has to update this file. It makes a
claim falsifiable, which is the whole point: a claim that cannot be proven wrong
is exactly the kind that quietly misleads the next agent.

The first version of this example named a real in-flight path and armed itself the
moment that file landed, which turned the guard into a false alarm about its own
documentation. An example of a tripwire must not be a tripwire.

Add a marker to any item you write that a future merge will invalidate. If you
cannot express the condition as a path, say plainly in the item what would make it
false, so a human can check in one glance.

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

## M3 - reporting and reproducibility: DONE

Blast v1 is complete. Landed #16.

- [x] [A] Run artifact and replay. Replay verifies byte-identical payload hashes
  before touching the network and refuses on mismatch.
- [x] [A] Category-versus-outcome summary, expectation-based. A degenerate input
  returning a clean 4xx is a PASS; a 5xx or a timeout is a FAIL; a clean input not
  returning 2xx is a FAIL. The summary points at bugs, not statuses.
- [x] [A] Matcher protocol and StatusOnlyMatcher.
- [ ] [B][M] Reporting and replay integration tests. NOT DONE and NO LONGER
  BLOCKED: `core/report.py` is on main. Claim freely.

## UI v1: DONE, with one real follow-up

- [x] [B] Branded `web/` shell over the engine. Landed #11. Controls, streaming
  results table, category-versus-outcome panel, dry-run default, explicit confirm.
  Guardrails delegate to `core/guardrails.py`; do not reintroduce a local copy.
- [x] [B][M] Swap `web/adapter.py` from the fixture stand-in to the real engine.
  DONE. `dry_run()` and `fire()` now build the corpus with
  `blast.generate` + `blast.corrupt`, put it on the wire with `core.transport`,
  and build records with `core.report`. The seam held: the change is confined
  to `web/adapter.py` plus a client parameter, and nothing else in `web/`
  learned anything new. `web/generator.py` and the fixtures are kept, because
  `tests/web/test_generator.py` still exercises the fixture path directly and
  because the fixtures are the cross-check corpus the engine lane depends on.
  Two things this exposed, both now guarded:
  - The fire path could open a real socket, and `web/targets.json`'s first
    entry is a localhost URL, so a test that forgot to inject a client made a
    genuine request and waited out the transport timeout. `web/server.py` now
    holds the client in a module-level `HTTP_CLIENT` seam, the server tests pin
    it with an autouse fixture, and `tests/web/test_adapter.py` replaces the
    default transport client with a raising stub so a forgotten injection fails
    in milliseconds instead of passing quietly. This was not theoretical: a
    duplicate test definition left behind during the swap added 12 seconds to
    the suite and still passed.
  - A dry run has no responses, so computing its summary with the normal rules
    reports every payload as a timeout and flags every degenerate one as a
    failure. Both true, both nonsense. The dry-run summary is built explicitly
    and there is a test saying why.
- [x] [B][M] Delegate `web/expectations.py` to `core/report.py`. DONE. It is now a
  re-export of the engine's rules and defines none of them. The docstring records
  why, because the reason is not obvious from the diff.
  The backlog framed this as one duplicated module. It was two.
  `web/static/app.js` also carried a `classifyRecord()` that re-derived each
  record's outcome from its status code, making the browser a third copy of
  `core/report.py` that no test could reach, because no CI here runs JavaScript.
  That copy is gone: the server now annotates every record with the engine's
  verdict and the browser renders it. The annotation is response-layer only, so
  the on-disk artifact schema and the fixtures are untouched, which matters
  because Barrage replays those artifacts and a display field does not belong in
  them.
  Two structural guards added to `tests/test_lane_hygiene.py`, both proven red by
  re-inlining the thing they forbid and confirmed clean after reverting: a rule
  body in `web/expectations.py`, and a `classifyRecord` or
  `is2xx`/`is5xx`/`isTimeout` helper in `web/static/app.js`.
  `tests/unit/test_report.py`'s cross-check between the two implementations was
  replaced rather than kept. Comparing two implementations could only ever go red
  on a fixture that happened to exercise a changed rule, which is the failure it
  existed to catch, and with one implementation it was vacuous. It is now an
  identity check plus a data-level check that the fixtures classify to the flags
  they claim.
- [ ] [B][M] Delegate `web/expectations.py` to `core/report.py`. The two copies
  agree today, verified record by record, and `tests/unit/test_report.py`
  cross-checks them. That cross-check stops meaning anything the moment the
  adapter above started building records with the real engine, because the UI's
  records now come from `core/report.py` while this copy keeps classifying them.
  This is now the top open item.

## Barrage v1: DONE

Full spec in the assignment pack (05 spec, 06 handoff). The prerequisite was Blast
v1, meaning M3 merged so `transport`, `config`, `guardrails`, `ratelimit`, and
`report` are stable. That happened in #16.

This section is being updated in the same PR that lands the code, because
`tests/test_backlog_freshness.py` turned the build red the moment
`testinghq/barrage/runner.py` appeared. That is the guard working as designed on
its first live run, and it is what "keep docs current in the same PR as the code"
looks like when it is enforced rather than requested.

- [x] [A] `barrage/runner.py`. Concurrency and rate control, closed-loop
  fixed-concurrency and open-loop fixed-arrival-rate, a warmup ramp, a steady-state
  hold, and a hard rate-and-duration ceiling that needs an explicit flag to raise.
- [x] [A] Barrage reporting in `barrage/report.py`. Throughput achieved versus
  target, latency p50/p90/p99, error rate over time, and the knee where the
  endpoint degrades. JSON artifact plus a human summary.
- [x] [A] `testinghq barrage fire` CLI in `barrage/fire.py`, dry-run default, plus
  replay.

### Known bug found by this lane, since fixed in Lane A

`core/ratelimit.py`'s `TokenBucket.acquire()` could spin forever under a purely
additive injected clock when the rate's reciprocal is not exactly representable in
binary. It computed `wait_for = deficit / rate`, advanced by exactly that, then
refilled by `wait_for * rate`, which rounds to just under `deficit`. The residual
is about 1e-16, the next `wait_for` about 1e-17, and adding 1e-17 to a clock reading
around 0.67 is a no-op at float precision: elapsed becomes 0, no refill happens,
and the loop never exits.

Under a real monotonic clock it self-heals, because the clock ticks regardless, so
it is invisible in production and fatal under the injected clocks this repo
mandates. Note the implication for how it was missed: `tests/unit/test_ratelimit.py`
drove the blocking `acquire()` at rates 1 and 2 only, both exactly representable,
and used rate 10 only through the non-blocking `try_acquire()`, which cannot spin.
Nobody chose 1 and 2 for any reason at all. The tests guarding the rate limiter
were green for a reason unrelated to whether the module worked.

Measured on 2026-07-27, one blocking process per rate against the pre-fix module:
3, 5, 6, 7, 9, 10, 11, 12, 13 and 25 all hang. Only 1, 2, 4, 8 and 16, the
powers of two, plus sub-unit rates with an integer reciprocal, terminate.

- [x] [A][M] Fix `TokenBucket` so it tolerates float residue. Compute the wait
  once as an absolute deadline rather than accumulating per-pass increments, and
  floor the wait at one nanosecond so the loop has a guaranteed exit. The
  regression is parameterised over 3, 5, 6, 7, 9, 10 and 11 with an injected
  clock, and asserts both that the loop terminates and that it still waits the
  full earned time, so the floor cannot quietly grow into a licence to pace
  faster than configured. The security lane's gate contract test is parameterised
  the same way and now drives the real bucket, not only the fake, so it can no
  longer pass by luck.


Barrage reuses `blast/generate` for clean payloads. It does not re-garble: Blast
proves the parser is correct under messy input, Barrage proves the pipeline holds
under load. The rate ceiling, the configured-target rule, and the dry-run default
are what keep it a load tester against your own infrastructure and not a flooding
tool. That framing goes in every prompt, README, and doc.
