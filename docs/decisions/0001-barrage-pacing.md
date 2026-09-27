# Decision 0001: Barrage paces on the schedule, not on the bucket

Date: 2026-09-27. Status: accepted, 2026-09-27. No pacing behaviour changed.

## The question

`TokenBucket.acquire()` used to spin forever under an injected clock at most
real rates. `barrage/runner.py` worked around that by never letting the bucket
block: arrivals are paced by an absolute schedule, and the bucket is asked for
a token only once the schedule says it is already earned.

That bug is fixed. Should the workaround be undone?

## Decision: keep the schedule. The gate stays a gate.

### A note on the metric, because the first version of this note got it wrong

An earlier draft reported row A as 6.35 req/s against a configured 6.0 and
called it a 6 percent overshoot. That was a fencepost artifact and it read as
though the run exceeded a ceiling it never touched. N dispatches span N-1
intervals, so:

- **inter-arrival rate** = (N-1) / elapsed. Whether the pacing held.
- **offered throughput** = N / elapsed. What the target actually saw.

Only the first is a pacing metric. `(18-1)/2.833 = 6.00/s` exactly, which is the
configured rate, as it should be. Barrage's own run artifact reports the second
one, as `summary.throughput.achieved_rps`, which is correct for what it is.

### The measurement

Injected clock, 6 req/s target, 167 ms interval, 3-second stage, a `send_fn`
that does not block, which is what open loop is supposed to mean:

| | dispatches | elapsed | inter-arrival | offered | slip vs ideal | gate blocked |
|---|---|---|---|---|---|---|
| schedule-paced, non-blocking sends | 18 | 2.833s | **6.00/s** | 6.35/s | **0 ms** | **0.000000 s** |

The schedule is exact and the gate never blocks once. Arrivals are always at
least `1/rate` apart and the bucket refills at exactly the rate it is consumed,
so there is nothing for a blocking gate to add.

Now the comparison that settles it, same target, with a `send_fn` that blocks
for 300 ms:

| | inter-arrival | shortfall | gate blocked |
|---|---|---|---|
| A. schedule-paced, serialised sends | 3.33/s | 44% | 0.000000 s |
| B. gate-paced, serialised sends | 3.33/s | 44% | varies |

**A and B are identical.** Swapping the pacer changes nothing, because the
shortfall is caused by the send being serialised, not by the pacer. So the
question as posed is not load-bearing: making the bucket block would add a
second wait on top of a schedule that already covers the case, and would give
the run two mechanisms that could disagree.

### What the gate is actually good for, stated narrowly

The gate catches an error in the schedule's **interval arithmetic**. The
schedule computes an interval by dividing (`1.0 / stage.rate`); the bucket is
handed the rate itself. Different expressions, same field, so a mistake in one
is not automatically a mistake in the other.

It does **not** catch a disagreement about the rate, because both read
`stage.rate`. That narrower scope was previously stated as "an independent
implementation of the rate limit", which overclaimed: they are not independent
of the rate, only of the interval.

Three tests in `tests/unit/barrage/test_runner.py` pin this, and the second one
is the one that carries the argument:

- `test_the_gate_never_waits_when_the_schedule_holds` asserts `waited == 0.0`
  exactly, not "small", for every dispatch of a correctly paced run. A gate
  that waited a little on every call would look fine in a throughput report
  while having quietly become a second pacer.
- `test_the_gate_blocks_when_tokens_are_taken_faster_than_they_are_earned`
  drives twelve tokens through `_pace_and_gate` at 5/s with no time passing and
  requires it to block and to throttle to exactly the configured rate. It goes
  **through the gate function**, not around it. An earlier version of this test
  constructed the `TokenBucket` directly, which proved the bucket can block and
  said nothing about the gate being wired to it: replacing the body of
  `_pace_and_gate` with `return 0.0` left that version green. Verified red
  against a neutered gate with the current version.
- `test_the_gate_and_the_schedule_read_the_rate_from_different_expressions`
  asserts the shared origin, so the day someone routes the bucket's rate
  through a different source, whoever reads this learns the gate's scope grew.

## The larger finding: nothing in Barrage is concurrent

`testinghq/` does not import `threading` anywhere. There is no
`concurrent.futures`, no executor, no asyncio, in `testinghq/` or in
`core/transport.py`. A request is issued, its response read, and only then does
the next dispatch begin.

`RunPlan`'s docstring says `concurrency` is "enforced by the caller's executor,
not by this module" for open mode. The caller is the CLI, and the CLI has no
executor either.

Measured against a **real** slow target: a local HTTP server sleeping 300 ms
per response, driven by the shipped `testinghq barrage fire` binary with the
real transport, 6 req/s target, 9-second run, 3-second warmup, seed 1.

**Open mode**, 300 ms target response against a 167 ms interval:

| `--concurrency` | requests | elapsed | inter-arrival | vs target |
|---|---|---|---|---|
| 1 | 46 | 14.324s | 3.14/s | 48% short |
| 4 | 46 | 14.368s | 3.13/s | 48% short |
| 64 | 46 | 14.236s | 3.16/s | 47% short |

**Closed mode**, same target:

| `--concurrency` | requests | elapsed | inter-arrival | vs target |
|---|---|---|---|---|
| 1 | 27 | 8.297s | 3.13/s | 48% short |
| 4 | 28 | 8.425s | 3.20/s | 47% short |
| 64 | 27 | 8.196s | 3.17/s | 47% short |

Two things fall out of that, and the second is worse than the first.

**Open mode is not open loop.** It cannot hold its arrival schedule against a
target slower than the interval, which is the exact condition a load test
exists to investigate. The mode meant to find a target's breaking point cannot
see it, because the run throttles itself before the target does.

**`--concurrency` is inert in both modes.** Not just open mode, where
`_run_open_loop_stages` does not take the parameter at all, but closed mode
too. The closed-loop slot logic is correct, but with a serialised send there is
never more than one request in flight, so 63 of those 64 slots never have
anything to hold. The flag is parsed, validated, stored in the run plan, echoed
into the run artifact's config block, and printed by the dry-run preview as
`concurrency: N`, and then has no effect at all.

That is a lie in user-facing output. Until an executor exists,
`--mode open` and `--mode closed` are both "a rate-capped serial send", and the
dry-run preview says otherwise.

Not fixed by this decision. It is a change to how requests are dispatched, and
it is tracked with a design proposal in issue #38.

## Why the gate is not the pacer, restated without the removed argument

A blocking gate would throttle offered load down exactly when the target is
slow, and measuring that is the entire point of an open-loop run. The schedule
is authoritative for that reason, which has nothing to do with the acquire()
defect that originally forced the arrangement.

## How to revisit this

The decision holds while the schedule is exact, which is the condition measured
in row one above. A run reporting inter-arrival throughput materially below its
target against a **responsive** target means the schedule stopped holding, and
that is the signal to reopen this. A shortfall against a *slow* target means
something else, and as of this writing the most likely cause is the missing
executor rather than the pacing.
