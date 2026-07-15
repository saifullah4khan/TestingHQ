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

**Barrage (next).** Volume and throughput. Where Blast proves your parser is
correct under messy input, Barrage will prove your pipeline holds under load.

The suite ships as one installable package, `testinghq`, with subcommands
(`testinghq blast ...`, later `testinghq barrage ...`). Blast and Barrage share a
common core: the firing transport, target configuration, guardrails, and rate
limiting.

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

# actually fire at a configured target (explicit and rate limited)
testinghq blast fire --target local --send
```

## Responsible use

Blast is a fuzzer and self-testing tool for endpoints you control. It POSTs
provider-shaped payloads at your own ingest. It is not an email sender: it does
not deliver mail to arbitrary inboxes, it does not try to defeat spam filters,
and it does not forge sender authentication to fool real recipients. All
generated content is synthetic and uses reserved example domains.

Dry-run is the default. Firing requires an explicit `--send` flag and a target
you have declared in configuration. Rate limiting is on by default.

## License

MIT. See [LICENSE](LICENSE).

Contact: saifullah4khan@gmail.com
