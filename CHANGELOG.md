# Changelog

All notable changes to TestingHQ are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).
From 1.0.0 on, a change to a flag, an exit code, a config key or the
`report --json` shape is a breaking change and needs a major version.

## [Unreleased]

## [1.0.0] - YYYY-MM-DD

The first published release. A 0.1.0 was prepared on 2026-09-28 and never
published: no tag was pushed and nothing reached PyPI. Everything it contained
is folded in below.

### Added

**Tools**

- `blast generate|fire|replay`: seeded inbound-email payloads, from clean to
  degenerate, fired at an endpoint you control.
- `barrage fire|replay`: load at a steady or ramped rate, open loop or closed
  loop, with a knee detector.
- `verify fire|check`: check what a pipeline actually stored, not what it
  answered. A 200 is not evidence a message was parsed. `verify check` reads
  back a run that already happened and refuses a blast artifact, which has no
  tags.
- `ledger fire`: account for every payload sent against every record produced:
  missing, duplicated, misparsed and stray records.
- `redeliver fire`: duplicate, slow-retry, reply-first and references delivery
  scenarios, and whether the pipeline handles each.
- `loop fire`: machine mail and auto-reply loop detection.
- `steady fire`: label stability under meaning-preserving rewrites of the same
  message.
- `sigcheck fire`: webhook signature enforcement for Mailgun, SendGrid and
  Postmark basic auth. Up to six variants (`valid`, `wrong-secret`,
  `unsigned`, `stale-timestamp`, `tampered-body`, `replayed-token`); a variant
  the scheme has nothing to vary is not sent. The secret is an `env:NAME`
  reference and a literal secret is refused. Dry run by default. The dry run
  refuses a target whose format the scheme cannot sign, as `--send` does.
- `compare`: diff two blast runs payload by payload, or two barrage runs on
  load metrics (see Compare below).
- `report`: read any tool's artifact and summarise it as text, as JSON in one
  stable shape, or with `--html OUT` as a single self-contained HTML page
  (verdict, counts, findings, records, Barrage charts as inline SVG, config,
  exit code; light and dark; no network).
- `config validate`: load a config with the real loaders and print what they
  resolved, including each target's `format`, `public` and public-target
  resolution. Sends nothing and never prints a header value.

**Compare**

- Two barrage runs are diffed on error rate, hold-stage p99 latency and
  achieved rps. A candidate regressed when the error or unsent rate rises by
  more than 1 point, hold p99 rises by more than 20% and 5ms, or achieved rate
  falls by more than 10% (judged only when mode and rate match).
- It refuses a dry-run artifact, an artifact from another tool and a pair
  that mixes a blast run with a barrage run. It says "nothing to compare" when
  no blast records align.
- `--fail-on-regression` exits 3 on a regression. Without it the exit is 0.
  The artifact records `fail_on_regression` and the exit code.

**Report**

- `report --json` always carries `schema_version` and `aborted`, in one fixed
  key set for every tool.
- It reads the artifacts of every tool, including steady, sigcheck and
  compare.
- `tool` is `verify` for both `verify fire` and `verify check`; the text report
  shows which.
- A blast dry-run artifact reads as DRY RUN with exit code 2, and nothing in it
  counts as sent.
- A compare artifact reports the exit code the command gave, which depends on
  `--fail-on-regression`.

**Sending**

- `--allow-public-target` on every send command. Barrage at a public host also
  needs `public = true` on the target's config table.
- Target hosts are resolved: a name that resolves only to private, loopback or
  link-local addresses counts as private.
- `[targets.<name>].format` selects the wire format, and every send path uses
  it: `sendgrid`, `sendgrid-raw` (raw MIME in the `email` field), `mailgun`,
  `postmark` and `mime`. `docs/FORMATS.md` documents each.
- The `sendgrid` format carries the documented fields it was missing: `dkim`,
  `SPF`, `sender_ip`, `spam_score`, `spam_report`, `content-ids`, `cc`,
  `charsets.text` and `attachment-info[*].content-id`.
- A header hook: `transport.post` and `transport.build_request` accept
  `extra_headers`, a callable over the finished body bytes.

