# Sweep findings: osTicket 1.18.4

Run on 2026-10-19 against osTicket v1.18.4 (`hlhd/osticket:v1.18.4`,
`THIS_VERSION` v1.18.4, git 8d38b06) on MariaDB 11.4.8, with the stack in
`sweep/compose/osticket.yml`. The run is published in `examples/runs/osticket/`.

Every command below runs from the repository root against that stack, after:

```
docker compose -f sweep/compose/osticket.yml up -d
bash sweep/setup/osticket.sh
set -a; . sweep/.env.osticket; set +a
export PYTHONPATH=.
KEY="$OSTICKET_API_KEY"
```

The curl reproductions post a raw message to osTicket's email API, the same
endpoint and body TestingHQ's `mime` target sends.

Five findings are osTicket's. Each section says why it is osTicket's and not
the recipe's. Things the run first reported that turned out to be the recipe's,
the setup's or TestingHQ's own are listed at the end with what was fixed.

## 1. A zero-byte attachment is dropped

- Tools: `verify` (exit 3, 4 of 20 messages) and `ledger` (exit 3, 6 of 50).
- Command:

  ```
  testinghq verify fire --config sweep/configs/osticket.toml --target osticket --send --max-wait 120 --quiet-window 10 --expect-route Support
  ```

- Sent: a multipart message with an attachment of zero bytes, for example
  `clean-0-0003` with `scan7138.csv` (`text/csv`, 0 bytes). Every attachment
  with content in the same run was stored.
- Stored: no attachment on the message entry. `ost_file` has a row for the file
  with `size` 0 that no `ost_attachment` points at, and the ticket's thread has
  a system note, "File Import Error: Unable to save attachment - scan7138.csv".
- Minimal reproduction, one empty and one non-empty attachment:

  ```
  printf 'From: Zero Byte <zero@example.com>\r\nTo: support@helpdesk.test\r\nSubject: zero-byte attachment\r\nMessage-ID: <zero-byte-1@example.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nSee the attached file.\r\n--b\r\nContent-Type: text/csv\r\nContent-Disposition: attachment; filename="empty.csv"\r\nContent-Transfer-Encoding: base64\r\n\r\n\r\n--b\r\nContent-Type: text/csv\r\nContent-Disposition: attachment; filename="full.csv"\r\nContent-Transfer-Encoding: base64\r\n\r\nYSxiCg==\r\n--b--\r\n' > zero.eml
  curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: message/rfc822' --data-binary @zero.eml http://127.0.0.1:3200/api/tickets.email
  ```

  `full.csv` is attached to the entry; `empty.csv` is an orphan `ost_file` row
  of size 0 and a "File Import Error" note.
- Why it is osTicket's: an empty file is a valid MIME part and the message
  arrives intact; osTicket's own log says it could not save it. In
  `include/class.file.php`, `AttachmentFile::create` saves the `ost_file` row
  first, then writes the data, and treats a backend `write('')` that wrote
  nothing as a failure, so it returns false and leaves the row behind;
  `ThreadEntry::normalizeFileInfo` then logs the import error and skips the
  file. The recipe reads `ost_attachment` joined to `ost_file`, which is where
  osTicket records every attachment it keeps.

## 2. Auto-responses go to list mail, no-reply senders and an out-of-office

- Tool: `loop` (exit 3, LOOPS-DETECTED), with `ticket_policy = "allowed"`.
- Command:

  ```
  testinghq loop fire --config sweep/configs/osticket.toml --target osticket --send --max-wait 120 --quiet-window 10
  ```

- Sent: thirteen kinds of machine mail. osTicket held back its auto-response
  for `Auto-Submitted: auto-replied` and `auto-generated`, for
  `Precedence: bulk`, `list` and `junk`, and for a bounce with
  `Return-Path: <>`.
- Stored and sent: a "Support Ticket Opened [#n]" auto-response to each of
  these, all five in `loop.json` under `outbound_unattributed`:
  - `digest@example.com`, a message with `List-Id`;
  - `users@example.com`, a message with `List-Unsubscribe`;
  - `noreply@billing.example.com` and `no-reply@billing.example.com`;
  - `colleague@example.com`, an out-of-office whose only marker is its
    subject, "Out of office: ...".
