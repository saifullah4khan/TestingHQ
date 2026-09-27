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

## M2 - chaos: DONE

- [x] [A] Messiness levels and the mutator pipeline in `blast/corrupt.py`. Landed #10.
  The five categories are weighted recipes over one mutator set, not separate code paths.
- [x] [A] Gibberish and encoding-sabotage mutators. Landed #10. Note: encoding
  sabotage was a silent no-op on ASCII content when first written, because UTF-8,
  Latin-1, cp1252 and Shift_JIS agree byte for byte below 0x80. Fixed by splicing
  a non-ASCII marker in. Verified genuinely corrupting, 40 of 40.
- [x] [A] Attachment generation in `blast/attachments.py`. Landed #10. Bytes seeded.
- [x] [A] Named edge-case catalog in `blast/catalog.py`. Landed #10. 20 cases.
- [x] [B][M] Mutator and category-mix integration tests under `tests/integration/`.
  DONE, in `tests/integration/test_corpus_generation.py`. A fixed-seed corpus is
  generated, corrupted per `DEFAULT_MIX`, serialized, and posted to the fake sink.
  Covers: every mutator is referenced by at least one recipe and vice versa; each
  recipe can be built in isolation; `clean` leaves payloads byte-identical; every
  non-clean recipe actually changes something; all five categories are reachable
  from the default mix; the observed distribution tracks the configured weights
  (loosely, since these are random draws); and every payload survives the real
  wire format.
  Writing it found that the ground-truth contract is subtler than the existing
  happy-path test assumed. The generator puts the RFC 5322 display-name form in
  the header and the bare addr-spec in ground truth, and puts a full message in
  `text` but only the substantive sentence in `body_core`. The hand-built list in
  `test_intake_happy_path.py` used bare addresses and set `body_core == text`, so
  its equalities held only for a shape the generator never emits.

## M3 - reporting and reproducibility: DONE

Blast v1 is complete. Landed #16.

- [x] [A] Run artifact and replay. Replay verifies byte-identical payload hashes
  before touching the network and refuses on mismatch.
- [x] [A] Category-versus-outcome summary, expectation-based. A degenerate input
  returning a clean 4xx is a PASS; a 5xx or a timeout is a FAIL; a clean input not
  returning 2xx is a FAIL. The summary points at bugs, not statuses.
- [x] [A] Matcher protocol and StatusOnlyMatcher.
- [x] [B][M] Reporting and replay integration tests. DONE, in
  `tests/integration/test_reporting_and_replay.py`. Drives the real CLI send and
  replay paths with an injected client. Covers: a fire run's artifact is exactly
  what `core.report` derives from the same records; a 500 from the endpoint is
  recorded as a 5xx and flags every clean payload; a raising client is recorded
  as a timeout rather than an invented status; replay re-fires byte-identical
  request bodies; replay refuses a tampered payload hash without firing; and a
  dry-run replay makes zero network calls.
  Writing it found a real defect: **`blast replay --send` had no injectable HTTP
  client.** `_run_fire` takes one, but `_cmd_replay` never passed one, so the
  replay send path opened real sockets and nothing about it could be tested
  hermetically. The first run of the new test spent 81 seconds making 40 real
  connection attempts before failing. Fixed by threading a `client` parameter
  through `_cmd_replay` exactly as the fire path already had. The fire path's own
  docstring calls out that `client` exists for hermetic tests; replay had no
  equivalent and no one had noticed because no test existed.

## UI v1: DONE

- [x] [B] Branded `web/` shell over the engine. Landed #11. Controls, streaming
  results table, category-versus-outcome panel, dry-run default, explicit confirm.
  Guardrails delegate to `core/guardrails.py`; do not reintroduce a local copy.
- [x] [B][M] Swap `web/adapter.py` from the fixture stand-in to the real engine.
  DONE. `dry_run()` and `fire()` now build the corpus with
  `blast.generate` + `blast.corrupt`, put it on the wire with `core.transport`,
  and build records with `core.report`. The seam held: the change is confined
  to `web/adapter.py` plus a client parameter, and nothing else in `web/`
  learned anything new. `web/generator.py` and `tests/web/test_generator.py`
  were kept at the time, and have since been deleted; see the entry below.
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
- [x] [B] Delete `web/generator.py` and `tests/web/test_generator.py`. DONE. It was
  the deterministic stand-in for the real engine, and once #22 moved the adapter
  onto the engine it became dead code that still looked authoritative: a
  `generate_run()` that produced convincing artifacts, a category list, and its
  own `GeneratorError`. Nothing would have complained if some later piece of code
  had imported it and quietly served the UI fixtures instead of the engine, which
  is the exact failure mode this repo has already suffered from three times with
  duplicated rules.
  The only live surface was `GeneratorError` and `CATEGORIES`. The error moved to
  `web/adapter.py`, keeping its name and its `ValueError` base so `web/server.py`'s
  HTTP 400 mapping did not have to change, and the category list comes from
  `core.report` directly. `tests/test_lane_hygiene.py` now fails if anything
  imports the module, verified red by adding a stray import.
  The shipped run artifacts under `web/tests/fixtures/` are kept: they are data,
  not a generator, and nothing in the tree produces them any more, so a guard
  pins their presence.
