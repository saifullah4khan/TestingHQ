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
(`testinghq blast ...`, `testinghq barrage ...`). Blast and Barrage share a common
core: the firing transport, target configuration, guardrails, and rate limiting.

## Status

This is early. The M0 skeleton is in place: the package installs, the CLI surface
and safety guardrails exist and are tested, and CI runs on every change. The
generator, transport, reporting, and web UI land across the milestones below.

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
testinghq barrage fire --target local --rate 20 --duration 60 --concurrency 8 --send

# open-loop (fixed arrival rate, finds the breaking point) is the default;
# closed-loop holds concurrency fixed and lets offered load self-limit
testinghq barrage fire --target local --mode closed --concurrency 8 --send

# write the run artifact, then re-run it later from its seed and config
testinghq barrage fire --target local --send --out load.json
testinghq barrage replay load.json --send
```

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

## License

MIT. See [LICENSE](LICENSE).

Contact: saifullah4khan@gmail.com
