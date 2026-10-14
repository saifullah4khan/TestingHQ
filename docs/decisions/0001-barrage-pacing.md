# Decision 0001: how Barrage paces load

Status: accepted. Revised for 1.0, which replaced the earlier record.

Barrage has two firing modes with their standard load-testing meanings. Both
share one worker pool, one run deadline and one report.

## Open loop (`--mode open`, the default)

Arrivals follow a schedule fixed before the run starts: each stage of the
warmup ramp and the hold contributes `round(rate * duration)` arrivals spaced
`1 / rate` apart, and each stage starts where the previous one ended. Nothing
in the schedule depends on how the target responds, and there is no drain
between stages, so a slow target cannot stretch the ramp.

`--concurrency` workers carry the arrivals. An arrival that finds every worker
busy waits in a queue. If it waits longer than `--max-queue-seconds` (default
5) it is recorded as `dropped_backlog` and never sent, so a target that falls
behind cannot build an unbounded backlog.

The schedule is the pacer. A `TokenBucket` gate, with two tokens of headroom,
checks the schedule's interval arithmetic (`1.0 / rate`) against the rate
itself. On a correctly paced run it never waits, and a test asserts exactly
zero waits. A gate that waited would throttle offered load exactly when the
target is slow, which is what an open-loop run exists to measure.

## Closed loop (`--mode closed`)

`--concurrency` workers each send, wait for the response, then take the next
token from one shared `TokenBucket` of capacity 1 whose rate follows the
current stage. The bucket's lock makes it safe to share. Offered load is
therefore `min(rate, concurrency / latency)`: a slow target slows the test.
A request's scheduled time is when its worker took the token.

Against a 200 ms target at 20/s with two workers, open loop sends every
scheduled arrival and its queue grows; closed loop sends about half as many
and nothing queues. `tests/unit/barrage/test_runner.py` asserts both
timelines.

## The run deadline

The run ends at `duration + --grace` (default 10s) after it started, whatever
the target does. A request still in flight then is `abandoned`; one still
queued is `not_dispatched`. Workers are daemon threads and sends never run on
the main thread, so nothing holds the process past `duration + grace +
--timeout`. Ctrl-C ends the run the same way, the artifact is written with
`aborted: true`, and the command exits 3.

## Connections

Each worker holds one `PersistentHttpClient` connection, opened before its
first request. The handshake is measured once per worker and reported under
`connections.setup_ms`, so service latency is time on an open connection.
`--no-keepalive` opens a connection per request, with the handshake inside
every latency.

## What the report measures

- Service latency: send to response. Offered latency: scheduled arrival to
  response, which includes queueing. Both as p50, p90 and p99, over the whole
  run and over the hold stage alone, and per one-second bucket.
- Achieved rate: `(N - 1) / span` over the real dispatch span of N sent
  requests, because N requests span N - 1 intervals.
- Errors: 5xx, 429 (counted as "shed"), no response, and abandoned requests.
  Status classes are counted per bucket and overall.
- The knee: the first of two consecutive buckets that are shedding (error rate
  at least 5%), falling behind (sent under 80% of what the plan targeted for
  the window), or slowing (median service latency above twice the baseline).
  The baseline is the hold stage's first two buckets, never a ramp request.
- `records`: one row per scheduled request (index, scheduled_at,
  dispatched_at, status, service_ms, offered_ms, outcome), the rows `compare`
  aligns two load runs on.

## How to revisit this

Against a responsive target an open-loop run reports an achieved rate equal
to its target and zero gate waits. If either stops being true, the schedule
stopped holding and this decision needs another look. A shortfall against a
slow target with queued or dropped requests is the pool, and the dispatch
note says to raise `--concurrency`.