**Blast**

- `--mix clean=20,messy=40,...`, `--catalog [names]` and
  `--attachments none|seeded|adversarial` (default `seeded`).
- `blast fire --send --catalog` sends every case. The synthetic-address guard
  skips a field that is deliberately empty, and refuses any other value that
  holds no address.
- `--fail-on-body <regex>`: a 2xx whose body matches is a flag.
- Catalog cases `null_byte_in_body`, `rtl_override_in_filename`,
  `missing_boundary_field`, `duplicate_form_field`, `malformed_envelope_json`
  and `oversized_body_1mb`; Outlook and Apple Mail quote and forward shapes.
- Realistic clean mail: consistent names, topics and weekdays, signatures,
  Gmail and Outlook HTML wrappers, threading headers on replies, `Received`,
  `Return-Path` and `Authentication-Results` on every message, RFC 2047
  encoded non-ASCII subjects in `headers`.
- Encoding sabotage puts real cp1252, latin-1 and shift_jis bytes on the wire
  with a lying `charsets` entry, for `text` as well as subject and html.

**Barrage**

- Concurrent dispatch through a pool of `--concurrency` worker threads, capped
  at 64 unless `--allow-high-rate` is passed. A pool too small for the schedule
  shows as achieved throughput falling short, and the run says how many
  requests waited for a free worker and for how long.
- A real closed loop: N workers each send, wait, and take the next token. A
  shortfall that `--concurrency` explains (workers / median latency is below
  the planned rate) is not reported as an endpoint knee; the summary carries
  `concurrency_limited` and says to raise `--concurrency`.
- One keep-alive connection per worker (`--no-keepalive` opens one per
  request), with connection setup time reported separately.
- `--timeout` (default 10s), `--grace` (default 10s) and
  `--max-queue-seconds` (default 5).
- Per-request `records` in the artifact, so `compare` can diff two load runs.
- Report: 429 counted and named "shed", a status-class histogram per bucket,
  p50, p90 and p99 per bucket, service and offered latency series, and
  percentiles overall and for the hold stage.

**Readback**

- Retry with backoff and jitter on 429, 5xx, timeouts and connection errors,
  honouring `Retry-After` (`retry_max`, default 5), and a readback rate cap
  (`--readback-rate`, default 4/s).
- A preflight request before anything is sent: refused credentials (401 or
  403), a response that is not JSON, a field map that finds no identifier or a
  list url that 404s is a refusal. A 404 on the tag lookup is not, because a
  missing record answers 404; a url that 404s every lookup is READBACK FAILED.
- `query` templates, `method = "POST"` with a `body` template, and pagination
  by `next_path` or `page_param`/`page_size_param` with `max_pages`.
- `*` wildcards in field paths, `false` to unmap a default field, and senders
  stored as objects.
- Settling rule, set by `--quiet-window` (default 5s), `--max-wait` (default
  60s) and `--poll-interval`. The readback is settled once the found tags,
  their record counts and the checked field values have held still for
  `--quiet-window`, counted from the start of the first of two identical
  complete polls. A tag still missing then is lost. Holding still is also
  what catches a duplicate that lands after the first sighting.
- UNSETTLED (exit 3) only when `--max-wait` runs out while the store is still
  changing, or before every tag could be polled twice. The headline names which
  and gives the numbers, with the `--max-wait` or `--readback-rate` that would
  fit. Findings already proven stay listed and counted.
- Every readback tool's dry run warns when the plan cannot settle within
  `--max-wait`: about 2 x tags / `--readback-rate` plus `--quiet-window`.
- `--max-wait`, `--poll-interval` and `--quiet-window` are checked by the
  parser, before anything is sent: the first two must be above zero, the last
  zero or more.
- Every `--rate` and `--readback-rate` must be a positive number, refused at
  the parser.
- READBACK FAILED (exit 3) when every lookup was a 404. The readback url is
  almost certainly wrong, so no message is reported as lost: ledger lists the
  tags under `not_checked`, verify and redeliver say "not checked: every
  lookup was 404", and `report` shows one readback-failed finding.
