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
  answered. A 200 is not evidence a message was parsed.
- `ledger fire`: account for every payload sent against every record produced:
  missing, duplicated, misparsed and stray records.
- `redeliver fire`: duplicate, slow-retry, reply-first and references delivery
  scenarios, and whether the pipeline handles each.
- `loop fire`: machine mail and auto-reply loop detection.
- `steady fire`: label stability under meaning-preserving rewrites of the same
  message.
- `sigcheck fire`: webhook signature enforcement, with `valid`, `wrong-secret`,
  `unsigned`, `stale-timestamp`, `tampered-body` and `replayed-token` variants
  for Mailgun, SendGrid and Postmark basic auth. Dry run by default.
- `compare`: diff two artifacts and report regressions.
- `report`: read any tool's artifact and summarise it as text, as JSON in one
  stable shape, or with `--html OUT` as a single self-contained HTML page
  (verdict, counts, findings, records, Barrage charts as inline SVG, config,
  exit code; light and dark; no network).
- `config validate`: load a config with the real loaders and print what they
  resolved, including each target's `format`, `public` and public-target
  resolution. Sends nothing and never prints a header value.

**Sending**

- `--allow-public-target` on every send command. Barrage at a public host also
  needs `public = true` on the target's config table.
- Target hosts are resolved: a name that resolves only to private, loopback or
  link-local addresses counts as private.
- `[targets.<name>].format` selects the wire format, and every send path uses
  it: `sendgrid`, the new `sendgrid-raw` (raw MIME in the `email` field),
  `mailgun`, `postmark` and `mime`. `docs/FORMATS.md` documents each.
- The `sendgrid` format carries the documented fields it was missing: `dkim`,
  `SPF`, `sender_ip`, `spam_score`, `spam_report`, `content-ids`, `cc`,
  `charsets.text` and `attachment-info[*].content-id`.
- A header hook: `transport.post` and `transport.build_request` accept
  `extra_headers`, a callable over the finished body bytes.

**Blast**

- `--mix clean=20,messy=40,...`, `--catalog [names]` and
  `--attachments none|seeded|adversarial` (default `seeded`).
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
- A real closed loop: N workers each send, wait, and take the next token.
- One keep-alive connection per worker (`--no-keepalive` for the old
  behaviour), with connection setup time reported separately.
- `--timeout` (default 10s), `--grace` (default 10s) and
  `--max-queue-seconds` (default 5).
- Per-request `records` in the artifact, so `compare` can diff two load runs
  on error rate and p99.
- Report: 429 counted and named "shed", a status-class histogram per bucket,
  p90 and p99 per bucket, service and offered latency series, and percentiles
  overall and for the hold stage.

**Readback**

- Retry with backoff and jitter on 429, 5xx, timeouts and connection errors,
  honouring `Retry-After` (`retry_max`, default 5), and a readback rate cap
  (`--readback-rate`, default 4/s).
- A preflight request before anything is sent, so bad auth or a wrong path is
  a refusal rather than a run reporting total loss.
- `query` templates, `method = "POST"` with a `body` template, and pagination
  by `next_path` or `page_param`/`page_size_param` with `max_pages`.
- `*` wildcards in field paths, `false` to unmap a default field, and senders
  stored as objects.
- Polling with `--quiet-window`, `--max-wait` and `--poll-interval`: waiting
  for the record counts to hold steady is what catches a duplicate that lands
  after the first sighting.
- Readback fields `category` and `priority`, so a tool can read a classifier's
  label.
- Recipes for Zendesk, Freshdesk, a generic REST API, an outbound mail sink,
  Mailpit, Chatwoot and a real deployment, in `examples/readback/`.
- Per-run default tag prefix `hq-<yymmddHHMM>-<4 hex>`; `--tag-prefix`
  overrides it and the artifact records it.

**Checks**

- `--subject-match exact|contains|suffix` (default `contains`) and
  `--attachment-match exact|loose`, honoured by verify, ledger and redeliver
  and recorded in the artifact.
- `--expect-route` honoured by ledger and redeliver.
- `[loop].ticket_policy` and `[steady].intents_path` config keys.

**Artifacts and project**

- Every artifact carries `schema_version: 1` and a top-level `tool`. Readers
  treat a missing `schema_version` as 0 and refuse a newer one.
- `docs/CONFIG.md`, generated from `testinghq/core/config_schema.py`, with a
  test that fails when the two drift.
- `CONTRIBUTING.md`, `ruff.toml` (pyflakes rules), a lint job in CI, and a
  `report-html-tests` job that runs the render.js tests against the installed
  wheel.

### Changed

- **One exit-code convention across every tool**: 0 ran and yes, 1 refused, 2
  dry run, 3 ran and found something. A usage error exits 1 rather than
  argparse's 2. `blast fire --send` now exits 3 when anything is flagged; it
  exited 0 unconditionally.
- **`compare` exit codes changed**: a regression is 3 (was 1) and a usage
  error is 1 (was 2).
- **Ctrl-C** in a long-running command writes what it has (when `--out` was
  given) with `aborted: true` and exits 3, instead of a traceback and no
  artifact.
