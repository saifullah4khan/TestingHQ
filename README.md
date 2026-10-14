# TestingHQ

Fuzz, load-test and verify your inbound email webhook. Catches lost, duplicated,
mis-threaded and looped mail that a 200 OK hides.

TestingHQ posts provider-shaped inbound email (SendGrid, Mailgun, Postmark or raw
MIME) at a webhook you run, then asks your system what it actually made of each
message.

## What it catches

- An email your endpoint answered 200 for and never turned into a ticket.
- Two tickets for one email: a provider retry, a redelivery, a worker that ran twice.
- A ticket nobody sent, carrying a test tag.
- A mangled sender, a changed subject, a truncated body, a dropped attachment.
- A reply filed as a new conversation, including one that arrives before its original.
- An auto-reply to an out-of-office, a bounce or a mailing list: how a mail loop starts.
- A triage label that moves when a signature, a forward or a translation is added.
- A webhook that accepts unsigned, wrongly signed, stale or replayed requests.
- A parser that answers 500 to messy, multilingual or malformed mail, or 200 with an error.
- The request rate at which the endpoint starts slowing down or shedding load.

## Which tool

| Question | Tool | Answer | Exit 3 means |
| --- | --- | --- | --- |
| Does my parser survive messy mail? | `blast` | a seeded corpus fired at the webhook, every bad answer flagged | a payload was flagged |
| How much load before it slows or sheds? | `barrage` | throughput, latency percentiles and the knee | the run was interrupted |
| Did it store what was sent? | `verify` | six checks per message, read back from your system | a check failed, or the readback never settled |
| Did we lose or duplicate anything? | `ledger` | missing, duplicated, misparsed and extra, counted | anything but BALANCED |
| Are retries and out-of-order replies safe? | `redeliver` | four provider misbehaviours, one ticket and one thread each | a scenario failed |
| Will it answer an auto-reply? | `loop` | twelve kinds of machine mail and a loop bait | a ticket or a reply for machine mail |
| Is the triage label stable? | `steady` | label dissent across meaning-preserving rewrites | dissent above the gate |
| Does it check the webhook signature? | `sigcheck` | six signed and mis-signed variants | it accepted one it should refuse |
| Did my change make it worse? | `compare` | regressions, fixes and changes between two runs | a regression, with `--fail-on-regression` |

## Install

```
pip install testinghq
```

Python 3.10 or newer. Standard library only at run time, plus `tomli` on 3.10.

## Quick start

```
cat > target.toml <<'EOF'
[targets.local]
url = "http://127.0.0.1:8000/inbound"   # your webhook
[targets.mailgun]                        # sigcheck --scheme mailgun needs format = "mailgun"
url = "http://127.0.0.1:8000/inbound"
format = "mailgun"

[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"   # answers GET ?tag=<tag> with the records it made
EOF
testinghq config validate target.toml
testinghq ledger fire --target local --count 50
testinghq ledger fire --target local --count 50 --send --out ledger.json
testinghq report ledger.json --html report.html
```

Every command is a dry run until you pass `--send`. Records are read with the
field names in [docs/CONFIG.md](docs/CONFIG.md) unless `[readback.fields]` maps
them. A target's `format` picks the wire format: `sendgrid` (default),
`sendgrid-raw`, `mailgun`, `postmark` or `mime`; see [docs/FORMATS.md](docs/FORMATS.md).

## What a finding looks like

`ledger` against a pipeline whose worker writes every fifth ticket twice. Every
POST in the run was answered 200. From `python examples/pipeline_demo.py`:

```
ledger fire: explicit --send flag set
ledger: UNACCOUNTED  (12 message(s) sent, seed=0, tag prefix 'hq-2610142218-d0f4')

  sent:            12
  produced:        14
  exactly once:    10
  missing:         0
  duplicated:      2
  misparsed:       0
  readback:        2.59s over 4 poll(s), settled
  extra:           0

  missing: none

  duplicated (2):
    hq-2610142218-d0f4-0-0004: 2 tickets ['T00005', 'T00006']
    hq-2610142218-d0f4-0-0009: 2 tickets ['T00011', 'T00012']

  misparsed: none
```

## Exit codes

| Code | Meaning | A script should |
| --- | --- | --- |
| 0 | ran, the answer was yes | carry on |
| 1 | refused: guardrail, bad config or bad usage; nothing was sent | fix the command |
| 2 | dry run: no `--send`, nothing was sent | carry on |
| 3 | ran, the answer was no | read the report |

