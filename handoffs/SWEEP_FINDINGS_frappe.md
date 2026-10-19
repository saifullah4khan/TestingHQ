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
The receive errors the published run left untraced were traced afterwards on
a fresh stack; they are in the section after the observation.

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

## Traced afterwards: the corpus run's receive errors

Frappe's Error Log held 10 `EmailAccount.receive` errors after the published
run. Four are the lost messages of finding 1 (one in verify, three in
ledger). The rest come from Zammad's test mail sent by `blast --corpus`, whose
messages the readback tools do not look for, so the published run does not
count them. They were traced on 2026-10-19 on a fresh stack, outside the
published run:

```
docker compose -f sweep/compose/frappe.yml up -d
bash sweep/setup/frappe.sh
set -a; . sweep/.env.frappe; set +a
testinghq blast fire --config sweep/configs/frappe.toml --target frappe --send \
  --corpus sweep/.cache/zammad/test/data/mail --count 100
```

followed by reading `tabError Log` and `tabCommunication` with
`bench --site helpdesk.localhost execute frappe.db.sql` once the mailbox had
drained.

What happened to the 100 files: `mail068.box` has no header and is never
sent, so 99 went out and 90 were filed. Four were dropped on purpose by
`auto_generated_reason` (`mail019.box`, null Return-Path; `mail053.box`, a
delivery report; `mail089.box` and `mail090.box`, `Auto-Submitted:
auto-generated`). The other five each left one `EmailAccount.receive` error
and are lost: pulled, deleted from the mailbox by POP3, no ticket and no
Communication. The errors carry no tag or Message-ID, so they were matched to
their files by the address in the message, by the attachment, and by pull
order (Frappe files a pull in arrival order). Both causes are Frappe's.

### A. A sender address Frappe will not accept loses the whole email

- Sent, with the error each left:

  | File | From as sent | Error ends |
  | --- | --- | --- |
  | `mail071.box` | `your secret past <"power quadrant system"@example.com>` | `power quadrant system@example.com is not a valid Email Address` |
  | `mail076.box` | `"Millions Lottery Spain transfer"@example.com` | `millions lottery spain transfer@example.com is not a valid Email Address` |
  | `mail087.box` | `"Test test"@example.com` | `test test@example.com is not a valid Email Address` |

  The traceback is the same for all three: `InboundMail.process` >
  `_create_reference_document` > `HD Ticket.insert` >
  `_validate_data_fields` > `validate_email_address(throw=True)` >
  `InvalidEmailAddressError`.
- What Frappe does: `InboundMail.decode_email` replaces every `"` in From and
  Reply-To with a space, so a quoted local part becomes a bare one with a
  space in it, which `validate_email_address` then rejects when the new
  ticket's `raised_by` (an Email field) is validated. Even with the quotes
  kept, `EMAIL_MATCH_PATTERN` has no quoted-string form. The exception escapes
  `process`, `receive` rolls back, and the message is gone.