- [x] [B][S] Fix the self-contradicting web fixture. `sample_run_with_failures.json`
  declared `by_status_class.5xx: 1` with two 500s in its own records. The defect was
  known and, worse, encoded: `tests/unit/test_report.py` carried a comment
  explaining the wrong number and then asserted the corrected one against a
  hardcoded literal, so the suite stayed green while the artifact the UI demos
  from stayed wrong. The fixture is corrected and the test now compares against
  the file. `tests/web/test_fixtures_schema.py` recounts the records by hand, so
  the file and the records cannot disagree again without going red.


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

## M4 - the readback seam: DONE, tools landing on top

The blind spot the three finished tools share. Blast and Barrage both judge a run
by the HTTP status, and a 200 only means the endpoint accepted the POST. It does
not mean a ticket was created, that the sender survived parsing, that the body
arrived whole, that the attachments came through, or that the message was routed
anywhere useful. This milestone adds the seam those tools are built on: a way to ask a system
what it produced, and the ground truth to check the answer against. It is the
first work in this repository that could fail a pipeline which was answering
200 the whole time. `verify` and `ledger` are built on it here; `redeliver` follows in its own
pull request.

This section is being written in the same PR that lands the code, which is the
house rule and not an accident: the file encodes state `main` already knows, and
hand-syncing it has rotted twice already.

- [x] [A] The readback seam in `pipeline/readback.py`. A `ReadbackAdapter` is
  `fetch(probe) -> Sequence[Readback]`, and the sequence is the point: one record
  is normal, none is a loss, several is a duplicated message, which is a real
  intake bug and not an error condition. `list_all` is optional, because not every
  system can be enumerated, and the tools say "not searched" rather than "none
  found" when it is absent.
- [x] [A] Per-message tagging in `pipeline/messages.py`. A tag stamped into a
  header, the body, the HTML and the Message-ID, derived from (prefix, seed,
  index) and nothing else. Deliberately NOT the subject: a subject carrying a
  synthetic suffix is not the subject that was sent, so verifying it would compare
  a mangled expectation against a mangled result and pass. The prefix exists so
  two concurrent runs against one system cannot read each other's records.
- [x] [A] The check layer in `pipeline/expectations.py`. Six checks (ticket
  created, sender, subject, body, attachments, routing) plus the two threading
  checks, each resolving to PASSED, FAILED or SKIPPED. SKIPPED is the load-bearing
  one: a field the adapter could not see is never reported as passing, because a
  check that cannot report "unknown" is a check that will eventually report "fine"
  about something it never read. `ticket_created` is the single exception and never
  skips, since a lookup that found nothing is the most consequential thing these
  tools can report.
- [x] [A] `GroundTruthMatcher`, a `core.report.Matcher` that grades on content.
  The join with everything Blast already does: the same three-argument `match`
  signature, so a verify artifact is a blast artifact with a stricter assertion.
  It does not re-grade status, which is `core/report.py`'s job, because a third
  copy of the expectation rules is exactly what `tests/test_lane_hygiene.py`
  exists to prevent.
- [x] [A] Adapters in `pipeline/adapters.py`. `http` and `mailbox` need no code,
  anything else is `--readback module:attribute`, and a plain function taking a
  Probe is a complete adapter. The readback URL is gated through the canonical
  guardrail in one call site, because on a real deployment the readback is a
  ticket store or a mail sink that may hold other people's data.
  already happened. Fires the CLEAN corpus only, for a structural reason: verify
  grades results, and a deliberately mangled payload has no correct parse to grade
  against, so verifying one would report a failure every time a mutator did its
  job. `verify check` verifies the clean records of a mixed blast run and prints
  how many it skipped and why.
  messages: missing, duplicated, extra, wrong, each a different bug with a
  different fix and reported separately. `strays_searched` is null rather than
  false when the adapter cannot enumerate, and an unsearched stray hunt does not
  count as balanced, so a lookup-only adapter cannot pass a CI gate by accident.
  slow-retry, reply-first, references. Threading is two checks, `thread_link` for
  the headers and `thread_together` for whether the reply ended up on its
  parent's ticket, because correct headers on two separate tickets is a real and
  common outcome and the two have different fixes.
- [x] [A] `[readback]` in `core/config.py`, carried through unparsed. Validated
  only as a table; the shape and meaning of it belong to the tools that read it,
  and parsing it in the security lane's file would make the core loader know
  about a tool that did not exist when it was written.
- [x] [A] `verify` in `pipeline/verify.py`, plus `verify check` for a run that
  already happened. Fires the CLEAN corpus only, for a structural reason: verify
  grades results, and a deliberately mangled payload has no correct parse to
  grade against, so verifying one would report a failure every time a mutator
  did its job. `verify check` verifies the clean records of a mixed blast run and
  prints how many it skipped and why.