- Minimal reproduction:

  ```
  printf 'From: Weekly Digest <digest-repro@example.com>\r\nTo: support@helpdesk.test\r\nSubject: This week on the list\r\nMessage-ID: <list-repro-1@example.com>\r\nList-Id: <users.lists.example.com>\r\nList-Unsubscribe: <mailto:leave@lists.example.com>\r\n\r\nThe weekly digest.\r\n' > list.eml
  curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: message/rfc822' --data-binary @list.eml http://127.0.0.1:3200/api/tickets.email
  curl -s -G http://127.0.0.1:3286/api/v1/search --data-urlencode 'query=to:digest-repro@example.com'
  ```

  The mail-out Mailpit holds "Support Ticket Opened" to the digest address.
  Sent one header at a time from addresses of their own, `List-Id` alone and
  `List-Unsubscribe` alone each get an auto-response, and `Precedence: list`
  alone and `Precedence: bulk` alone get none. The same with
  `From: noreply-repro@billing.example.com`, or with
  `Subject: Out of Office: back Monday` and no other marker, gets one too; with
  `Auto-Submitted: auto-replied` added it gets none.
- Why it is osTicket's: RFC 3834 section 2 says a responder should not answer
  mail from a list (`List-Id`, `List-*` headers) and should not answer
  addresses that cannot take a reply. osTicket already suppresses its reply
  for `Auto-Submitted` and `Precedence` (`Ticket::create` skips the
  autorespond for the `bounce` and `auto-reply` mail flags), so the check
  exists and these markers are not in it. The no-reply and subject-only
  out-of-office cases are heuristics rather than RFC rules and are the weaker
  half of this finding; the `List-Id` and `List-Unsubscribe` cases are the
  strong half. The config's sink is the helpdesk's own outbound SMTP, and
  setup turns the staff alerts off, so every message counted went to the
  sender of the machine mail.
- Why loop could not attribute them: osTicket's auto-response does carry
  `In-Reply-To` and `References` for the message it answers, but Mailpit's
  search API, which the sink reads, returns no In-Reply-To field, so loop
  lists them as unattributed. The recipients above are the attribution.

## 3. osTicket's own auto-response encodes Auto-Submitted and Precedence

- Found while reading the auto-responses for finding 2. No TestingHQ tool
  reports it directly: loop checks what osTicket answers, not the headers of
  its answer.
- Reproduction: after the `list.eml` post above,

  ```
  ID=$(curl -s -G http://127.0.0.1:3286/api/v1/search --data-urlencode 'query=to:digest-repro@example.com' | python -c "import sys, json; print(json.load(sys.stdin)['messages'][0]['ID'])")
  curl -s http://127.0.0.1:3286/api/v1/message/$ID/raw | grep -iE '^(auto-submitted|precedence):'
  ```

- Sent by osTicket:

  ```
  Precedence: =?UTF-8?Q?auto=5Freply?=
  Auto-Submitted: =?UTF-8?Q?auto-replied?=
  ```

- Why it is osTicket's, and why it matters: `Auto-Submitted` (RFC 3834) and
  `Precedence` are structured fields whose values are tokens, and RFC 2047
  section 5 allows encoded words only in unstructured text and phrases. A
  receiving autoresponder that looks for `auto-replied` sees an encoded word
  and does not recognise osTicket's reply as automatic, which is how two
  autoresponders end up answering each other. The bytes are as Mailpit
  received them from osTicket's SMTP transaction.

## 4. A reply that arrives before its original is never threaded

