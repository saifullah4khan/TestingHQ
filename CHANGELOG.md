# Changelog

All notable changes to TestingHQ are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

`0.x` releases are for a tool whose command surface is still moving. A breaking
change to a flag, an exit code, or an artifact shape is expected between minor
releases and is called out under `Changed` below rather than hidden.

## [Unreleased]

Nothing yet. The next set of merged pull requests lands here.

## [0.1.0] - 2026-09-28

The first published release. Everything below shipped in it.

### Added

**Tools**

- `verify fire` and `verify check`: check what a pipeline actually produced,
  not what it answered. A 200 is not evidence a message was parsed.
- `ledger fire`: account for every payload sent against every record produced.
  Reports missing, duplicated, misparsed and stray records.
- `redeliver fire`: four delivery scenarios (duplicate, slow-retry, reply-first,
  references) and whether a correct pipeline handles each.
- `report`: read a run artifact from any tool and summarise it. One stable JSON
  shape whatever wrote the file.
- `config validate`: load a config with the real loaders and print the effective
  config. Sends nothing, and never prints a header value.

**Pluggable wire formats.** `blast` could only speak multipart/form-data in the
SendGrid Inbound Parse shape. `[targets.<name>].format` now selects `sendgrid`
(the default, byte-for-byte what was emitted before), `mailgun`, `postmark` or
`mime`.

**A header hook.** `transport.post` and `transport.build_request` accept
`extra_headers`, a callable taking the finished body bytes and returning
headers. It runs after the body is built, because a signature covers specific
bytes. It may add headers but not replace `Content-Type` or `Content-Length`.

**Readback recipes** for Zendesk, Freshdesk, a generic REST API, and an
outbound mail sink, in `examples/readback/`. Each states whether it was run or
written from vendor documentation, and a test checks the statement.

**`docs/CONFIG.md`**, generated from `testinghq/core/config_schema.py`, with a
test that fails when the checked-in file no longer matches.

**Readback fields** `category` and `priority`, so a tool can measure a
classifier's label. No verify check uses them, because the corpus has no
triager's label to compare against.

**Readback polling.** `--quiet-window`, `--max-wait` and `--poll-interval`.
Waiting on the record *counts* holding steady is what catches a duplicate that
lands after the first sighting.

### Changed

- **`compare` exit codes changed. This is the one breaking change in 0.1.0.**

  | Situation | Before | Now |
  | --- | --- | --- |
  | no regression | 0 | 0 |
  | a regression | 1 | **3** |
  | a usage error | 2 | **1** |

  A script reading `1` as "refused" was reading a `compare` regression as the
  tool declining to run. A regression is a result, not a failure to run.

- **One exit-code convention across every tool**: 0 ran and yes, 1 refused, 2
  dry run, 3 ran and no. A usage error exits 1 rather than argparse's 2, so a
  mistyped flag cannot be misread as a dry run.

- `--concurrency` above 1 is refused in both Barrage modes rather than accepted
  and ignored in one of them.

- README examples are executed as dry runs by the suite, so a documented command
  cannot rot into one that is refused.

### Fixed

- A `NetworkBlocked` exception could be swallowed by a broad `except Exception`
  in the send path, so a message that was never sent could be recorded as a
  clean failure. It now inherits from `BaseException`, so a `except Exception`
  cannot misclassify it.
- Send plans are validated before the tool announces that it is executing.

### Security

- Readback URLs on a public host are refused unless `--allow-public-readback`
  is passed, because on a real deployment a readback URL is often a ticket store
  holding other people's data.
- `[readback.headers]` values must be `env:NAME` references. A literal is
  refused, and so is a variable that is not set, before anything is sent.
- A run artifact records header *names* and never values.

### Provenance

This release has **not** been run against any real third-party mail provider or
ticket system. The `mailgun` and `postmark` wire formats and the Zendesk and
Freshdesk readback recipes are written from those vendors' published
documentation and verified against sample responses, not against a live
account. `docs/CONFIG.md` and this file are generated and have been checked
against the code. Everything else was run by the suite, and the parts that open
sockets are run in CI against loopback.

[Unreleased]: https://github.com/saifullah4khan/TestingHQ/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saifullah4khan/TestingHQ/releases/tag/v0.1.0
