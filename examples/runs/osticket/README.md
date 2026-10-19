# osTicket reference run

Every tool run against osTicket v1.18.4 (the `hlhd/osticket:v1.18.4` image,
on MariaDB 11.4.8) on 2026-10-19, with TestingHQ 1.0.0 plus the changes
listed under `[Unreleased]` in the changelog. The command, from the
repository root, on a fresh stack:

```
SWEEP_KEEP=1 sweep/run.sh osticket
```

The stack is [sweep/compose/osticket.yml](../../../sweep/compose/osticket.yml),
set up by [sweep/setup/osticket.sh](../../../sweep/setup/osticket.sh), and the
config is [sweep/configs/osticket.toml](../../../sweep/configs/osticket.toml):
the inbound target is osTicket's email API, `POST /api/tickets.email` with the
raw message, and the readback is the recipe in
[examples/readback/osticket.toml](../../readback/osticket.toml), osTicket's
MySQL tables through `examples/readback/adapters/osticket_mysql.py`. loop
reads what osTicket sent from a Mailpit sidecar; setup turns on the SMTP
account that points there and osTicket's new-ticket and new-message
auto-responders, and turns off the staff alerts.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.html) | MISMATCHED | 3 | 20 of 20 filed in the Support department with sender, subject and body intact; 4 lost a zero-byte attachment |
| [ledger](ledger.html) | MISPARSED | 3 | 50 sent, 50 filed exactly once, no strays; 6 lost a zero-byte attachment |
| [redeliver](redeliver.html) | DELIVERY-BUGS | 3 | duplicates, slow retries and a References chain thread correctly; replies sent before their original open tickets of their own |
| [loop](loop.html) | LOOPS-DETECTED | 3 | no ticket counts against it (ticket_policy allowed), but 5 "Support Ticket Opened" auto-responses went to list, no-reply and out-of-office senders |
| [steady](steady.html) | STABLE | 0 | 105 variants, every one in Support: osTicket makes no other decision about a message |
| [blast --catalog](blast-catalog.html) | CLEAN | 0 | 26 of 26 edge cases answered 2xx |
| [blast --corpus](blast-corpus.html) | FLAGGED | 3 | 95 of 99 of Zammad's test mails filed; the 4 with no usable sender answered 500 |
| [barrage](barrage.html) | STEADY | 0 | 288 requests at 5/s, p99 160ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.

The five osTicket findings, with what was sent, what was stored and how to
reproduce each, are in
[handoffs/SWEEP_FINDINGS_osticket.md](../../../handoffs/SWEEP_FINDINGS_osticket.md):
zero-byte attachments dropped, auto-responses to machine mail, its own
auto-response's Auto-Submitted and Precedence sent as encoded words, a reply
filed before its original never threaded, and a 500 for a message with no
sender.

steady's constant-label warning is expected here: the department is the only
label osTicket gives a message, so every variant reading `Support` is the
stable answer, not a misconfigured field.

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/osticket/20261019T074051Z/`.
