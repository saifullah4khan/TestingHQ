# Examples

Runnable examples for Blast. These demonstrate usage; they are not the test
suite. For the real hermetic tests, see `tests/integration/`.

## Files

- **`target.example.toml`** -- a sample target configuration, shaped to
  match `testinghq.core.config.Config` (a named table of targets, each with
  a `name` and a `url`). Copy it, rename it, and point `url` at an intake
  endpoint you own or have explicit permission to test. Pass it with
  `--config <path>`, which defaults to `./target.toml`, so
  `testinghq blast fire --target <name> --config target.toml` reads a file
  shaped like this one.

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
