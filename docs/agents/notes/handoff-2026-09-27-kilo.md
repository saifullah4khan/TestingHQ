# TestingHQ handoff, 2026-09-27

Author: Kilo, working for Saifullah.
State at handoff: `main` = `84b8c10`, 599 tests passing, all three CI checks green.

---

## 1. Where things stand

| Tool | Purpose | Status |
|---|---|---|
| `blast` | Parser correctness under messy input | Shipped. Generate, fire, replay. |
| `barrage` | Throughput and latency under load | Shipped. Both firing modes, run artifact, replay. |
| `compare` | Did that change help? | **Shipped this session.** |
| `web/` UI | Same engine, dry-run default | **Now runs the real engine.** |

`599 passed in 12.7s`. Required status check on `main` is `tests` (the job in
`ci.yml`); `security-tests` and `dependency-audit` also run. Direct pushes to
`main` are blocked by branch protection, which is correct and should stay.

---

## 2. What was fixed, and why each mattered

### 2.1 `TokenBucket.acquire()` could spin forever (#20, merged before this session's work)

**The bug.** `acquire()` recomputed its wait on every pass and re-accumulated it.
At any rate whose reciprocal is not exactly representable in binary, the refill
rounded to just under the deficit, so the next wait was about `1e-17`. Adding
`1e-17` to a clock reading near `0.67` is a no-op at float precision, so elapsed
became zero, no refill happened, and the loop never exited.

Invisible in production, because a real monotonic clock keeps ticking regardless
and self-heals. Fatal under the injected clocks this repo mandates.

**Measured, not assumed.** One blocking process per rate against the pre-fix
module:

| | rates |
|---|---|
| hang | 3, 5, 6, 7, 9, 10, 11, 12, 13, 25 |
| terminate | 1, 2, 4, 8, 16 (powers of two), plus 0.5 and 0.1 |

The handoff named three rates. There were ten, and the dividing line is exactly
representability of the reciprocal.

**Why it survived.** The existing tests drove the blocking `acquire()` at rates
1 and 2, both exact. The one test using a non-representable rate used the
non-blocking `try_acquire()`, which cannot spin. The tests were green for a
reason unrelated to whether the module worked.

**The fix.** Compute the wait once as an absolute deadline, so no error
compounds, and floor it at one nanosecond so the loop has a guaranteed exit. It
still waits the full earned time, asserted over 100 calls at every swept rate,
so the floor cannot quietly grow into a licence to pace faster than configured.

**Proven red first:** reverting only the module and rerunning gave 22 failures
in 0.18s, no hang.

### 2.2 A shipped fixture lied about its own records (#21)

`web/tests/fixtures/sample_run_with_failures.json` declared
`by_status_class.5xx: 1`. Two of its six records carried a 500.

The web lane had a test that recomputed the fixture's *flags* and passed,
because the flags really did agree. It never recomputed the *counts*. The engine
lane had a test that noticed the defect, documented it at length, and then
asserted the corrected number against a hardcoded literal rather than against the
file, specifically so the suite would stay green. The comment said it was doing
that in order not to encode the bug. What it did was make the bug invisible: the
artifact the web UI demos from stayed wrong while 512 green tests said it was fine.

Fixed in three places: the fixture, the artifact test now compares against the
file, and both fixtures are now recounted by hand in
`tests/web/test_fixtures_schema.py` so the file and its records cannot disagree
again. The recount is hand-written rather than a call to `compute_summary()`,
because that function is one of the two implementations under suspicion.

**Proven red first:** reverting only the fixture gives 3 failures from three
independent angles.

### 2.3 The web UI was not running the real engine (#22)

`web/adapter.py` still called `web/generator.py`, a deterministic stand-in written
when the engine modules did not exist. Its own docstring claimed
`blast/generate.py` and `core/transport.py` "do not exist on this branch". Both
had been on `main` since #10.

Swapped to `blast.generate` + `blast.corrupt` + `core.transport` + `core.report`.
The seam held: the change is confined to `web/adapter.py` plus a client
parameter, and nothing else in `web/` learned anything new.

**Two things the swap exposed, which mattered more than the swap.**

