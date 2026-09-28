# Examples

Runnable examples. These demonstrate usage; they are not the test suite. For
the real hermetic tests, see `tests/integration/`.

## Files

- **`readback/`** -- five worked `[readback]` configurations, one per shape of
  intake system: `zendesk.toml`, `freshdesk.toml`, `generic-rest.toml`,
  `mail-sink.toml` and `handlehq.toml`. These are the fastest route from "I
  have a ticket API" to a config that loads.

  Each one states, in its own first lines, what was verified and what was not.
  Two of them (`mail-sink.toml`, `generic-rest.toml`) were run by this
  repository's own tests. The two vendor recipes were written from the
  published API documentation and have **not** been pointed at a live
  Zendesk or Freshdesk account, because nobody on this project has one.
  `handlehq.toml` is a template for a real deployment and has never been run
  against one; `docs/DOGFOOD.md` is what a run looks like when it happens.

- **`target.example.toml`** -- a sample target configuration, shaped to
  match `testinghq.core.config.Config` (a named table of targets, each with
  a `name`, a `url` and an optional `format`), plus an optional `[readback]`
  table describing where
  the pipeline's own output can be read from. Copy it, rename it, and point
  `url` at an intake endpoint you own or have explicit permission to test.
  Pass it with `--config <path>`, which defaults to `./target.toml`, so
  `testinghq blast fire --target <name> --config target.toml` reads a file
  shaped like this one.

  The `[readback]` table is used by the five pipeline tools: `testinghq
  verify`, `testinghq ledger`, `testinghq redeliver`, `testinghq loop` and
  `testinghq steady`. Everything else in the file is a comment, because
  an example that cannot be parsed is worse than no example: it produces a
  refusal that names a file the reader has no reason to distrust. A test loads
  this file and builds an adapter from it, so the two cannot drift apart.

- **`demo.py`** -- a self-contained dry run: builds a small deterministic
  clean corpus from a seed with `testinghq.blast.generate`, serializes each
  payload the way `transport.post` would, and stops there. It never touches
  the network, so it is safe to run anywhere.

  ```
  python examples/demo.py
  python examples/demo.py --count 50 --seed 7
  ```

  By default it does not POST. `--send` runs the send path against an in-process
recording fake, so a socket is still never opened. The integration tests in `tests/integration/` are what
  check payloads survive the real wire format intact, against the in-process
  sink at `tests/integration/fake_sink.py`.

- **`pipeline_demo.py`** -- an end-to-end run of `verify`, `ledger`,
  `redeliver`, `loop` and `steady` against a real HTTP server it starts on
  127.0.0.1. Twelve CLI invocations: four of `verify` (three `fire` and one
  `check`), two of `ledger`, one of `redeliver`, three of `loop` and two of
  `steady`. This one does open sockets, to loopback only.

  ```
  python examples/pipeline_demo.py
  ```

  It is here because most of this repository's suite is hermetic, and the
  injectable-client seam that buys that means the real transport, the real
  serializer and the real readback HTTP client are only proven against a real
  server here and in `tests/e2e/`. A seam that has only ever met a stub is a
  seam nobody has checked.

  The server it starts is a small, correct intake pipeline: it deduplicates on
  Message-ID, and it files a reply on its parent's ticket even when the reply
  arrives first. Every address in every payload is synthetic and
  reserved-example, and nothing is sent anywhere but loopback.

  Read the output for the two runs that matter. The first fires 12 messages at
  that correct pipeline and `verify` says VERIFIED with all six checks
  running. The second fires the same 12 at the same server and gets
  MISMATCHED, naming the messages the system does not hold. Every status code
  in both runs was a 200. That difference is the entire argument for these
  tools, made by running them.

## Dry-run versus live

Every Blast invocation is dry-run by default: it builds payloads and shows
what it would send, with no network calls. Firing for real requires both an
explicit `--send` flag and a target you have declared in your config, per
the guardrails in `testinghq/core/guardrails.py`.

```
# dry-run: builds payloads, shows what would be sent, no network calls
testinghq blast fire --target local

# live: actually POSTs to the configured target's url
testinghq blast fire --target local --send
```

`local` above must match a table name in your config file (see
`target.example.toml`). Firing at a target name that isn't in your config
is refused: see `require_configured_target` in `testinghq/core/guardrails.py`.

## Responsible use

Blast is a fuzzer and self-testing tool for intake endpoints you control.
It is not an email sender. Point it only at endpoints you own or have
explicit permission to test; see the "Responsible use" section of the
top-level [README](../README.md) for the full guardrail framing.