Every tool uses the same four codes. `report` exits 0 whenever it could read the artifact.

## The tools

**Blast.** Fires a seeded corpus (clean, messy, multilingual, malformed, degenerate) with one to three clean attachments on about a third of payloads (`--attachments none|seeded|adversarial`).
`--mix clean=20,messy=40,...` sets the category percentages; `--catalog` fires the named edge cases instead, all of them or a comma-separated list.
A 5xx or no response is flagged in every category, and a missing response says why (refused, DNS, timeout, TLS, other). `--fail-on-body REGEX` flags a 2xx whose body reports failure.
The same seed and options rebuild a byte-identical corpus, and `blast replay` re-fires it.

```
testinghq blast generate --count 100 --seed 1 --out corpus
testinghq blast fire --target local --send --catalog --out blast.json
testinghq blast replay blast.json --send
```

**Barrage.** Clean payloads at a controlled rate after a warmup ramp. `--mode open` (default) keeps arrivals on schedule whatever the target does; `--mode closed` runs `--concurrency` workers that each send the next request when the last completes.
It reports achieved against target throughput, p50/p90/p99 latency, errors over time with 429 counted as shed load, and the knee where the endpoint gives way.
The run ends `--grace` seconds (default 10) after `--duration`, and a request that waited longer than `--max-queue-seconds` (default 5) for a worker is dropped and counted. Each worker keeps one connection alive unless `--no-keepalive`.
Ctrl-C writes a partial artifact with `aborted: true` and exits 3. Rate, duration and concurrency are capped at 50/s, 300s and 64 unless `--allow-high-rate`.

```
testinghq barrage fire --target local --rate 20 --duration 60 --send --out load.json
testinghq barrage fire --target local --mode closed --concurrency 8 --send
```

**Verify.** Fires clean tagged messages, reads back what the system stored, and checks that a record exists and that its sender, subject, body, attachments and route match.
Routing is checked only when you pass `--expect-route`; without it the check is skipped. `--subject-match contains` (the default) accepts helpdesk prefixes like `[Ticket #123]`, and `--attachment-match loose` tolerates sanitised filenames.
A check that could not run says why (no record found, adapter cannot see field, nothing to check) and never counts as a pass.
`verify check` re-reads an earlier run without sending anything.

```
testinghq verify fire --target local --send --expect-route support --out verify.json
testinghq verify check verify.json
```

