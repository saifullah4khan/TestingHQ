# Security policy

## Responsible use

TestingHQ POSTs provider-shaped webhook payloads at an HTTP endpoint you
control, to find where your inbound-email pipeline breaks. It is not an email
sender. It never opens an SMTP connection, never delivers to a real inbox, and
does not forge sender authentication or evade spam filters. Contributions that
add any of those are out of scope and will not be accepted.

Only test endpoints you own or are explicitly authorized to test. The
guardrails below stop this tool from making that mistake automatically; they
are not a substitute for authorization.

## What the tool cannot do

- **Send without asking.** Dry run is the default on every command. Sending
  needs `--send` on that run; no configuration makes sending the default.
- **Fire at an unconfigured target.** Only a name declared in
  `[targets.<name>]` can be fired at, checked before any request.
- **Generate a real address.** Every generated address is on a reserved domain
  or TLD (RFC 2606, RFC 6761: `example.com/.net/.org/.edu`, `.test`,
  `.invalid`, `.example`, `.localhost`). Every address in every payload is
  checked before the first request, with `email.utils.getaddresses`, so a
  multi-recipient header cannot smuggle a second address past the check.
- **Claim a real domain's authentication.** The SendGrid `dkim` and `SPF`
  fields are fixed synthetic values naming `example.com`, and `sender_ip` is
  `192.0.2.1` (TEST-NET-1). See docs/FORMATS.md.
- **Flood.** Every send path is paced by a token bucket. Barrage has a hard
  ceiling on rate, duration and concurrency, raised only by `--allow-high-rate`.

## What needs explicit consent

A **public host** is one that is not a reserved test domain, not localhost or
a bare internal name, not a private, loopback or link-local address, and does
not resolve only to such addresses.

| Action | Requires |
| --- | --- |
| Fire at a public target (blast, verify, ledger, loop, redeliver, steady) | `--allow-public-target` on the run |
| Load-test a public target with barrage | `--allow-public-target` on the run **and** `public = true` in `[targets.<name>]` |
| Read back from a public readback URL | `--allow-public-readback` on the run, or `allow_public_hosts = true` in `[readback]` |

The flag is per run on purpose: the decision is made again every time. The
artifact of a run fired at a public host records `allow_public_target: true`
under `config`. `testinghq config validate` prints, per target, whether its
host is public and what firing at it needs.

## How "public" is decided, and its limits

The target's host is checked when the run starts, before any request:

1. A reserved name, localhost, a single-label name or a private IP literal is
   private. A public IP literal is public.
2. Any other name is resolved with the system resolver. If every address is
   private, loopback or link-local, it is private (a split-horizon internal
   name). If any address is public, or the lookup fails, it is public.

Caveats:

- The check runs once, at resolution, and the result is cached for the
  process. The HTTP client resolves the name again when it connects, so DNS
  that changes in between (rebinding, a TTL expiring, a different resolver)
  can send traffic to an address the check never saw. The check stops
  mistakes, not an adversary who controls your DNS.
- A name that resolves privately on your network may resolve publicly
  elsewhere. Run the check from the machine that sends.
- The readback guard uses the string check only, with no DNS lookup.
- `TESTINGHQ_TARGET_<NAME>_URL` replaces a target's URL and goes through the
  same check.

## Threat model

In scope: TestingHQ used against your own systems. Assets defended: third
parties (real mailboxes, real domains, other people's reputation) must never
be reachable as a side effect; your own endpoint must not be hit unexpectedly
or flooded; generated content must not be mistakable for a real identity.

Scenarios: accidental real delivery (dry-run default, allow-list, public-host
consent); repurposing as a spam tool (synthetic addresses only, no SMTP, no
authentication forgery); runaway volume (rate limits, Barrage ceilings);
supply-chain compromise (stdlib only at run time, Dependabot, a scheduled
dependency audit in CI).

Out of scope: consent to test an endpoint you do not control, which is your
responsibility; the security of the service you point TestingHQ at.

## Reporting a vulnerability

Report privately, not in a public issue: email **saifullah4khan@gmail.com**
with the affected version or commit and reproduction steps. Examples worth
reporting: a dry run that makes a network call; a send to an unconfigured
target or to a public host without consent; a non-reserved address that passes
`require_synthetic_content`; a way around the rate limiter. Please allow
reasonable time for a fix before disclosure, and do not include third-party
personal data or evidence from systems you were not authorized to test.