- [x] [A] `ledger` in `pipeline/ledger.py`. Exactly-once accounting over N tagged
  messages: missing, duplicated, extra, wrong, each a different bug with a
  different fix and reported separately. `strays_searched` is null rather than
  false when the adapter cannot enumerate, and an unsearched stray hunt does not
  count as balanced, so a lookup-only adapter cannot pass a CI gate by accident.

- [x] [A][M] Tests. `tests/integration/pipeline_under_test.py` is a configurable
  intake pipeline, correct by default and breakable one defect at a time, and
  `tests/integration/test_pipeline_verification.py` drives the real transport
  through it. Every test starts from a correct pipeline and breaks exactly one
  thing, because a test that started from a broken one and looked for any
  non-zero exit would pass against a tool that returned 3 at random.
- [x] [A][M] Lane hygiene. `tests/unit/test_cli_pipeline.py` asserts structurally
  that no module in `testinghq/pipeline/` defines its own `require_synthetic_content`,
  `require_configured_target` or `evaluate_send`, and that the exit codes are
  defined in one place. Structural rather than behavioural on purpose: comparing
  two implementations would pass happily while they drifted, which is the failure
  this repository has already paid for once.

### Defects this milestone's own tests found

Recorded because each one was in the new code, each was found by running the
thing rather than by reading it, and none would have been caught by the tools
working correctly.

1. **`blast/generate.py` never attaches anything.** The attachment check was
   named as one of the six things verify would check, and in the tool whose whole
   purpose is to run it, it could never have run: the clean corpus is always
   attachment-free. `pipeline/verify.py` now builds its own corpus with a
   deterministic subset carrying seeded attachments, seeded per (seed, index)
   rather than from one stream so a payload does not depend on how many were
   generated before it. Without this the sixth check was decorative.
2. **The redelivery scenarios contaminated each other through Message-ID.** They
   share the generated corpus, a generated payload carries a Message-ID, and a
   correct pipeline deduplicates on Message-ID. So the slow-retry scenario
   delivered messages the duplicate scenario had already delivered, the pipeline
   correctly recognised them as redeliveries, and the scenario reported that the
   system held no record of any of its own messages. Nothing was wrong with any
   component. Each scenario now carries a Message-ID prefix as well as a tag
   prefix, so the payload bytes stay shared (one parse bug should show up four
   ways, not four ways plus three unrelated findings) while identity is scoped.
3. **`messages.stamp` changed the subject but not the ground truth.** A reply
   stamped with a `Re: ` prefix kept the parent's `ground_truth.subject`, so the
   subject check compared a reply's real subject against its parent's and both
   threaded scenarios failed on a mismatch the test had manufactured. The ground
   truth's job is to describe what went on the wire; a stamped payload whose
   subject and ground truth disagree have every check grading against something
   nobody sent.
4. **`--readback module:attribute` threw the config file's `[readback]` table
   away.** The flag says HOW to read the system and the file says WHAT its url,
   field names and timeout are, and a factory handed only a spec has nothing to
   connect to. Found by `examples/pipeline_demo.py`, which is the only thing in
   the tree that runs the CLI the way a user would rather than through an
   injected client.
5. **The http adapter could not enumerate, so every ledger run over an API
   reported strays as not searched.** It had no `list_all` at all, and a ledger
   that can never search for tickets it never sent can never report "extra".
   Most ticket APIs already answer their lookup url with everything when the tag
   parameter is absent, so it does that by default, with `list_path` for the
   ones that need a separate listing endpoint and `enumerate = false` for the
   ones that genuinely cannot. The strict `balanced` rule stayed: a lookup-only
   adapter still does not count as balanced.

`examples/pipeline_demo.py` was written for this and is worth keeping. Every
test in the suite is hermetic, and the injectable-client seam that buys that
also means nothing in the suite proves the real transport, the real serializer
and the real readback client can talk to a real server. It found three of the
five above and the first run failed four different ways before it demonstrated
anything at all, which is a fair measure of how untested a socket path is.

### Known limitations

- The readback is one poll per tag, taken once after every send has been sent.
  A pipeline that takes minutes to process a message needs `--settle` large
  enough, and there is no retry-on-empty. A tool that polled until a timeout
  would be right for an async pipeline and wrong for a synchronous one, and
  there is no way to tell those apart from the outside.
- There is no database adapter, only a URL or an import path. The reason is
  dependencies, not principle: a driver is a heavy thing to add to a package
  whose whole design goal is that it has none.
- The redelivery scenarios send the same payload bytes, and differ only in
  delivery semantics, so a corpus whose *size* is sensitive to a parse bug shows
  that bug four times rather than once. Deliberate: one bug reported four ways
  is one bug with corroboration, and four unrelated findings would not be.
- `verify check` needs the run's corpus to still be reproducible from its seed.
  A change to the generator's shape makes an old artifact uncheckable, and the
  tool refuses with that explanation rather than looking records up against tags
  that never existed.