- Tool: `redeliver` (exit 3, DELIVERY-BUGS), scenario `reply-first`.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/osticket.toml --target osticket --send --max-wait 120 --quiet-window 10 --expect-route Support
  ```

- Sent: two replies, each with `In-Reply-To` and `References` naming the
  root's Message-ID and a `Re:` subject, then the root. The Dates are in thread
  order, root first.
- Stored: each reply opened its own ticket (317801 and 631964 in the published
  run) and the root opened a third (258987). Nothing was merged once the root
  arrived. The `references` scenario, delivered in order, threads all three
  onto one ticket, so osTicket reads the headers when the parent exists.
- Why it is osTicket's: the API files each message synchronously on arrival,
  so when a reply comes in its parent does not exist yet and osTicket opens a
  ticket; when the root then arrives it never looks for tickets whose
  `In-Reply-To` names it. That is filing in arrival order with no later
  re-threading, the behaviour this scenario exists to catch: a provider
  retrying a queued delivery reorders mail like this. It is a design limit
  more than a parse bug, and it is the same class of finding expected from
  Frappe Helpdesk.

## 5. A message with no usable sender answers 500

- Tool: `blast --corpus` (exit 3, FLAGGED), 4 of Zammad's 99 test mails:
  `mail016.box`, `mail019.box`, `mail053.box`, `mail054.box`.
- Command:

  ```
  testinghq blast fire --config sweep/configs/osticket.toml --target osticket --send --corpus sweep/.cache/zammad/test/data/mail --count 100
  ```

- Sent: messages whose source has no From address. `mail016` and `mail054`
  have no From header at all (`mail054`'s only From is inside an attached
  message), `mail019` has `From: "..." <>` and `mail053` has
  `From: MAILER-DAEMON (Mail Delivery System)` with no address. TestingHQ sends
  them with an empty `From:`.
- Answered: HTTP 500, body
  `Unable to create new ticket :user` / `Incomplete client information`.
- Minimal reproduction:

  ```
  printf 'To: support@helpdesk.test\r\nReply-To: someone@example.com\r\nSubject: no sender\r\nMessage-ID: <no-sender-1@example.com>\r\n\r\nThis message has no From header.\r\n' > nofrom.eml
  curl -s -w '\nHTTP %{http_code}\n' -X POST -H "X-API-Key: $KEY" -H 'Content-Type: message/rfc822' --data-binary @nofrom.eml http://127.0.0.1:3200/api/tickets.email
  ```

- Why it is osTicket's: refusing a message with no sender is reasonable; the
  status is not. `TicketApiController::createTicket` in
  `include/api.tickets.php` ends with `$this->exerr($errors['errno'] ?: 500, ...)`,
  so a validation error, which is the client's problem and permanent, is
  reported as a server error. A mail-to-HTTP bridge treats a 5xx as temporary
  and retries it, so the same unfileable message is resent until the bridge
  gives up. A 4xx (400 or 422) would be correct. The `Reply-To` that two of
  these carry is a sender osTicket could also have used.

## Reported first, then traced to the recipe, the setup or TestingHQ

None of these is osTicket's. Each was fixed on this branch, and the published
run is after the fixes.

- `blast --catalog` crashed (exit 1) on the header-injection case: TestingHQ's
  `mime` format could not build a message with CRLF in the subject. Fixed in
  `testinghq/core/formats.py`.
- `redeliver`'s `references` scenario reported the replies' subject as the
  original's: osTicket keeps no title on a threaded reply, and the adapter fell
  back to the ticket's subject. Fixed in the adapter, which reads the reply's
  stored Subject header.
- `blast --corpus` sent `mail044.box` (`From: "Clement.Si"
  <Claudia.Shu@yahoo.com.>`) with an empty From and osTicket answered 500:
  TestingHQ's corpus rewrite dropped an address the stdlib parser misread.
  Fixed in `testinghq/blast/mailcorpus.py`; it is filed now.
- `loop` reported eleven "outbound message emitted for machine mail" findings:
  they were staff "New Ticket Alert" mails to `admin@helpdesk.test`, which
  quote the tag. Setup now turns staff alerts off.
- `loop` first had no outbound sink and could not check replies, and `steady`
  was refused for want of a label field: config gaps, now filled.
- The stack did not start: `tiredofit/osticket` is gone from Docker Hub, the
  replacement image's mariadb client needs TLS that MariaDB 10.11 does not
  serve, and the setup script's `MSYS_NO_PATHCONV` broke curl on Windows.
