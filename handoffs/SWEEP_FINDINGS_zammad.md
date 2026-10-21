# Sweep findings: Zammad 7.2.2

Run on 2026-10-19 against Zammad 7.2.2 (`ghcr.io/zammad/zammad:7.2.2-0000`,
database search, no Elasticsearch) with the stack in
`sweep/compose/zammad.yml`: a POP3 email channel fetching every 30 seconds
from Dovecot, which serves the Maildir the delivery sidecar writes. The run is
published in `examples/runs/zammad/`.

Every command below runs from the repository root against that stack, after:

```
docker compose -f sweep/compose/zammad.yml up -d
bash sweep/setup/zammad.sh
set -a; . sweep/.env.zammad; set +a
```

Three findings are Zammad's. The redeliver ones are what Zammad stored on its
tickets; the loop one is what it sent to the zammad-mail-out Mailpit
(`http://127.0.0.1:3386`).

## 1. A redelivered message opens a second ticket

- Tool: `redeliver` (exit 3, DELIVERY-BUGS), scenarios `duplicate` and
  `slow-retry`.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/zammad.toml --target zammad --send --max-wait 240 --quiet-window 60 --expect-route Users --scenario duplicate --scenario slow-retry
  ```

- Sent: three messages, each delivered twice with the same Message-ID, back
  to back (`duplicate`) and again 120 seconds after the first copy was
  fetched (`slow-retry`).
- Stored: two tickets for every one of the six messages, for example 72 and
  73 for `dup-0-0000`, and 78 and 81 for `rty-0-0000`.
- Why it is Zammad's: Zammad skips a message it has already imported only on
  the IMAP and Microsoft Graph drivers, and only with "keep messages on
  server". `Channel::Driver::BaseEmailInbound::MessageValidator#already_imported?`
  says so ("used by IMAP and MicrosoftGraphInbound only"); the POP3 driver
  imports every message it is handed. A message delivered twice to the
  mailbox, which a mail server does when its acknowledgement of the first
  copy was lost, becomes two tickets with the same Message-ID. Each copy was
  a separate message in the mailbox, so the sidecar did what a mail server
  does.

## 2. A reply that arrives before its original is never threaded

- Tool: `redeliver`, scenario `reply-first`.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/zammad.toml --target zammad --send --max-wait 240 --quiet-window 60 --expect-route Users --scenario reply-first
  ```

- Sent: two replies, each with `In-Reply-To` and `References` naming the
  root, then the root, all within one fetch. The Dates are in thread order.
- Stored: tickets 84 and 85 for the replies and 86 for the root. The
  `references` scenario, sent in order, threads all three onto one ticket,
  so Zammad reads the headers when the parent is already there.
- Why it is Zammad's: Zammad imports a fetch in the order the mailbox lists
  it (`Channel::Driver::Pop3#messages_iterator`), which is arrival order, and
  `Channel::Filter::FollowUpCheck` can only attach a reply to a ticket that
  already holds its parent. When the root arrives it is not matched to the
  tickets whose articles name it. That is filing in arrival order with no
  later re-threading, the case this scenario is for: a provider retrying a
  queued delivery reorders mail like this.
- Not the sidecar: Dovecot lists the Maildir in arrival order, checked by
  hand with six messages. The first live run used a Mailpit for this mailbox,
  whose POP3 server lists newest first; it reversed every fetch, so
  `references` failed and `reply-first` passed. That was the setup's fault and
  is fixed; see the end of this file.

## 3. The auto-reply answers List-Id mail and subject-only out-of-office

- Tool: `loop` (exit 3, LOOPS-DETECTED), with `ticket_policy = "allowed"`.
- Command:

  ```
  testinghq loop fire --config sweep/configs/zammad.toml --target zammad --send --max-wait 240 --quiet-window 60
  ```

- Sent: thirteen kinds of machine mail.
- Sent back by Zammad's shipped "auto reply (on new tickets)" trigger, as
  "Thanks for your inquiry (...)": one to `digest@example.com`, a message
  whose only marker is `List-Id`, and one to `colleague@example.com`, an
  out-of-office whose only marker is its subject. Nothing went to the
  `Auto-Submitted`, `Precedence`, `List-Unsubscribe`, bounce or no-reply
  senders.
- Minimal reproduction, one marker per message from addresses of their own:

  ```
  python - <<'EOF'
  import smtplib
  from email.message import EmailMessage
  for addr, header, value, subject in [
      ("zl-listid@example.com", "List-Id", "<a.lists.example.com>", "header test"),
      ("zl-ooo@example.com", None, None, "Out of Office: away"),
      ("zl-unsub@example.com", "List-Unsubscribe", "<mailto:x@lists.example.com>", "header test"),
  ]:
      m = EmailMessage(); m["From"] = addr; m["To"] = "support@zammad.test"; m["Subject"] = subject
      if header: m[header] = value
      m.set_content("body")
      with smtplib.SMTP("127.0.0.1", 3325) as s: s.send_message(m)
  EOF
  curl -s -G http://127.0.0.1:3386/api/v1/search --data-urlencode 'query=to:zl-listid@example.com'
  ```

  After the next fetch the List-Id and Out of Office senders each have a
  "Thanks for your inquiry" and the List-Unsubscribe sender has none.
- Why it is Zammad's: `Channel::Filter::AutoResponseCheck` holds the
  auto-reply back for `List-Unsubscribe`, `X-Loop`, `Precedence`,
  `Auto-Submitted` and `X-Auto-Response-Suppress`, and not for `List-Id`,
  which RFC 3834 section 2 names with the other list headers as mail a
  responder should not answer. `Channel::Filter::OutOfOfficeCheck` reads only
  headers, so an out-of-office that says so only in its subject is answered.
  The subject case is a heuristic and the weaker half; the List-Id case is the
  stronger. Zammad's own reply is marked well (`Auto-Submitted:
  auto-generated`, `Precedence: bulk`, `X-Loop: yes`), which limits the harm.

## Reported first, then traced to the setup or the recipe

- `references` reported a broken thread (each message on its own ticket,
  created newest first): the Mailpit used as the channel's mailbox lists POP3
  newest first, so Zammad imported every fetch in reverse. The mailbox is now
  Dovecot fed by the delivery sidecar, and PULL_MAIL.md says why.
- `references` still lists sender and subject mismatches for the two replies:
  the ticket search answers per ticket, so a reply reads back with the
  ticket's customer and title. All three are on one ticket, which is the
  scenario's question; the recipe says this limit in its header.
- `ledger` answers UNVERIFIED STRAYS (exit 3) because the recipe cannot list
  tickets by their articles (`enumerate = false`); every message was filed
  exactly once.
- The first live run had no `[loop]` table and no `[steady]` label field;
  both were added, with the mail-out Mailpit as the outbound sink.
