# FreeScout reference run

Every tool run against FreeScout 1.8.248 (the `nfrastack/freescout:2.2.19`
image, on MariaDB 11.8.9, with Dovecot 2.3.21.1 serving its mailbox) on
2026-10-19, with TestingHQ 1.0.0 plus the changes listed under `[Unreleased]`
in the changelog. The command, from the repository root, on a fresh stack:

```
SWEEP_KEEP=1 sweep/run.sh freescout
```

The stack is [sweep/compose/freescout.yml](../../../sweep/compose/freescout.yml),
set up by [sweep/setup/freescout.sh](../../../sweep/setup/freescout.sh), and
the config is [sweep/configs/freescout.toml](../../../sweep/configs/freescout.toml).
FreeScout pulls mail: TestingHQ sends over SMTP to a delivery sidecar, which
writes a Maildir that Dovecot serves over IMAP, and the mailbox fetches it
every minute. The readback is the recipe in
[examples/readback/freescout.toml](../../readback/freescout.toml), FreeScout's
MySQL tables through `examples/readback/adapters/freescout_mysql.py`; loop
reads what FreeScout sent from the sidecar's outbound record.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.txt) | MISMATCHED | 3 | 20 of 20 filed in the sweep mailbox with sender, subject and body intact; 4 lost a zero-byte attachment |
| [ledger](ledger.txt) | MISPARSED | 3 | 50 sent, 50 filed exactly once, no strays; 6 lost a zero-byte attachment |
| [redeliver](redeliver.txt) | DELIVERY-SAFE | 0 | duplicates and slow retries recognised; a References chain and replies sent before their original all threaded |
| [loop](loop.txt) | LOOPS-DETECTED | 3 | 13 conversations for machine mail, reported under ticket_policy allowed; 6 auto-replies to machine mail, one of them to a bounce |
| [steady](steady.txt) | STABLE | 0 | 105 variants, every one in the sweep mailbox: FreeScout makes no other decision about a message |
| [blast --catalog](blast-catalog.txt) | CLEAN | 0 | 26 of 26 edge cases accepted over SMTP |
| [blast --corpus](blast-corpus.txt) | CLEAN | 0 | 99 of Zammad's test mails accepted over SMTP |
| [barrage](barrage.txt) | STEADY | 0 | 288 messages at 5/s over SMTP, p99 17ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.
`testinghq report <artifact>.json --html <artifact>.html` renders any of them
as a self-contained HTML report.

The three FreeScout findings, with what was sent, what was stored and how to
reproduce each, are in
[handoffs/SWEEP_FINDINGS_freescout.md](../../../handoffs/SWEEP_FINDINGS_freescout.md):
zero-byte attachments dropped, an auto-reply to a bounce FreeScout itself
flagged as one, and auto-replies to list and no-reply mail.

redeliver's reply-first scenario passes here because FreeScout files each fetch
in Date order and the replies arrived in the same fetch as their original. The
blast and barrage verdicts are about the SMTP hand-off only; whether each
message became a conversation is what the readback tools check.

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/freescout/20261019T080856Z/`.