- Readback fields `category` and `priority`, so a tool can read a classifier's
  label.
- Recipes for Zendesk, Freshdesk, a generic REST API, an outbound mail sink,
  Mailpit, Chatwoot and a HandleHQ deployment in `examples/readback/`. The
  HandleHQ recipe has not been run against a live deployment.
- Per-run default tag prefix `hq-<yymmddHHMM>-<4 hex>`; `--tag-prefix`
  overrides it and the artifact records it.
- Message-IDs are unique per run, so a pipeline that drops a repeated
  Message-ID does not drop a second run. Verify, ledger, redeliver and steady
  derive each from the message's tag; sigcheck draws one per run and every
  variant shares it. One explicit `--tag-prefix` gives the same Message-IDs on
  every run.

**Checks**

- `--subject-match exact|contains|suffix` (default `contains`) and
  `--attachment-match exact|loose`, honoured by verify, ledger and redeliver
  and recorded in the artifact.
- `--expect-route` honoured by verify, ledger and redeliver.
- `verify check` re-checks with the match modes the artifact recorded and says
  where each came from. A flag overrides one; `--no-expect-route` and
  `--no-body-exact` turn those two off.
- `[loop].ticket_policy` and `[steady].intents_path` config keys.
- A key or top-level table the schema does not declare is refused (exit 1)
  before anything is sent, naming the file, the table, the key and the closest
  known key. `config validate` reports the same error and checks the key names
  of `[loop]`, `[steady]` and `[sigcheck]`. The names inside `[readback.headers]`
  and `[readback.fields]` stay open, and a custom adapter's table may carry its
  own keys.

**Artifacts and project**

- Every artifact carries `schema_version: 1` and a top-level `tool`. Readers
  treat a missing `schema_version` as 0 and refuse a newer one.
- `docs/CONFIG.md`, generated from `testinghq/core/config_schema.py`, with a
  test that fails when the two drift.
- `docs/README.md`, an index of the docs, and `docs/DOGFOOD.md`, how to run
  the suite against a real deployment.
- `examples/pipeline_demo.py` runs verify, ledger, redeliver, loop and steady
  over real sockets against a pipeline that imports nothing from TestingHQ and
  owns its machine-mail check, tag regex, Message-ID dedup, threading and
  classifier. Its defects are on the write side and each run's exit code is
  asserted.
- `CONTRIBUTING.md`, `ruff.toml` (pyflakes rules), a lint job in CI, and a
  `report-html-tests` job that runs the render.js tests against the installed
  wheel.

### Changed

- **One exit-code convention across every tool**: 0 ran and yes, 1 refused, 2
  dry run, 3 ran and found something. A usage error exits 1 rather than
  argparse's 2. `blast fire --send` exits 3 when anything is flagged.
- **`compare` shares the convention**: a usage error is 1 and, with
  `--fail-on-regression`, a regression is 3.
- **Ctrl-C** in a long-running command writes what it has (when `--out` was
  given) with `aborted: true` and exits 3.
- **Blast verdict**: a 5xx or transport failure in any category is a flag. A
  4xx on malformed input is recorded and not flagged. Transport failures are
  reported by reason (refused, DNS, timeout, TLS, other).
- **`headers` stay in sync** with `subject`, `to` and `from` after every
  mutation and catalog case.
- **The default Blast corpus contains attachments** (`--attachments seeded`).
- **Barrage** ends at `duration + grace` whatever the target does: in-flight
  requests past the deadline are `abandoned`, queued ones `not_dispatched`,
  and requests that waited past `--max-queue-seconds` are `dropped_backlog`.
  Achieved throughput is measured over the real dispatch span. The knee is the
  first of two buckets in a row that shed (error rate of 5% or more), send over
  20% fewer requests than planned, or run at over twice the hold-stage median
  latency. In a closed loop, fewer requests than planned count only when the
  workers could have sent more.
- **Routing is not checked unless `--expect-route` is given**; it reports
  SKIPPED with "no expected route".
- **Subjects match by containment by default**, so a stored
  "[Ticket #123] Re: ..." passes. An empty expected body is SKIPPED. HTML
  bodies are compared after tag stripping. "Last, First <addr>" senders parse.