*The test suite was opening real sockets.* `web/targets.json`'s first entry is a
localhost URL. Before the swap no fire path opened a socket, so that never
mattered. Now it does, and a test that forgets to inject a client makes a
genuine request and waits out the 10-second timeout. A duplicate test definition
I left behind mid-edit did exactly that: **+12.2s on the web suite, still green.**
Found only because the suite got slow and I asked why instead of shrugging.

Now: `web/server.py` holds its client in a module-level `HTTP_CLIENT` seam, the
server tests pin that with an *autouse* fixture (opting in per test is how one of
them ends up hitting a port that may or may not be listening), and
`tests/web/test_adapter.py` replaces the default transport client with a stub
that **raises**, so a forgotten injection fails in milliseconds with a message
saying what to do. Verified firing, not assumed.

*A dry run's summary had to stop being computed normally.* A dry run has no
responses, so every record's status is `None` and the normal rules report every
payload as a timeout and flag every degenerate one as a failure. Both true, both
nonsense, because the endpoint was never asked. Built explicitly, with a test
saying why, so the next person does not simplify it back.

### 2.4 The expectation rules existed three times, not two (#23)

The backlog called this one duplicated module. It was two. `web/static/app.js`
carried its own `classifyRecord()` re-deriving each record's outcome from its
status code, so the browser was a third copy of `core/report.py` that no test in
this repo could reach, because no CI here runs JavaScript.

This is the guardrails incident again, one module over. The web lane copied the
canonical rules, the canonical rules were hardened, and the copy did not inherit
it. The guardrail version meant a target the CLI refused, the UI would have fired
at. This version was display-only, so it could not fire anything, but it could
have shown a failing payload as passing.

The server now annotates each record with `outcome`, computed by
`report.classify_record`, and the browser renders it. Response-layer only: the
on-disk artifact schema and the fixtures are untouched, which matters because
Barrage replays those artifacts and a display field does not belong in them.

Two structural guards in `tests/test_lane_hygiene.py`, both proven red by
re-inlining what they forbid: a rule body in `web/expectations.py`, and a
`classifyRecord` or `is2xx`/`is5xx`/`isTimeout` helper in `web/static/app.js`.

`test_report.py`'s cross-check between the two implementations was **replaced,
not kept.** Comparing two implementations passes happily while they drift, which
is exactly the failure it existed to catch. It is now an identity check plus a
data-level check that the fixtures classify to the flags they claim.

### 2.5 Seven documents asserted that code did not exist (#24)

Every one verified against the code before being rewritten, not assumed.

| Claim | Reality |
|---|---|
| `examples/README.md`: TOML loader "not wired up yet" | works; I used it |
| `examples/README.md`: `demo.py` "not yet wired" | runs, uses `blast.generate` |
| `web/README.md`: `blast/generate.py` "does not exist on this branch" | on main since #10 |
| `README.md`: "M0 skeleton ... generator, transport, reporting, and web UI land across the milestones below" | all shipped; the file contradicted itself 4 lines later |
| `site/index.html`: `generate`/`fire`/`replay` "each still prints a 'not implemented yet' placeholder" | all three run |
| `site/index.html`: "Reporting, the run artifact ... has not been built yet" | all exist |
| `site/index.html`: "Barrage has not been built yet: no code exists for it today" | a working load generator |

The last two were on the project's public landing page.

Also fixed: `web/README.md` had a structural defect, its file list interrupted
mid-section and resumed as bullets on a list about guardrail narrowing. And
`cli.py`'s unreachable `_not_yet()` fallback printed "not implemented yet (M0
skeleton)" for a milestone that finished long ago; since every subcommand is
implemented, reaching it is a bug in that file, and the message now says so.

Deliberately **not** changed: `GOALS.md` and `BLAST_BACKLOG.md` use "not yet" in
*rules* and in *dated planner logs*; `PROTOCOL.md` is an instruction to create a
directory; `coder-a-NOTE.md` is a dated record and got a `RESOLVED` banner
instead of a rewrite.

### 2.6 The two integration gaps, and a missing seam they exposed (#25)