**Ledger.** Sends N uniquely tagged messages and reconciles them: missing, duplicated, misparsed, and extra (a record with this run's tag family that this run never sent).
Each run draws its own tag prefix, `hq-<yymmddHHMM>-<4 hex>`, so runs never read each other's records; `--tag-prefix` fixes one.
BALANCED exits 0. A readback that cannot list every record reports UNVERIFIED STRAYS, and one still changing at `--max-wait` reports UNSETTLED; both exit 3.
Run it while Barrage holds the system under load to see what load loses.

```
testinghq ledger fire --target local --send --count 50
```

**Redeliver.** Four scenarios: the same Message-ID delivered twice; every message re-sent after one `--retry-after` wait (default 120s); two replies delivered before their original; a three-message thread whose last reply must carry the full References chain.
Duplicate tickets and threading are checked separately. `--scenario` picks one, and can be repeated.

```
testinghq redeliver fire --target local --send --scenario slow-retry --retry-after 30
```

**Loop.** Sends machine mail a pipeline must not act on (Auto-Submitted, Precedence, out-of-office, a delivery-status bounce, noreply senders, list mail) and a loop bait: an ordinary message, then an autoresponder answering the pipeline's own acknowledgement of it.
A ticket for machine mail is a finding unless `--ticket-policy allowed`; a reply to machine mail, or to the bait, is always a finding.
Replies are read from `[loop.outbound]` in the config. Without it the reply check is NOT CHECKED, never passed, and outbound mail it cannot tie to a message is itself a finding.

```
testinghq loop fire --target local --send --ticket-policy allowed
```

**Steady.** Sends reviewed intents in English, Urdu and Roman Urdu, each with meaning-preserving edits: a signature, a forward wrapper, quoted history, reflowed whitespace, a reworded greeting.
It reads back the label (`--label-field category|route|priority`) and reports the share of variants that dissent from their family's majority, by transform and by language. Above `--max-dissent-rate` (default 0.1) it exits 3.
`--repeats N` measures answers to identical input separately. It measures stability, not correctness.

```
testinghq steady fire --target local --send --label-field category --repeats 3
```

**Sigcheck.** Sends one request per variant (valid, wrong-secret, unsigned, stale-timestamp, tampered-body, replayed-token) and reports which the endpoint accepted.
Schemes: `mailgun` (HMAC of timestamp and token, as form fields); `sendgrid` (ECDSA over timestamp and raw body, on the curve SendGrid documents: give the private key matching the endpoint's public key, and the dry run prints that public key); `postmark-basic` (Postmark does not sign; its protection is Basic credentials).
The secret is read only from the environment variable `env:NAME` names, and the artifact records the reference, never the value. For Mailgun, accepting a stale timestamp but refusing a replayed token follows Mailgun's own guidance and is informational.
Exit 3: the endpoint accepted what it should refuse, refused a valid request, or answered 5xx.

```
testinghq sigcheck fire --target mailgun --scheme mailgun --secret env:MAILGUN_SIGNING_KEY --send
```

**Compare.** Two Blast runs are lined up payload by payload, with regressions, fixes and changes reported apart. Two Barrage runs are compared on load: error and unsent rate, hold-stage p99, and achieved rate when mode and rate match.
A dry run on either side, or two runs with nothing in common, is refused.

```
testinghq compare --baseline before.json --candidate after.json --fail-on-regression
```

**Reports.** `testinghq report run.json` summarises any tool's artifact, and `testinghq report run.json --json` prints one shape for every tool. `testinghq report run.json --html report.html` writes a self-contained HTML report. `testinghq config validate` shows what the loaders resolved and never prints a header value.

## Readback

The pipeline tools ask your system what it made of each message, found by a tag
in an `X-TestingHQ-Tag` header and `[testinghq:<tag>]` in the body. Three kinds:

```toml
[readback]                         # a JSON API: GET url?tag=<tag>
kind = "http"
url = "http://127.0.0.1:8000/tickets"
query = "type:ticket tags:{tag}"   # optional template, for search APIs

[readback.headers]
Authorization = "env:HQ_READBACK_TOKEN"   # env: references only; a literal is refused
```

```toml
[readback]                         # a JSON Lines file your pipeline writes
kind = "mailbox"
path = "./mail-sink.jsonl"
```

Anything else is an import path, `--readback mypkg.adapters:build`: a class, an
object with `fetch`, or a function taking one probe and returning records.

The readback settles once what it sees (which payloads are found, how many records each, the field
values the checks read) has held still for `--quiet-window` seconds (default 5); a payload still not
found then is lost. A store still changing when `--max-wait` (default 60) runs out is UNSETTLED
(exit 3), not a loss, and anything already proven, such as a duplicate, is still listed. For a slow
or async pipeline, raise `--quiet-window` above its worst processing latency. Lookups retry 429, 5xx
and timeouts with backoff, honour `Retry-After`, and are paced to `--readback-rate` (default 4 per
second). A public readback host needs `--allow-public-readback`. Recipes for Zendesk, Freshdesk,
Mailpit, Chatwoot, a generic REST API and a mail sink are in
[examples/readback/](examples/readback/).

## Responsible use

TestingHQ is for endpoints you own or are authorised to test. It never sends
email: it POSTs webhook payloads, and every address in them is on a reserved
example domain.

- Dry run is the default; every run that sends needs `--send`. Only a target declared in `[targets.<name>]` can be fired at.
- A public host needs `--allow-public-target`; Barrage at a public host also needs `public = true` on the target.
- Every send is rate limited, and Barrage has hard ceilings.

See [docs/SECURITY.md](docs/SECURITY.md) for the full model and how to report a vulnerability.

## Documentation

- [docs/README.md](docs/README.md): index of everything below
- [docs/CONFIG.md](docs/CONFIG.md): every config key, generated from the code
- [docs/FORMATS.md](docs/FORMATS.md): the five wire formats, field by field
- [docs/SECURITY.md](docs/SECURITY.md): guardrails, public targets, threat model
- [docs/DOGFOOD.md](docs/DOGFOOD.md): running against a real staging deployment
- [examples/README.md](examples/README.md): the demo scripts and sample configs
- [CHANGELOG.md](CHANGELOG.md) and [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
