"""Barrage: a load generator for TestingHQ.

Barrage fires provider-shaped payloads at an endpoint the operator controls,
at a high but controlled rate, and reports throughput, latency distribution,
and error behaviour under sustained load. It is a load tester against your
own infrastructure.

Barrage is explicitly NOT an email sender, NOT a flooding tool, and NOT for
endpoints you do not own. The rate ceiling, the configured-target-only rule,
and the dry-run default are what keep it a load tester rather than a
weapon. They are non-negotiable: nothing in this package weakens them.

Where Blast (testinghq/blast) proves the parser is correct under messy
input, Barrage proves the pipeline holds under load. Same suite, same
installable, same core: Barrage reuses testinghq.core.ratelimit,
testinghq.core.guardrails, and testinghq.core.transport rather than
reimplementing any of them.
"""