- TestingHQ's rewrite is not the cause. The originals are `<power quadrant
  system@example.com>` and `Test test@example.com` (spaces, unquoted) and
  `"Millions Lottery Spain transfer"@example.com>`; Python's parser reads them
  as quoted local parts, which is what TestingHQ sends. The reproduction below
  sends both forms by hand, and Frappe loses both. Zammad's own expected
  parse (the `.yml` beside each file) keeps a sender for all three.
- Why it is Frappe's: a quoted local part is valid RFC 5322, and a malformed
  sender is ordinary in spam and in mail from broken clients. Either way the
  sender is the one field it cannot store, not a reason to drop the text.
  Frappe already does better when From is missing altogether: it logs `Email
  missing From header` and files the ticket with no sender (`mail016.box` and
  `mail054.box` in this run).

### B. A `.pdf` attachment pypdf cannot read loses the whole email

- Sent: `mail028.box` (`Znuny GmbH .pdf`, 7647 bytes that do not start with
  `%PDF`, beside a `Lebenslauf.pdf` that does) and `mail094.box` (a single-part
  `application/pdf` message whose content is the 14 bytes `you can see me`).
  TestingHQ sends binary attachments as they are; the bytes sent match the
  originals' (sha256 checked).
- What Frappe does: `save_attachments_in_doc` > `File.save` >
  `check_content` > `pdf_contains_js` > `PdfReader` >
  `PdfStreamError: Stream has ended unexpectedly`. As in finding 1, the
  exception escapes `InboundMail.process` and the whole email is dropped
  after POP3 has deleted it.
- Why it is Frappe's: the same fault as finding 1 with pypdf in place of
  Pillow. A check on one attachment's content, here the scan for JavaScript in
  a PDF, takes the message down with it when the parser raises. Zammad's
  expected data keeps both attachments.

### Minimal reproduction

Six messages, sent raw to the deliver sidecar so no client normalises the
headers. Every address is on a reserved domain.

```python
import smtplib
H = "To: support@frappe.test\r\nSubject: repro {t}\r\nMessage-ID: <{t}@repro.example.test>\r\nMIME-Version: 1.0\r\n"
def plain(t, frm):
    return f"From: {frm}\r\n" + H.format(t=t) + f"Content-Type: text/plain\r\n\r\nBody {t}\r\n"
def att(t, name, ctype, b64):
    return (f"From: <att-{t}@repro.example.test>\r\n" + H.format(t=t)
            + 'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            + f"--b\r\nContent-Type: text/plain\r\n\r\nBody {t}\r\n"
            + f'--b\r\nContent-Type: {ctype}\r\nContent-Disposition: attachment; filename="{name}"\r\n'
            + f"Content-Transfer-Encoding: base64\r\n\r\n{b64}\r\n--b--\r\n")
msgs = {
    "r1": plain("r1", '"Test test"@repro.example.test'),    # quoted local part, RFC-valid
    "r2": plain("r2", "Test test@repro.example.test"),      # the corpus's unquoted form
    "r3": plain("r3", "Someone <a b@repro.example.test>"),  # space inside <>
    "r4": plain("r4", "Someone <plain@repro.example.test>"),
    "r5": att("r5", "notapdf.pdf", "application/pdf", "eW91IGNhbiBzZWUgbWU="),  # 'you can see me'
    "r6": att("r6", "notapdf.bin", "application/octet-stream", "bm90IGEgcGRm"),  # 'not a pdf'
}
with smtplib.SMTP("127.0.0.1", 3525) as s:
    for raw in msgs.values():
        s.sendmail("repro@repro.example.test", ["support@frappe.test"], raw.encode())
```

Result on Frappe Helpdesk 1.31.0 after one pull: `r4` and `r6` opened
tickets; `r1`, `r2`, `r3` and `r5` made no ticket and no Communication and
left four `EmailAccount.receive` errors, `test test@repro.example.test is not
a valid Email Address` twice, `a b@repro.example.test is not a valid Email
Address`, and `PdfStreamError: Stream has ended unexpectedly`. The mailbox was
empty afterwards.

Run it on a fresh site. `File.save_file` looks for an existing File with the
same content hash, and when it finds one it reuses it and never reaches
`check_content`, so once a site holds a File with r5's bytes, r5 is filed.
That is why the control r6 carries different bytes. It also means finding B
depends on what the site has stored before: the same broken PDF that loses a
message on a fresh site is accepted once a copy of it exists. Finding 1 does
not: the EXIF strip runs before the duplicate check.

### TestingHQ side: one corpus file loses its headers in the rewrite

Not a loss in Frappe, but found while tracing. `mail054.box` has an unfolded
header line (an `X-Brightmail-Tracker` continuation with no leading
whitespace) before its From. Python's parser ends the header there
(`MissingHeaderBodySeparatorDefect`), so the payload TestingHQ sends has no
From, To or Subject and the rest of the header is in the body. Frappe filed it
with no sender (one of the four `Email missing From header` entries). Zammad
recovers the From (`Karoline.Smith@example.com` in its `.yml`). So for this
file the corpus run tests TestingHQ's parse, not the original message.

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
