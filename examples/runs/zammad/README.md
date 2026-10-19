# Zammad reference run

Every tool run against Zammad 7.2.2 (the `ghcr.io/zammad/zammad:7.2.2-0000`
image, with PostgreSQL 17.11, Redis 8.10.2, Memcached 1.6.45 and no
Elasticsearch) on 2026-10-19, with TestingHQ 1.0.0 plus the changes listed
under `[Unreleased]` in the changelog. The command, from the repository root,
on a fresh stack:

```
SWEEP_KEEP=1 sweep/run.sh zammad
```

The stack is [sweep/compose/zammad.yml](../../../sweep/compose/zammad.yml),
set up by [sweep/setup/zammad.sh](../../../sweep/setup/zammad.sh) through
Zammad's auto wizard, and the config is
[sweep/configs/zammad.toml](../../../sweep/configs/zammad.toml). Zammad pulls
mail: TestingHQ sends over SMTP to a delivery sidecar, which writes a Maildir
that Dovecot serves over POP3, and the email channel fetches it every 30
seconds. The readback is the recipe in
[examples/readback/zammad.toml](../../readback/zammad.toml), Zammad's ticket
search; loop reads Zammad's outbound mail from a Mailpit.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.html) | VERIFIED | 0 | 20 of 20 filed in the Users group with sender and subject intact; body and attachments NOT CHECKED (the search does not return articles) |
| [ledger](ledger.html) | UNVERIFIED STRAYS | 3 | 50 sent, 50 filed exactly once; strays not searched, since the search cannot list every ticket by its articles |
| [redeliver](redeliver.html) | DELIVERY-BUGS | 3 | a redelivered Message-ID opens a second ticket; replies that arrive before their original are never joined to it |
| [loop](loop.html) | LOOPS-DETECTED | 3 | 13 tickets for machine mail, reported under ticket_policy allowed; Zammad's auto-reply went to a List-Id digest and a subject-only out-of-office |
| [steady](steady.html) | STABLE | 0 | 105 variants, every one in Users: Zammad makes no other decision about a message |
| [blast --catalog](blast-catalog.html) | CLEAN | 0 | 26 of 26 edge cases accepted over SMTP |
| [blast --corpus](blast-corpus.html) | CLEAN | 0 | 99 of Zammad's own test mails accepted over SMTP |
| [barrage](barrage.html) | STEADY | 0 | 288 messages at 5/s over SMTP, p99 20ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.

The three Zammad findings, with what was sent, what was stored and how to
reproduce each, are in
[handoffs/SWEEP_FINDINGS_zammad.md](../../../handoffs/SWEEP_FINDINGS_zammad.md).

redeliver's `references` scenario lists sender and subject lines although
Zammad threaded all three messages onto one ticket: the search answers per
ticket, so a reply reads back with the ticket's customer and title. That is
the recipe's limit, written into it, not a Zammad finding. The blast and
barrage verdicts are about the SMTP hand-off only; whether each message became
a ticket is what the readback tools check.

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/zammad/20261019T055956Z/`.
