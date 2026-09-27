# Decision 0001: Barrage paces on the schedule, not on the bucket

Date: 2026-09-27. Status: accepted, no code change.

## The question

`TokenBucket.acquire()` used to spin forever under an injected clock at any
rate whose reciprocal is not exactly representable in binary. `barrage/runner.py`
worked around that by never letting the bucket block: arrivals are paced by an
absolute schedule, and the bucket is asked for a token only once the schedule
says it is already earned.

That bug is fixed. Should the workaround be undone?

## Decision: keep the schedule. The gate stays a gate.

Two reasons, and the second is the one that settles it.

**The schedule is correct, and it holds.** Measured with an injected clock, a
6 req/s target, a 3-second stage, and a `send_fn` that does not block
(fire-and-forget, which is what open loop is supposed to mean):

| | dispatches | elapsed | effective | slip vs ideal | gate blocked |
|---|---|---|---|---|---|
| schedule-paced, non-blocking sends | 18 | 2.833s | **6.35/s** | **0ms** | **0.000000s** |

The schedule is exact. The gate never blocks once, because arrivals are always
at least `1/rate` apart and the bucket refills at exactly the rate it is
consumed. There is nothing for a blocking gate to add.

**The gate is not what costs the load.** Same target, but with a `send_fn` that
blocks for 300ms, which is what actually ships:

| | effective | shortfall | gate blocked |
|---|---|---|---|
| A. schedule-paced, serialised sends | 3.53/s | **41%** | 0.000000s |
| C. gate-paced, serialised sends | 3.53/s | **41%** | varies |

A and C are identical. Swapping the pacer changes nothing at all, because the
shortfall is caused by the send being serialised, not by the pacer. So the
question as posed is not load-bearing: making the bucket block would add a
second wait on top of a schedule that already covers the case, and would give
the run two mechanisms that could disagree.

The gate is still worth keeping, but for a different reason than it had. It is
an **independent implementation of the rate limit**, built from a different
mechanism than the schedule. If the schedule arithmetic is ever wrong, the gate
notices, which is a real check on this module's own pacing rather than a
formality.

## The thing this turned up

**Barrage's open-loop mode is not currently open loop, and that is a larger
problem than the one that was asked about.**

`testinghq/barrage/fire.py`'s `send_fn` calls `post()` inline and returns the
result, so a dispatch cannot happen until the previous send completes. The
`RunPlan` docstring says `concurrency` is "enforced by the caller's executor, not
by this module", but there is no executor: the caller is the CLI, and the CLI
sends serially.

That is why row A shows 41% shortfall and 2.3 seconds of slip. The configured
rate is not being offered to the target at all once the target is slower than
the arrival interval, which is exactly the condition a load test exists to
investigate. The mode that is supposed to find a target's breaking point cannot
currently see it, because the run throttles itself before the target does.

This is **not** fixed here, and it should not be, because it is a change to how
requests are dispatched rather than a pacing decision. It is the next thing
worth looking at. The fix is a real executor with bounded outstanding requests,
and it needs its own decisions: what `concurrency` means when a send outlives
its slot, what happens to the run artifact when a request is still in flight at
stage end, and whether the rate ceiling is checked on dispatch or on
outstanding count.

Until that lands, the honest description of `--mode open` is "a rate-capped
serial send", and the run report's achieved-versus-target throughput is the
number that reveals it.

## Two comments in `barrage/runner.py` that are now wrong

Left alone deliberately, per the instruction not to change code without
approval. Both cite a defect that no longer exists, which is the same stale-doc
failure this repo has been bitten by repeatedly.

**`_GATE_CAPACITY`, lines 166 to 172.** The comment reads:

> Do not lower this to 1.0: that puts the bucket exactly on the knife-edge
> where float rounding makes it spin (see _pace_and_gate).

The spin is fixed and the 1.0 boundary is safe. The reason to keep capacity at
2.0 is now ordinary jitter tolerance. Proposed replacement: keep the value,
rewrite the reason.

**`_pace_and_gate`, lines 236 to 252.** A paragraph of history explaining that
the bucket "could not be trusted to block at all" and that Barrage "therefore
arrives here with the token already earned by construction, because the
alternative was a hang". Accurate as history, wrong as a description of the
present. Proposed replacement: state that the schedule is the pacer because open
loop must hold its arrival rate independently of service time, and that the
gate is an independent check on that schedule.

Say the word and both are a two-line change.

## How to revisit this

The decision is only correct while open loop actually offers the configured
rate. Row B above is the condition, and a run reporting achieved throughput
materially below its target against a responsive target means the decision's
premise has stopped holding, whether because sends are serialised or for some
other reason. That is the signal to reopen this.
