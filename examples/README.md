# Examples

Runnable examples for Blast. These demonstrate usage; they are not the test
suite. For the real hermetic tests, see `tests/integration/`.

## Files

- **`target.example.toml`** -- a sample target configuration, shaped to
  match `testinghq.core.config.Config` (a named table of targets, each with
  a `name` and a `url`). Copy it, rename it, and point `url` at an intake
  endpoint you own or have explicit permission to test. The TOML loader
  that reads a file like this is not wired up yet (`docs/agents/BLAST_BACKLOG.md`,
  M1, Lane A); once it lands, `testinghq blast fire --target <name>` will
  read a file shaped like this one.

- **`demo.py`** -- not yet wired. It depends on the clean generator
  (`testinghq/blast/generate.py`) and the transport (`testinghq/core/transport.py`),
  both still in progress (M1, Lane A). Once those land, this file will
  generate a clean corpus from a seed and dry-run it end to end against
  `tests/integration/fake_sink.py`, the same in-process sink the
  integration tests use to check payloads survive the real wire format
  intact.

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