- **ledger** reports UNVERIFIED STRAYS when the adapter cannot enumerate,
  instead of refusing after sending. "Extra" means records carrying this run's
  tag family that this run did not send; other runs' records are listed
  separately and untagged records are not strays. The `extra` list is capped
  at 200 with a count.
- **`--max-wait` is one budget for the whole readback**: a request or retry
  that would run past it ends the poll.
- **A spec adapter** (`--readback module:attr`) may be a class (instantiated),
  an object with `fetch(probe)`, a one-argument fetch function, or a factory
  taking nothing or `config`. An import error is a refusal before anything is
  sent. The class or factory gets the table with the adapter's own keys as
  written (an `env:NAME` value in them arrives unresolved) plus the built-in
  keys; header names are included and header values are not.
- **redeliver** slow-retry sends every original, waits once, then sends every
  retry; the references scenario checks the full `References` ancestry. Every
  Message-ID, In-Reply-To and References id it writes is in angle brackets.
- **loop** enumerates the outbound sink, so an auto-reply it cannot attribute
  is a finding ("outbound, unattributed") rather than "no reply". The loop
  bait is an auto-reply from the customer's address to the pipeline's real
  outbound Message-ID, falling back to a made-up parent when no acknowledgement
  arrives within `--ack-wait`. `--reply-address` takes precedence over the
  config file.
- **steady** headline is per-family majority dissent, with the pairwise flip
  rate secondary (it gates the exit code only with `--max-flip-rate`). Cross-language pairs are
  reported separately and every report has a sample-size line. It refuses when
  every family would read the same constant label.
- The CLI is a package, one module per command. The command surface is
  unchanged.
- `project.license` is the SPDX string `"MIT"`.
- The sdist ships the package, the docs and the examples, not `tests/`.
- The suite's meta tests (README wording, CI YAML, docstrings, file absence)
  are cut to the ones that guard behaviour.
  `docs/decisions/0003-what-the-suite-protects.md` records what each removed
  check had encoded.

### Fixed

- **`--allow-public-readback` wins over `allow_public_hosts = false`** in the
  `[readback]` table of the config file.
- `steady` runs from an installed wheel: its intent fixture is package data.
- A negative `--count` is a refusal, not a traceback.
- The mailbox readback adapter tolerates a partially written trailing line and
  parses the file once per poll instead of once per tag per poll.
- A readback list under the wrong `items_key` says so by name.
- A relative mailbox path resolves against the config file's directory, not
  the working directory.
- Undefined names (`Any` in `core/config.py`, `FormPart` in `core/formats.py`),
  the `record_id_for` export that did not exist, and unused imports.

### Removed

- **The web UI** (`web/`), which exposed only Blast. Its rendering lives
  on as `report --html`.
- **The marketing page** (`site/`).
- `--expect-route` on `loop` and `steady`, which never honoured it.
- `testinghq/pipeline/corpus.py`, a duplicate of code in `verify.py`.

### Security

- Public targets require explicit consent: `--allow-public-target`, plus
  `public = true` for Barrage. Synthetic reserved-domain addresses, the
  dry-run default and the configured-target allow-list are unchanged.
- Readback URLs on a public host are refused unless `--allow-public-readback`
  is passed.
- `[readback.headers]` values must be `env:NAME` references, checked before
  anything is sent, and an artifact records header names, never values.
- Any artifact written at a public target records `allow_public_target: true`
  under `config`.
- The `report --html` page carries a Content-Security-Policy that allows only
  its own inline script and style, and escapes every value from the artifact.
- The CI, e2e and security workflows run with a read-only token and have job
  timeouts, and cancel superseded pull request runs.

### Provenance

This release has **not** been run against any real third-party mail provider
or ticket system. The provider wire formats and the vendor readback recipes are
written from published documentation and checked against sample responses,
not against a live account. Everything else is run by the suite, and the parts
that open sockets run in CI against loopback.

[Unreleased]: https://github.com/saifullah4khan/TestingHQ/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/saifullah4khan/TestingHQ/releases/tag/v1.0.0
