# Chatwoot reference run

Every tool run against Chatwoot v4.18.0 (the `chatwoot/chatwoot:v4.18.0-ce`
image, on pgvector 0.8.0 with PostgreSQL 16 and Redis 7.4.1) on 2026-10-19,
with TestingHQ 1.0.0 plus the changes listed under `[Unreleased]` in the
changelog. The command, from the repository root, on a fresh stack:

```
SWEEP_KEEP=1 sweep/run.sh chatwoot
```

The stack is [sweep/compose/chatwoot.yml](../../../sweep/compose/chatwoot.yml),
set up by [sweep/setup/chatwoot.sh](../../../sweep/setup/chatwoot.sh), and the
config is [sweep/configs/chatwoot.toml](../../../sweep/configs/chatwoot.toml):
the inbound target is Action Mailbox's SendGrid ingress in the `sendgrid-raw`
format behind Basic auth, and the readback is Chatwoot's message search, the
lookup in [examples/readback/chatwoot.toml](../../readback/chatwoot.toml), one
record per message with the conversation it landed on as its ticket.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.html) | VERIFIED | 0 | 20 of 20 filed in inbox 1 with sender, subject and body intact; attachments NOT CHECKED (a message's attachments carry no file names) |
| [ledger](ledger.html) | BALANCED | 0 | 50 sent, 50 filed exactly once, no strays among the tagged messages |
| [redeliver](redeliver.html) | DELIVERY-BUGS | 3 | duplicates and slow retries are recognised; an in-order thread sent quickly is split across conversations, and a reply is recorded as sent by the conversation's contact |
| [loop](loop.html) | LOOP-SAFE (auto-reply NOT CHECKED) | 0 | 11 conversations for machine mail, reported under ticket_policy allowed; Chatwoot sends no automatic email and the stack has no outbound sink |
| [steady](steady.html) | STABLE | 0 | 105 variants, every one in inbox 1: Chatwoot makes no other decision about a message |
| [blast --catalog](blast-catalog.html) | CLEAN | 0 | 26 of 26 edge cases answered 2xx |
| [blast --corpus](blast-corpus.html) | CLEAN | 0 | 99 of Zammad's test mails, every one answered 2xx |
| [barrage](barrage.html) | STEADY | 0 | 288 requests at 5/s, p99 26ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.

The three Chatwoot findings, with what was sent, what was stored and how to
reproduce each, are in
[handoffs/SWEEP_FINDINGS_chatwoot.md](../../../handoffs/SWEEP_FINDINGS_chatwoot.md).

steady's constant-label warning is expected here: the inbox is the only label
Chatwoot gives a message, so every variant reading `1` is the stable answer.

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/chatwoot/20261019T074646Z/`.
