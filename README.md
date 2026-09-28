# TestingHQ

Self-testing tools for intake pipelines.

TestingHQ is a small suite of tools for stress-testing the systems that ingest
messy real-world input. The first tool, Blast, generates a spectrum of
realistic-to-garbled inbound-email payloads and fires them at an intake endpoint
you control, so you can see how your parser behaves under real-world nonsense
instead of hand-typed happy-path samples.

## Tools

**Blast (v1, in progress).** Generates inbound-email payloads across a spectrum
of messiness, from clean and well-formed through typo-ridden, multilingual, and
half-gibberish, up to structurally malformed and degenerate. It POSTs them as
SendGrid Inbound Parse-shaped payloads to a configured endpoint and reports where
the parser choked. Blast is about variety and correctness: finding the inputs
that break extraction, reproducibly.

**Barrage (v1).** Volume and throughput. Where Blast proves your parser is correct
under messy input, Barrage proves your pipeline holds under load. It is a load
generator: it fires clean, provider-shaped payloads at an endpoint you control, at
a high but controlled rate, and reports throughput achieved versus targeted,
latency percentiles (p50/p90/p99), error rate over time, and the knee where your
endpoint starts shedding or slowing. Barrage is a load tester against your own
infrastructure. It is not an email sender, not a flooding tool, and not for
endpoints you do not own.


The suite ships as one installable package, `testinghq`, with subcommands
(`testinghq blast ...`, `testinghq barrage ...`, `testinghq verify ...`,
`testinghq ledger ...`, `testinghq redeliver ...`). They share a common core: the firing transport, target configuration,
guardrails, and rate limiting.
A readback seam is built on that core, described below, and `verify` and
`ledger` and `redeliver` are the tools on it.

**Verify.** What the pipeline actually produced. Every tool above judges a run by
the HTTP status, and a 200 only means the endpoint accepted the POST. It does
not mean a ticket was created, that the sender survived parsing, that the body
arrived whole, that the attachments came through, or that the message went to the
right place. Verify gives the seam an adapter that reads your system's own
output and checks each payload against ground truth Blast already generates:
was a ticket created, is the sender right, is the subject right, is the body
intact, are the attachments there, was it routed correctly. That turns "did it
crash" into "did it parse correctly", which is the question anyone actually has.

**Ledger.** Exactly-once accounting. Sends N messages, each carrying a tag no
other run could produce, then counts what the pipeline made of them: missing
(an email your customer sent and you lost), extra (a ticket nobody sent), and
duplicated (two tickets for one email). Point it at a system under Barrage load,
or with a dependency failing, and it answers the question every intake owner
asks and nothing else answers: did we lose anything?

**Redeliver.** Delivery semantics. Webhook providers retry, deliver the same
message twice, and deliver out of order. Redeliver does all three: the same
Message-ID sent twice, re-sent after a delay the way a provider's retry would
be, and a reply delivered before the message it answers. It then checks for no
duplicate tickets and for correct threading, separately, so a report that says
"the headers were right but the reply went to its own ticket" says something
different from one that says the headers were wrong.

### The readback seam

Blast and Barrage both judge a run by the HTTP status, and a 200 only means the
endpoint accepted the POST. It does not mean a ticket was created, that the
sender survived parsing, that the body arrived whole, or that the message went
anywhere useful. The seam is how a tool asks your system what it made of a
message. It is small: an adapter that can read your system's output.

```
# a JSON API: GET the url with the tag as a query parameter, read the records
[readback]
kind = "http"
url = "http://localhost:8000/tickets"

# an outbound mail sink: a JSON Lines file, one delivered message per line
[readback]
kind = "mailbox"
path = "./mail-sink.jsonl"
```

Anything else, including a database, is one import path: `--readback
mypkg.myadapters:build`.

The target may be a `ReadbackAdapter`, a factory taking the `[readback]` config
table, or a plain function taking a `Probe`. A one-line lambda is a complete
adapter, which is what makes the seam cheap enough that nobody skips checking
and goes back to trusting the status code.

**A check that could not run says so, and says why.** Three distinct causes, kept
apart because they mean opposite things: `no record found` (the finding itself),
`adapter cannot see field` (a gap in your integration), and `nothing to check`
(most often a payload with no attachments). A run that verified nothing says so
too, and the reports print what they skipped as prominently as what they found.

**The readback polls until it settles.** After the last send it keeps asking, and
believes the answer when every payload has been found AND the per-tag record
counts have held steady for `--quiet-window` seconds. Waiting on the counts
rather than on "did it create anything" is what catches a duplicate that lands
after the first sighting. It gives up at `--max-wait` and says GAVE UP rather
than claiming a quiet it did not hear, and the report always states how long it
took.

**Credentials come from the environment.** `[readback.headers]` takes
`Authorization = "env:HQ_READBACK_TOKEN"` and nothing else. A literal value is
refused rather than warned about, because a config file is a file that gets
shared, and a missing variable is refused before the run sends anything. A run
artifact records header names and never values.

