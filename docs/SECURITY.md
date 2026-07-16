# Security policy

## Responsible use

TestingHQ, and its first tool Blast, exist to help operators find where
their own intake pipeline breaks under messy real-world input. Every
guardrail in this codebase exists to keep that true in practice, not just
in the README.

The one-sentence distinction: **Blast POSTs provider-shaped payloads at
your own ingest endpoint; it is not an email sender, and it does not spoof
real recipients or defeat spam filters.**

In concrete terms:

- Blast never delivers mail to a real inbox. It sends HTTP POST requests
  shaped like an inbound-email-parsing provider's webhook payload (for
  example, SendGrid Inbound Parse), to an HTTP endpoint you configure. No
  SMTP connection to a real mail server is ever made.
- Generated "from" and "to" addresses are synthetic and use only reserved
  domains and TLDs (RFC 2606 / RFC 6761: `example.com`, `example.net`,
  `example.org`, `example.edu`, and the `.test`, `.invalid`, `.example`,
  `.localhost` TLDs). There is no code path that constructs an address on
  a real, resolvable domain.
- Blast does not implement, and will not accept contributions that
  implement, sender authentication forgery (SPF/DKIM/DMARC bypass),
  spam-filter evasion, or delivery to arbitrary third-party inboxes. Those
  are the properties of an email sender or a spam tool, and are explicitly
  out of scope.
- Dry-run is the default for every firing path. Actually sending a
  request requires an explicit `--send` flag; there is no configuration
  that makes sending the default.
- Firing only ever targets a configured target: an unconfigured or
  arbitrary target is refused before any network call is made. A target
  that looks like a real, publicly routable host (not a reserved domain,
  not localhost, not a private address) is refused even if present in
  configuration, unless the operator explicitly overrides that check for
  their own real endpoint.
- Firing is rate limited by default, so a misconfiguration cannot turn a
  test run into a flood.

If you find a way to use TestingHQ to deliver mail to a real recipient,
defeat a spam filter, or forge sender authentication against a real
domain, that is a bug in this project's threat model, not a feature.
Please report it (see below) rather than using it.

## Threat model

**In scope: TestingHQ used against your own systems.**

Assets we defend:

- Third parties (real mailboxes, real domains, spam-filter reputation of
  uninvolved parties) must never be reachable as a side effect of using
  this tool as intended.
- An operator's own target endpoint must not be flooded or hit
  unexpectedly: sending is opt-in per run (`--send`), scoped to configured
  targets, and rate limited.
- Generated content must be inert outside the test context: addresses and
  payloads must not resolve to, or be mistakable for, real-world
  identities.

Attack scenarios this project defends against:

1. **Accidental real-world delivery.** A user runs Blast expecting a dry
   run, or points it at an unconfigured or mistyped target, and mail-like
   payloads land somewhere real. Mitigated by: dry-run-by-default
   (`evaluate_send`), the configured-target allow-list, and the
   non-reserved-public-host refusal (`require_configured_target`).
2. **Repurposing as a spam or phishing tool.** Someone tries to use
   Blast's payload generator to send believable, real-looking messages to
   real inboxes, or to construct addresses that impersonate real people
   or domains. Mitigated by: the reserved-domain/TLD constraint on all
   generated addresses (`is_synthetic_address`,
   `require_synthetic_content`), and the explicit non-goal of
   implementing sender-authentication bypass.
3. **Runaway or abusive firing volume.** A bug, a bad loop, or a hostile
   input causes Blast to fire far faster or far more than intended,
   overwhelming the operator's own endpoint or, if guardrails were
   bypassed, a real target. Mitigated by: the rate-limit gate contract
   (`RateLimitGate`) that every firing path must go through.
4. **Supply-chain compromise.** A malicious or vulnerable dependency is
   pulled into the project and used to weaken a guardrail or exfiltrate
   data. Mitigated by: Dependabot version updates for pip and
   GitHub Actions dependencies, and a scheduled dependency audit in CI.

**Out of scope:**

- Protecting a target endpoint that the operator does not control or has
  not consented to testing. Getting that consent is the operator's
  responsibility; TestingHQ's guardrails only prevent *this tool* from
  making that mistake automatically, they are not a substitute for
  authorization to test a given endpoint.
- General web-application security of whatever service the operator
  chooses to point Blast at. That service's own security is out of
  scope for this project.
- Denial of service against TestingHQ itself (it is a CLI tool an
  operator runs against their own infrastructure, not a hosted service
  with external users, as of this writing).

## Reporting a vulnerability

If you find a security issue in TestingHQ, including but not limited to:

- a way to make a dry run perform a real network call,
- a way to fire at a target outside the configured allow-list,
- a way to make generated content use a real or non-reserved domain,
- a way to bypass or defeat the rate limiter,

please report it privately rather than opening a public issue. Email
**saifullah4khan@gmail.com** with a description of the issue, the affected
version or commit, and reproduction steps if you have them. Please allow
a reasonable time to respond and address the report before any public
disclosure.

Do not include real third-party personal data, credentials, or evidence
gathered from a system you were not authorized to test.