**`blast replay --send` had no injectable HTTP client.** `_run_fire` takes one,
and its docstring says it exists so tests can stay hermetic, but `_cmd_replay`
never passed one. The replay send path always opened real sockets, so there was
no way to test anything about it. The first run of the new test **spent 81
seconds making 40 real connection attempts** before failing.

Fixed by threading `client` through `_cmd_replay`. The gap went unnoticed because
no test existed and no test could exist, which was the second time in this batch
that a missing seam and a missing test turned out to be the same defect.

New integration coverage:

- `test_corpus_generation.py` drives a seeded corpus through real serialization
  and a real decode: every mutator reachable from a recipe and vice versa, each
  recipe buildable in isolation, `clean` leaves payloads byte-identical, every
  non-clean recipe actually changes something, all five categories reachable, and
  the observed distribution tracks `DEFAULT_MIX`.
- `test_reporting_and_replay.py` drives the real CLI send and replay paths: the
  artifact is what `core/report` derives from it, a 500 is recorded as a 5xx and
  flags clean payloads, a raising client is a timeout and not an invented status,
  **replay re-fires byte-identical request bodies**, replay refuses a tampered
  hash without firing, and a dry-run replay makes zero network calls.

**What the corpus test found about the existing happy-path test.** It asserted
`decoded.from_addr == ground_truth.from_addr` and
`decoded.text == ground_truth.body_core`. Both are true for its hand-built
payloads and neither was true against real generated data: the generator puts the
RFC 5322 display-name form in the header and the bare addr-spec in ground truth,
and puts a full message in `text` but only the substantive sentence in
`body_core`. Those equalities had never been exercised against real data. The
new tests assert containment, which is the correct relationship, and pin the
distinction so it cannot be collapsed.

### 2.7 `compare`, a regression differ (#29, new this session)

Blast answers whether a parser is correct. Barrage answers whether a pipeline
holds under load. Neither can answer the question you have after you change
something: **did that change help?** That was being answered by running the same
seed twice and eyeballing two summaries.

```
testinghq compare --baseline before.json --candidate after.json
```

Three decisions worth arguing with:

- **Outcomes, not statuses.** A structurally malformed payload returning 422
  where it returned 400 has not regressed; both are a clean rejection, a PASS for
  that category. A status differ would flag it, and once you are trained to ignore
  the output you will miss the real ones.
- **Align by id, not position.** Under positional alignment three of four
  records in the test fixture appear to move when one genuinely did. There is a
  test for this with a control that fails if the two ever stop diverging.
- **A failure that changes kind is neither fix nor regression.** A degenerate
  payload that used to 4xx and now 500s has not improved.

Two properties checked structurally rather than behaviourally: it **cannot send
anything** (no target, no transport, no `--send`, no guardrail gate because there
is nothing to gate), enforced by walking the AST of every module, with a test
that the walker rejects a module which *does* reach for a transport; and it
defines **no classification rules of its own**, guarded at source level because
this repo has made that mistake three times.

### 2.8 The rename, and why

`bellwether` was renamed to `compare`. `blast` and `barrage` are guessable from
the sound of the word; a bellwether is the lead sheep wearing a bell, which is a
real answer to the question I asked myself and a useless answer to the question a
user has. Getting nothing from a tool name defeats the purpose of naming it.
Dropped to a single level, no subcommand, because there is one operation and
`compare compare` would be a joke at the reader's expense.

### 2.9 Merge damage, found and fixed (#28)

Merging #21 through #27 **concatenated both sides of the `UI v1` conflict instead
of choosing**. `main` carried completed items and their stale unticked originals
at the same time, three of them. A reader would have found "NOT DONE and NOT
BLOCKED, this is the highest-value item available" sitting under an entry saying
it was done.

Cause: PR #23 and PR #27 carried the same work through two different branches, so
the entry was applied twice and #21's older copy was never retired.

**Why the guards missed it.** Every sentence in there was once true. The
contradiction was *between* entries rather than within one, and both stale-claim
tests pass on a file containing two contradictory claims about the same module.
That is the guard's blind spot and it is worth writing down: it catches sentences
that stopped being true, not sentences that stopped agreeing with each other.