Readback URLs go through the same guardrail as firing targets, and a public host
is refused unless you pass `--allow-public-readback`. On a real deployment the
readback is a ticket store or a mail sink that may hold other people's data.

## Status

All five tools work end to end. The suite is 1080 tests, green on every push, and
CI runs on every change.

**Blast** ships: seeded deterministic generation, six mutators behind five
messiness recipes, a 20-case named edge-case catalog, seeded attachments,
SendGrid Inbound Parse-shaped serialization, the HTTP transport, target
configuration from a TOML file, rate limiting, the safety guardrails, the run
artifact, expectation-based reporting with pass/fail assertions, and
byte-identical replay from a saved run.

**Barrage** ships: closed-loop and open-loop firing, a rate-controlled schedule
with a warmup ramp, throughput and latency percentile reporting, a run artifact,
and replay from a saved run. **It has no executor yet**, so requests are
dispatched serially in both modes, one in flight at a time, and `--concurrency`
is refused in open mode because it cannot mean anything there. Against a target
slower than the arrival interval, achieved throughput is capped by the target's
response time rather than by the rate you asked for. Tracked in
[issue #38](https://github.com/saifullah4khan/TestingHQ/issues/38).


**The web UI** ships: a dependency-free single-page app that runs the same
engine, with dry-run as the default action and a configured-target allow-list
plus an explicit confirm step before anything is sent.

**Redeliver** ships: four scenarios, a per-scenario tag and Message-ID scope so
one scenario's deduplication cannot absorb another's deliveries, and threading
checked as two things rather than one.

**Ledger** ships: per-message tagging, an accounting that keeps missing,
duplicated, misparsed and extra apart, and a verdict plus an exit code that is
the answer. A lookup-only adapter reports `extra: null` and `UNVERIFIED STRAYS`
rather than reporting none found for a question it never asked, so it cannot
pass a CI gate by accident.

**Verify** ships: the six per-field checks plus the two threading checks,
per-message tagging so a run is reproducible and two concurrent runs cannot read
each other's records, a readback phase that runs after every send, and an
artifact that is a blast artifact with an extra block on each record, so
`testinghq compare` reads it like any other run.

Milestone history and what is still open are tracked in
`docs/agents/BLAST_BACKLOG.md`.

## Install

```
pip install -e ".[dev]"
```

## Quick start

```
# generate a corpus to disk, no network
testinghq blast generate --count 100 --seed 1 --out corpus

# dry run by default: shows what it would send, makes no network calls
testinghq blast fire --target local

# actually fire at a configured target (explicit, rate limited, and paced)
testinghq blast fire --target local --send --rate 5 --out run.json

# --config points at your target TOML (see examples/target.example.toml);
# defaults to ./target.toml
testinghq blast fire --target local --send --config target.toml

# re-fire the exact same corpus from a saved run, byte-identically
testinghq blast replay run.json --send
```

Barrage, for load rather than variety:

```
# dry run by default: previews the load plan, makes no network calls
testinghq barrage fire --target local --rate 20 --duration 60

# actually run the load test against a configured target
testinghq barrage fire --target local --rate 20 --duration 60 --send

# closed-loop is the other mode. It is also serial today, so --concurrency is
# refused in both modes until an executor lands (issue #38)
testinghq barrage fire --target local --mode closed --send

# write the run artifact, then re-run it later from its seed and config
testinghq barrage fire --target local --send --out load.json
testinghq barrage replay load.json --send
```


The slow-retry scenario is the duplicate half of a provider retry, not a
failure-driven one: it re-sends a message whose first delivery SUCCEEDED, which
is what a correct pipeline should deduplicate by Message-ID. It cannot tell you
how a pipeline behaves when the failure that caused a real retry is also
present, and a pipeline that deduplicates only on a failure signal rather than on
Message-ID will pass this and still duplicate a real retry.


Verify, for what the pipeline made of the payloads rather than what it answered:

```
# dry run by default: previews the corpus and the adapter, makes no calls
testinghq verify fire --target local

# fire the clean corpus, then read your system's output back and check it
testinghq verify fire --target local --send

# say where your system should have routed each message, if it has its own
# routing taxonomy; without it, the expected route is the message's recipient
testinghq verify fire --target local --send --expect-route queue/support

# for a pipeline that creates the ticket on a queue consumer rather than inside
# the request, keep asking until the record counts stop moving
testinghq verify fire --target local --send --quiet-window 10 --max-wait 120

# check a run that already happened. Sends nothing, so it has no --send.
testinghq verify check verify.json
```


Ledger, for exactly-once accounting:

```
# 50 uniquely tagged messages, then reconcile them against what the system holds
testinghq ledger fire --target local --send --count 50

# change the tag prefix when two runs hit the same system at once
testinghq ledger fire --target local --send --tag-prefix spike-2026-09
```


Redeliver, for what your pipeline does when the provider misbehaves:

```
# four scenarios: duplicate, slow-retry, reply-first, references
testinghq redeliver fire --target local --send

# just one of them, and with a longer retry gap than the default 120s
testinghq redeliver fire --target local --send --scenario slow-retry --retry-after 30
```

The slow-retry scenario is the duplicate half of a provider retry, not a
failure-driven one: it re-sends a message whose first delivery SUCCEEDED, which
is what a correct pipeline should deduplicate by Message-ID. It cannot tell you
how a pipeline behaves when the failure that caused a real retry is also
present, and a pipeline that deduplicates only on a failure signal rather than on
Message-ID will pass this and still duplicate a real retry.

Every `testinghq` command in this file is executed as a dry run by
`tests/unit/test_readme_examples.py`, so an example cannot rot into a command
that exits non-zero without the suite noticing. The three pipeline commands are
run with a mail-sink adapter the harness supplies, because they refuse to run
without one by design.

## Exit codes

Every subcommand returns a process exit code, and the code is the answer a
script reads. There is one convention, across every tool:

| Code | Meaning | A script should |
| --- | --- | --- |
| 0 | ran, answer was yes | carry on |
| 1 | refused: guardrail, bad config, bad usage, unreadable file | fix the invocation; nothing was sent |
| 2 | ran, sent nothing (no `--send`) | carry on; this was a dry run |
| 3 | ran, answer was no | look at the report |

The three non-zero codes call for three different responses, which is why they
are three different numbers. A refusal means nothing ran and the command has to
change. A dry run is a successful run that was asked to hold back. A finding is
a successful run that found something.

`compare` used to report a regression as 1 and a usage error as 2, so a script
reading 1 as "refused" read a regression as the tool declining to run. It now
follows the table: a regression is 3, a usage error is 1. **This is a breaking
change for any script that calls `compare` and checks its exit code**, and it is
the reason this table exists.

The numbers are defined once, in `testinghq/core/exit_codes.py`, and
`tests/unit/test_exit_codes.py` fails if any module defines its own or returns a
code outside the set.

## Reading a run artifact

Six tools write a run artifact and each grew its own shape, so a CI system that
wanted to know what happened had to know which tool produced the file first.
`testinghq report` is the one place that knows:

```bash
# a readable summary
testinghq report run.json

# one stable shape, for a machine
testinghq report run.json --json
```

It detects the tool, derives the verdict that tool would have printed, and
reports counts and findings. With `--json` the output has the same keys whatever
wrote the file, so a script parses one thing: `tool`, `verdict`,
`verdict_source`, `exit_code_derived`, `counts`, `findings`, `seed`, `target`
and `dry_run`.

It sends nothing, needs no config file, and has no `--send` or `--target`
because it cannot reach the network. An artifact it cannot identify is refused
with a message saying what the top-level keys were, rather than summarized as a
run that never happened.

Two things it reports are not in the artifact, and both are labelled. The
verdict: `verify` and `loop` compute theirs at print time and never write it
down, so `report` reimplements those derivations, pinned against the tools' own
formatters by a test. The exit code: no artifact records one, so it is
reconstructed from the same fields the tool used, and reported as
`exit_code_derived` rather than `exit_code`.

Reading a report exits 0 whenever the artifact was readable, even when the run
found something. The findings are in the output; deciding what to do about them
is the caller's job.

## Responsible use

Blast is a fuzzer and self-testing tool for endpoints you control. It POSTs
provider-shaped payloads at your own ingest. It is not an email sender: it does
not deliver mail to arbitrary inboxes, it does not try to defeat spam filters,
and it does not forge sender authentication to fool real recipients. All
generated content is synthetic and uses reserved example domains.

Dry-run is the default. Firing requires an explicit `--send` flag and a target
you have declared in configuration. Rate limiting is on by default.

The same applies to Barrage, with one addition. Barrage is a load tester against
your own infrastructure: it is not a flooding tool, and it must not be pointed at
an endpoint you do not own. Three controls are what keep it a load tester rather
than a weapon, and none of them are cosmetic:

- **Dry-run is the default.** A dry run makes zero network calls. It never
  resolves a target and never builds a request. `--send` is required to put
  anything on the wire.
- **Configured targets only.** Both the target name and the URL it resolves to are
  checked against the canonical guardrails, so a real public host cannot hide
  behind a friendly name.
- **A hard rate and duration ceiling** (50 requests/second, 300 seconds) that
  requires an explicit `--allow-high-rate` to raise. This exists so that a typo in
  `--rate` or `--duration` cannot become a self-inflicted denial of service. Pass
  it deliberately, and only against infrastructure you own.

Barrage fires clean, valid payloads only. It reuses Blast's seeded generator for
realistic bodies and deliberately never garbles them: Barrage is about volume, not
malformed input. That is Blast's job.

Verify also fires clean payloads only, for the same structural reason and a
different one: verify grades results, and a deliberately mangled payload has no
correct parse to grade against. Blast owns messy input.

All three pipeline tools are correctness tools rather than load tools, so none
of them carries `--allow-high-rate`: the thing that needs a hard ceiling is
sustained load, and that is Barrage's job with the ceiling already in place.


## License

MIT. See [LICENSE](LICENSE).

Contact: saifullah4khan@gmail.com
