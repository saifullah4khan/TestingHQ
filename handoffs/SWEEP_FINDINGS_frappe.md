# Sweep findings: Frappe Helpdesk 1.31.0

Run on 2026-10-19 against Frappe Helpdesk v1.31.0 (`ghcr.io/frappe/helpdesk:v1.31.0`,
Frappe Framework version-15, MariaDB 10.6.20) with the stack in
`sweep/compose/frappe.yml`: an Email Account, "Sweep Support", pulling POP3
every minute from Dovecot, which serves the Maildir the delivery sidecar
writes. The site mutes outgoing mail. The run is published in
`examples/runs/frappe/`.

Every command below runs from the repository root against that stack, after:

```
docker compose -f sweep/compose/frappe.yml up -d
bash sweep/setup/frappe.sh
set -a; . sweep/.env.frappe; set +a
```

Two findings are Frappe's, and one behaviour is recorded as an observation.

## 1. One image attachment Pillow cannot read loses the whole email

- Tools: `verify` (exit 3, 1 of 20 never filed) and `ledger` (exit 3, 3 of 50
  never filed): every message lost carried a `.jpg` attachment.
- Command:

  ```
  testinghq verify fire --config sweep/configs/frappe.toml --target frappe --send --max-wait 600 --quiet-window 150 --expect-route "Sweep Support"
  ```

- Sent: `clean-0-0000`, a text and HTML message with `invoice4.jpg`
  (`image/jpeg`, 2048 bytes: the JPEG magic bytes and filler, as TestingHQ's
  generated attachments are, not a decodable picture).
- Stored: no ticket and no Communication. The message was pulled and deleted
  from the mailbox, and an Error Log entry for `EmailAccount.receive` ends
  in `File.save_file` > `strip_exif_data` > `PIL.Image.open` >
  `OSError: Truncated File Read`.
- Why it is Frappe's: Frappe strips EXIF from every image it saves, and when
  Pillow cannot read the image the exception escapes `InboundMail.process`,
  so the whole email, text and all, is dropped instead of the one
  attachment. POP3 has already deleted it, so it is gone from both places and
  the only trace is the Error Log. Corrupt and truncated image attachments
  are ordinary in real mail (a broken client, a size limit somewhere on the
  way); here the image is synthetic, which is the trigger, and the loss of the
  message is the bug.

## 2. A reply that arrives before its original is never threaded

- Tool: `redeliver` (exit 3, DELIVERY-BUGS), scenario `reply-first`. The sweep
  runs redeliver at `--seed 7`, whose thread carries no attachment, so
  finding 1 does not take the original out of every scenario.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/frappe.toml --target frappe --send --seed 7 --max-wait 600 --quiet-window 150 --expect-route "Sweep Support" --scenario reply-first
  ```

- Sent: two replies, each with `In-Reply-To` and `References` naming the
  root, then the root, within one pull. The Dates are in thread order.
- Stored: in the published run the replies opened tickets 0052 and 0053, and
  the root was filed on 0049 (see the observation below); nothing joined
  them. The `references` scenario, delivered in order, threads all three onto
  one ticket.
- Why it is Frappe's: Frappe files a pull in the order the mailbox lists it
  (`receive.py`, `get_new_mails` and `EmailAccount.receive` keep the POP3
  order), and a reply finds its parent only through a Communication that
  already exists. When the root arrives, the tickets whose replies name it are
  not looked for. Mail filed in arrival order with no later re-threading is
  what this scenario is for: a provider retrying a queued delivery reorders
  mail like this. Dovecot lists the mailbox in arrival order, checked by hand.

## Observation: new mail with an open ticket's subject and sender joins it

Not counted as a finding: it is Frappe's documented behaviour, not a parse
error, but it decides what several tools see.

- Sent: the roots of redeliver's four scenarios, distinct Message-IDs, no
  `In-Reply-To` or `References`, the same sender and subject (the scenarios
  share payloads on purpose).
- Stored: all four on ticket 0049, the first scenario's, along with the
  in-order references thread. Frappe appends a new
  message to an open ticket from the same sender with the same subject.
  Two separate requests from one customer under one subject ("Invoice") end up
  on one ticket; and redeliver's `duplicate` and `slow-retry` scenarios pass
  partly because of it, since a second copy lands on the first copy's ticket
  either way.

## Not traced: other receive errors

Frappe's Error Log held 10 `EmailAccount.receive` errors after the published
run. Four are the lost messages of finding 1 (one in verify, three in
ledger). At least one of the others ends `... is not a valid Email Address`,
most likely from Zammad's test mail sent by `blast --corpus`, whose messages
the readback tools do not look for; each such error also loses its message.
They were not traced to their files before the stack was taken down, so
they are not counted here; rerun the sweep and read `tabError Log` to follow
them up.

## Reported first, then traced to the setup or the config

Fixed on this branch; the published run is after the fixes.

- The stack's Mailpit lists POP3 newest first, which would have reversed every
  pull, the fault found first against Zammad. Inbound is now the delivery
  sidecar and Dovecot.
- `sweep/.env.frappe` held `FRAPPE_API_AUTH=token <key>:<secret>` unquoted, so
  loading it with `set -a; .` failed. Quoted now.
- loop had no `[loop]` table and counted every ticket for machine mail as a
  finding. Frappe Helpdesk drops bounces, `X-Auto-Generated` and
  `Auto-Submitted` other than `auto-replied` on purpose
  (`helpdesk/overrides/email_account.py`, `auto_generated_reason`), and lets
  the rest in, so the config sets `ticket_policy = "allowed"`. Outgoing mail
  is muted, so the auto-reply check reads NOT CHECKED.
- At seed 0 redeliver lost every scenario's original to finding 1, which hid
  the threading answers; the sweep now runs it at seed 7.
