# Frappe Helpdesk reference run

Every tool run against Frappe Helpdesk 1.31.0 (the
`ghcr.io/frappe/helpdesk:v1.31.0` image, Frappe Framework version-15, on
MariaDB 10.6.20, with Dovecot 2.3.21.1 serving its mailbox) on 2026-10-19,
with TestingHQ 1.0.0 plus the changes listed under `[Unreleased]` in the
changelog. The command, from the repository root, on a fresh stack:

```
SWEEP_KEEP=1 sweep/run.sh frappe
```

The stack is [sweep/compose/frappe.yml](../../../sweep/compose/frappe.yml),
set up by [sweep/setup/frappe.sh](../../../sweep/setup/frappe.sh), and the
config is [sweep/configs/frappe.toml](../../../sweep/configs/frappe.toml).
Frappe pulls mail: TestingHQ sends over SMTP to a delivery sidecar, which
writes a Maildir that Dovecot serves over POP3, and the Email Account "Sweep
Support" pulls it every minute. The readback is the recipe in
[examples/readback/frappe-helpdesk.toml](../../readback/frappe-helpdesk.toml),
each Communication through Frappe's REST API. The site mutes outgoing mail.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.html) | MISMATCHED | 3 | 19 of 20 filed through Sweep Support with sender, subject and body intact; the one with a JPEG was never filed |
| [ledger](ledger.html) | UNACCOUNTED | 3 | 50 sent, 47 filed exactly once, no strays; the 3 with a JPEG were never filed |
| [redeliver](redeliver.html) | DELIVERY-BUGS | 3 | at seed 7: duplicates, slow retries and a References chain land on one ticket; replies sent before their original open tickets of their own |
| [loop](loop.html) | LOOP-SAFE (auto-reply NOT CHECKED) | 0 | 11 tickets for machine mail, reported under ticket_policy allowed; bounces and auto-generated mail dropped; outgoing mail is muted |
| [steady](steady.html) | STABLE | 0 | 105 variants, every one through Sweep Support: Frappe makes no other decision about a message |
| [blast --catalog](blast-catalog.html) | CLEAN | 0 | 26 of 26 edge cases accepted over SMTP |
| [blast --corpus](blast-corpus.html) | CLEAN | 0 | 99 of Zammad's test mails accepted over SMTP |
| [barrage](barrage.html) | STEADY | 0 | 288 messages at 5/s over SMTP, p99 17ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.

The three Frappe findings and one observation, with what was sent, what was
stored and how to reproduce each, are in
[handoffs/SWEEP_FINDINGS_frappe.md](../../../handoffs/SWEEP_FINDINGS_frappe.md):
an attachment Pillow or pypdf cannot read loses the whole email, a reply that
arrives before its original is never threaded, a sender with a quoted local
part loses the whole email (traced from the corpus run's error log), and new
mail with an open ticket's subject and sender joins that ticket.

redeliver runs at `--seed 7` (`SWEEP_ARGS_REDELIVER` in
`sweep/profiles/frappe.env`): at seed 0 every scenario's original carries a
JPEG and is lost, which verify and ledger already report, and the threading
questions go unanswered. The blast and barrage verdicts are about the SMTP
hand-off only; whether each message became a ticket is what the readback
tools check.

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/frappe/20261019T082602Z/`.
