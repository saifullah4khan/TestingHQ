# Sweep findings: FreeScout 1.8.248

Run on 2026-10-19 against FreeScout 1.8.248 (`nfrastack/freescout:2.2.19`,
`config/app.php` version 1.8.248) on MariaDB 11.8.9, with the stack in
`sweep/compose/freescout.yml`: a mailbox fetching over IMAP every minute from
Dovecot, which serves the Maildir the delivery sidecar writes, and an outbound
SMTP that goes back to the sidecar, which records it in
`sweep/.state/freescout/outbound.jsonl`. The run is published in
`examples/runs/freescout/`.

Every command below runs from the repository root against that stack, after:

```
docker compose -f sweep/compose/freescout.yml up -d
bash sweep/setup/freescout.sh
set -a; . sweep/.env.freescout; set +a
export PYTHONPATH=.
```

Three findings are FreeScout's. redeliver was DELIVERY-SAFE: duplicates,
slow retries, a References chain and replies sent before their original all
landed on the right conversation, because FreeScout files each fetch in Date
order and the replies arrived in the same fetch as their original. By the
rule for this scenario that is not a finding.

## 1. A zero-byte attachment is dropped without a word

- Tools: `verify` (exit 3, 4 of 20 messages) and `ledger` (exit 3, 6 of 50).
- Command:

  ```
  testinghq verify fire --config sweep/configs/freescout.toml --target freescout --send --max-wait 300 --quiet-window 90 --expect-route support@freescout.test
  ```

- Sent: a multipart message with an attachment of zero bytes, for example
  `clean-0-0003` with `scan7138.csv` (`text/csv`, 0 bytes). Every attachment
  with content in the same run was stored.
- Stored: no attachment on the thread, and nothing in the log or on the
  conversation to say one was dropped.
- Why it is FreeScout's: `App\Attachment::create` begins with
  `if (!$content && !$uploaded_file) { return false; }`, and in PHP an empty
  string is false, so a valid empty file is discarded at the first line. The
  adapter reads FreeScout's `attachments` table, where every attachment it
  keeps is recorded. osTicket drops the same files, for its own reason.

## 2. The auto-reply answers a bounce FreeScout has flagged as a bounce

- Tool: `loop` (exit 3, LOOPS-DETECTED), case `bounce-dsn`.
- Command:

  ```
  testinghq loop fire --config sweep/configs/freescout.toml --target freescout --send --max-wait 300 --quiet-window 90
  ```

- Sent: a delivery status notification: `From: MAILER-DAEMON@example.com`,
  `Return-Path: <>`, `Content-Type: multipart/report;
  report-type="delivery-status"`.
- Stored and sent: FreeScout stored the thread with
  `send_status_data = {"is_bounce": true}`, so its bounce detection worked,
  and its mailbox auto-reply, "We received your message", went to
  `mailer-daemon@example.com` with `In-Reply-To` naming the bounce. It was
  sent with a null envelope sender, so the mailbox shows `Return-Path: <>` as
  a mail server writes it. A bounce from Zammad's own test mail in the same
  run was answered the same way.
- Minimal reproduction, with a null envelope sender as a real DSN has:

  ```
  python - <<'EOF'
  import smtplib
  host = "mx-repro.example.com"
  msg = (f"From: MAILER-DAEMON@{host}\r\nTo: support@freescout.test\r\n"
         "Subject: Undelivered Mail Returned to Sender\r\n"
         f"Message-ID: <dsn-repro@{host}>\r\nMIME-Version: 1.0\r\n"
         'Content-Type: multipart/report; report-type=delivery-status; boundary="r"\r\n\r\n'
         "--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
         "--r\r\nContent-Type: message/delivery-status\r\n\r\n"
         f"Reporting-MTA: dns; {host}\r\n\r\nFinal-Recipient: rfc822; x@example.com\r\n"
         "Action: failed\r\nStatus: 5.1.1\r\n--r--\r\n").encode()
  with smtplib.SMTP("127.0.0.1", 3425) as s:
      s.sendmail("", ["support@freescout.test"], msg)
  EOF
  ```

  After the next fetch the thread has `is_bounce` true and
  `sweep/.state/freescout/outbound.jsonl` holds an auto-reply to
  `mailer-daemon@mx-repro.example.com`. The same DSN with
  `Auto-Submitted: auto-replied` added gets none.
- Why it is FreeScout's: `App\Listeners\SendAutoReply` returns early when
  `$thread->isBounce()`, and FreeScout's own fetch marks this thread a bounce,
  yet the reply is sent; the bounce flag evidently is not set by the time the
  listener asks. An auto-reply to a mailer daemon is the classic mail loop:
  RFC 3834 section 2 says a responder must not answer a message with a null
  Return-Path. FreeScout's own comment in the listener names the risk and
  relies on a per-customer rate limit to cap it.

## 3. The auto-reply answers list mail and no-reply senders

- Tool: `loop`, the same run.
- Sent and answered: "We received your message" went to
  `digest@example.com` (`List-Id`), `users@example.com` (`List-Unsubscribe`),
  `noreply@billing.example.com`, `no-reply@billing.example.com` and
  `colleague@example.com` (an out-of-office marked only by its subject). It was
  held back for `Auto-Submitted: auto-replied` and `auto-generated` and for
  `Precedence: bulk`, `list` and `junk`. Each reply's `In-Reply-To` names the
  message it answers, in `loop.json` and the outbound sink.
- Why it is FreeScout's: `App\Misc\Mail::isAutoResponder` checks only
  `X-Autoreply`, `X-Autorespond`, `X-Autoresponder`, `Auto-Submitted`,
  `Delivered-To: autoresponder` and `Precedence`/`X-Precedence`. RFC 3834
  section 2 names `List-Id` and the other `List-*` headers as mail a responder
  should not answer. The no-reply and subject-only cases are heuristics and
  the weaker half; the list headers are the stronger.

## Reported first, then traced to TestingHQ or the setup

Fixed on this branch; the published run is after the fixes.

- Every message with attachments was stored with the MIME headers and
  undecoded quoted-printable as its body and a stray attachment (`66617ed4`,
  then `61cdb76b` holding `boundary="`). TestingHQ named nested boundaries
  `<boundary>-1`, and FreeScout's parser splits on the boundary string
  wherever it appears. TestingHQ now derives nested boundaries so none is a
  prefix of another or inside one; the same message with a distinct inner
  boundary was parsed correctly before the fix.
- Setup stopped silently after "configuring mailbox": Git Bash rewrote
  `docker compose exec -w /www/html` into a Windows path. Fixed in the
  setup script.
- loop's bounce went out with `MAIL FROM:<MAILER-DAEMON@example.com>`, so
  the sidecar wrote that as the Return-Path above the message's own `<>`.
  TestingHQ now sends it with a null reverse-path, and the published run's
  bounce, sent that way, is still answered.
- steady had no label field and verify no expected route; both added.
