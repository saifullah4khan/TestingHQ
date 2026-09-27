# Examples

Runnable examples. These demonstrate usage; they are not the test suite. For
the real hermetic tests, see `tests/integration/`.

## Files

- **`target.example.toml`** -- a sample target configuration, shaped to
  match `testinghq.core.config.Config` (a named table of targets, each with
  a `name` and a `url`), plus an optional `[readback]` table describing where
  the pipeline's own output can be read from. Copy it, rename it, and point
  `url` at an intake endpoint you own or have explicit permission to test.
  Pass it with `--config <path>`, which defaults to `./target.toml`, so
  `testinghq blast fire --target <name> --config target.toml` reads a file
  shaped like this one.

  The `[readback]` table is only used by `testinghq verify`, `testinghq ledger`
  and `testinghq redeliver`. Everything else in the file is a comment, because
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

  It does not POST. The integration tests in `tests/integration/` are what
  check payloads survive the real wire format intact, against the in-process
  sink at `tests/integration/fake_sink.py`.

- **`pipeline_demo.py`** -- an end-to-end run of `verify`, `ledger` and
  `redeliver` against a real HTTP server it starts on 127.0.0.1. This one
  does open sockets, to loopback only.

  ```
  python examples/pipeline_demo.py
  ```

  It is here because every other test in this repository is hermetic, and the
  injectable-client seam that buys that means nothing in the suite proves the
  real transport, the real serializer and the real readback HTTP client can
  talk to a real server. A seam that has only ever met a stub is a seam nobody
  has checked.

  The server it starts is a small, correct intake pipeline: it deduplicates on
  Message-ID, and it files a reply on its parent's ticket even when the reply
  arrives first. Every address in every payload is synthetic and
  reserved-example, and nothing is sent anywhere but loopback.

  Read the output for the two runs that matter. The first fires 12 messages at
  that correct pipeline and `verify` says VERIFIED with all six checks
  running. The second fires the same 12 at the same server and gets
  MISMATCHED, naming four messages the system does not hold. Every status code
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