Also repaired: a duplicate `Mutator and category-mix integration tests` entry in
both M2 and M3, and two UTF-8 BOMs introduced by a shell I used instead of an
editor. The BOM inside `compare` made a guard's `ast.parse` raise `SyntaxError`
instead of reporting, which is why `compare` now has a test asserting none of its
modules carry one.

---

## 3. Things I got wrong, kept for the record

The repo values this, so it is written down rather than tidied away.

1. **Committed item 2 onto item 1's branch.** Split back out and rebased.
2. **A duplicate test definition** left behind during the web swap made the suite
   open real sockets for 12 seconds and stay green.
3. **`--dry-run` is not a flag on `blast replay`.** Dry-run is the absence of
   `--send`; passing it is an argparse error.
4. **Let replay use `--rate`'s default of 5/s**, which the rate bucket enforces
   with a real `time.sleep`. Seven of those 7.01s were genuine pacing.
5. **Asserted category coverage from random draws.** `degenerate` carries a 5%
   weight, so "did any appear" is a coin flip. Every category-specific test is now
   driven from a single-category corpus.
6. **`_delta` in `compare` had duplicate keys**, so every status movement silently
   reported as zero. Caught by the totals test.
7. **The `compare` transport guard was a substring check** and failed on my own
   docstrings. Rewritten to walk the AST and record alias names, since
   `from ..core import transport` names `core` in `node.module` and `transport`
   only in the alias list.
8. **Two of my own rate-ceiling probes were wrong.** The first inferred "refused"
   from a non-zero exit code with a dead sink. The second passed
   `--duration 5` against a default warmup of 5, so every run was refused for an
   unrelated reason before the ceiling was checked at all. The ceiling is
   **correct**: 50 req/s and 300s allowed, 50.5 and 301 refused, and
   `--allow-high-rate` lifts it.

---

## 4. Open items, in priority order

Nothing is blocked. The backlog has zero open entries; these are the things worth
picking up that the backlog does not track.

1. **Enable GitHub Pages for `site/`.** Still not enabled, so the marketing page
   is not published. Its copy is now accurate as of #24, so it is finally worth
   turning on.
2. **Get the security-lane review for #23.** It is the only change in the batch
   that crosses the boundary `FLEET.md` calls non-negotiable. I read the backlog
   as sanctioning it; the lane map is yours to confirm.
3. **Add a suite-wide "no test opens a socket" check.** The whole batch was full
   of tests that quietly went to the network. A session-scoped autouse fixture
   patching `socket.socket` to raise, with an explicit opt-out, would stop that
   class permanently instead of per test.
4. **Close the stale-claim guard's blind spot.** It cannot detect two entries that
   contradict each other, which is exactly how the #21 to #27 merge went wrong.
   Something that checks one module is not claimed both done and not done would
   have caught it.
5. **Re-examine Barrage's pacing now that `TokenBucket` is fixed.** #20 left the
   schedule-pacing workaround in place deliberately and flagged the decision as
   still open. The bug that motivated it is gone; the design reason is
   independent. Worth a deliberate call rather than drift.

---

## 5. Invariants worth protecting

These are the things that were true at handoff and are worth a test if anyone is
tempted to move them.

- **One definition of the expectation rules.** `testinghq/core/report.py` only.
  `web/expectations.py` re-exports, `web/static/app.js` reads `record.outcome`
  from the server, `compare` delegates. Three structural guards enforce it.
- **One definition of the guardrails.** `testinghq/core/guardrails.py` only.
  Enforced for the web lane; the web UI adds two strictly-narrowing rules on top
  (boolean-`True` confirm, name-to-URL resolution) and owns no rules.
- **A dry run makes zero network calls**, on the fire path, the replay path, and
  in the UI. Three separate tests, all with an injected recording client.
- **`compare` cannot send anything**, and defines no rules of its own.
- **Barrage's ceiling** is 50 req/s and 300s, raisable only with an explicit
  `--allow-high-rate`.
- **The rate bucket terminates** at any rate, including the ten whose reciprocal
  is not binary-exact.
- **No em-dashes** in tracked text. House rule, enforced.