- **Blast verdict**: a 5xx or transport failure in any category is a flag,
  not only in `clean` and `degenerate`. A 4xx on malformed input is recorded
  and not flagged. Transport failures are reported by reason (refused, DNS,
  timeout, TLS, other) rather than all as "timed out".
- **`headers` stay in sync** with `subject`, `to` and `from` after every
  mutation and catalog case.
- **The default Blast corpus contains attachments** (`--attachments seeded`).
- **Barrage** ends at `duration + grace` whatever the target does: in-flight
  requests past the deadline are `abandoned`, queued ones `not_dispatched`,
  and requests that waited past `--max-queue-seconds` are `dropped_backlog`.
  The open loop no longer pauses between ramp stages. Achieved throughput is
  measured over the real dispatch span. Knee detection compares achieved with
  target rate and the 429/5xx rate, needs two buckets in a row, and takes its
  baseline from the hold stage.
- **Routing is not checked unless `--expect-route` is given**; it reports
  SKIPPED with "no expected route" rather than failing on every message.
- **Subjects match by containment by default**, so a stored
  "[Ticket #123] Re: ..." passes. An empty expected body is SKIPPED. HTML
  bodies are compared after tag stripping. "Last, First <addr>" senders parse.
- **ledger** reports UNVERIFIED STRAYS when the adapter cannot enumerate,
  instead of refusing after sending. "Extra" means records carrying this run's
  tag family that this run did not send; other runs' records are listed
  separately and untagged records are not strays. The `extra` list is capped
  at 200 with a count.
- **A readback that never settles is UNSETTLED** (exit 3), distinct from lost
  mail, and `--max-wait` is enforced inside the per-tag loop. The quiet window
  watches the checked field values as well as the counts.
- **A spec adapter** (`--readback module:attr`) that is a class is
  instantiated; any one-argument callable is a fetch function; an import error
  is a refusal before anything is sent.
- **redeliver** slow-retry sends every original, waits once, then sends every
  retry; the references scenario checks the full `References` ancestry.
- **loop** enumerates the outbound sink, so an auto-reply it cannot attribute
  is a finding ("outbound, unattributed") rather than "no reply". The loop
  bait replies to the pipeline's real outbound Message-ID from a third-party
  autoresponder address. Every corpus shape is sent at the default count. The
  `--reply-address` flag takes precedence over the config file.
- **steady** subjects no longer carry the expected label. The headline is
  per-family majority dissent, with the pairwise flip rate secondary,
  cross-language pairs reported separately, and a sample-size line in every
  report. It refuses when every family would read the same constant label.
  Transforms preserve meaning for a triage classifier.
- **`compare`** refuses a dry-run artifact and says "nothing to compare" when
  no records align, instead of reporting fake fixes or "no regressions".
- **`report`** reads steady and sigcheck artifacts.
- The CLI is a package, one module per command, in place of a 1570-line
  `cli.py`. The command surface is unchanged.
- `project.license` is the SPDX string `"MIT"`.
- The suite's meta tests (README wording, CI YAML, docstrings, file absence)
  are cut to the ones that guard behaviour.
  `docs/decisions/0003-what-the-suite-protects.md` records what each removed
  check had encoded.

### Fixed

- **`steady` could not run from an installed wheel**: its intent fixture was
  not package data.
- A negative `--count` is a refusal, not a traceback.
- The mailbox readback adapter tolerates a partially written trailing line and
  parses the file once per poll instead of once per tag per poll.
- A readback list under the wrong `items_key` says so by name, and a 404 on
  every lookup says "check the readback url".
- `build_adapter(config_dir=...)` is passed by every caller, so mailbox paths
  resolve against the config file, not the working directory.
- Undefined names (`Any` in `core/config.py`, `FormPart` in `core/formats.py`),
  the `record_id_for` export that did not exist, and unused imports.
- A `NetworkBlocked` exception could be swallowed by a broad `except
  Exception` in the send path, so a message never sent could be recorded as a
  clean failure.
- Send plans are validated before the tool announces that it is executing.

### Removed

- **The web UI** (`web/`), which exposed only Blast and had thrown on every
  page load since it last changed. Its rendering lives on as
  `report --html`.
- **The marketing page** (`site/`).
- `--expect-route` on `loop` and `steady`, which never honoured it.
- `testinghq/pipeline/corpus.py`, a duplicate of code in `verify.py`.
- Dead parameters and unused state across Barrage, loop and steady.
- `tests/` and `web/` from the sdist. The sdist now ships the package, the
  docs and the examples.

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
- CI workflows run with a read-only token, cancel superseded pull request
  runs, and have job timeouts.

### Provenance

This release has **not** been run against any real third-party mail provider
or ticket system. The provider wire formats and the vendor readback recipes are
written from published documentation and checked against sample responses,
not against a live account. Everything else is run by the suite, and the parts
that open sockets run in CI against loopback.

[Unreleased]: https://github.com/saifullah4khan/TestingHQ/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/saifullah4khan/TestingHQ/releases/tag/v1.0.0
