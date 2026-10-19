# The pull-mail pattern

Some helpdesks never take a request for an incoming email. They pull it: an
email channel logs in to a mailbox over POP3 or IMAP on a schedule, opens a
ticket per new message, and deletes or flags what it fetched. Zammad, FreeScout
and Frappe Helpdesk work this way. TestingHQ reaches them by playing the part
of the mail that arrives in that mailbox.

```
testinghq ──SMTP──▶ mail-in ◀──POP3/IMAP── helpdesk ──SMTP──▶ mail-out
    ▲                (sidecar)              (polls)            (sidecar)
    └──────────── HTTP readback: the helpdesk's ticket API ◀────┘
```

1. **Send.** The target is `transport = "smtp"` at the mail-in sidecar's SMTP
   port on 127.0.0.1. The bytes are what the `mime` format builds.
2. **Poll.** The helpdesk's channel fetches mail-in on its own schedule and
   turns each message into a ticket.
3. **Read back.** `[readback]` asks the helpdesk's API for the ticket by tag.
   TestingHQ stamps the tag into the body as `[testinghq:<tag>]` and into the
   `X-TestingHQ-Tag` header, so a search over ticket text finds it.

`sweep/compose/zammad.yml`, `sweep/setup/zammad.sh` and
`sweep/configs/zammad.toml` are the worked example.

## The two mailboxes

The helpdesk's outbound SMTP must not go to the mailbox it fetches from. A
mail server sidecar like Mailpit keeps one store for every recipient, so the
helpdesk's own auto-reply to a test customer would come straight back in on the
next fetch and be filed as a new message, which is a loop the harness made,
not one the helpdesk has. Give outbound its own sidecar (`zammad-mail-out`).
Its API then holds every reply the helpdesk sent, which is what a
`[loop.outbound]` sink reads.

## Mail-in: a delivery sidecar and Dovecot

The mailbox the helpdesk fetches has to hand mail back in the order it
arrived, because a helpdesk imports a fetch in the order the server lists it,
and a thread delivered together is only threaded if its original is imported
before its replies. The sweep uses two sidecars for it:

- a delivery sidecar, `sweep/compose/freescout/maildir_smtp.py`, an SMTP
  server that writes each message for the mailbox address into a Maildir,
  `tmp/` then `new/`, under a name that sorts in arrival order;
- Dovecot serving that Maildir over POP3 or IMAP, which lists it in the order
  the mail arrived. A POP3 `DELE` or an IMAP expunge removes a fetched
  message, as with a real server.

`sweep/compose/zammad.yml` (POP3) and `sweep/compose/freescout.yml` (IMAP) are
the two worked examples.

Mailpit is not used as mail-in, although it serves POP3: its POP3 server lists
newest first. Zammad imports a fetch in list order, so against Mailpit every
fetch was imported in reverse, an in-order thread was filed reply before
original, and redeliver reported a broken thread the helpdesk did not cause.
Mailpit stays the outbound sink, where only its API is read.

## Target

```toml
[targets.<helpdesk>]
transport = "smtp"
host = "127.0.0.1"
port = 3325                         # the sidecar's SMTP port on the host
envelope_to = "support@zammad.test" # synthetic, and the channel's own address
```

`envelope_to` must be on a reserved domain or TLD: TestingHQ refuses any other.
The delivery sidecar writes mail for this address into the mailbox and records
mail for any other address as outbound, so it is the channel's own address.

## Waiting

A ticket exists only after the next fetch and whatever processing follows it.
The readback settles once nothing it sees has changed for `--quiet-window`
seconds, and a tag still missing then is reported as lost, so the quiet window
has to be longer than one poll period plus processing, with room to spare, and
`--max-wait` well above that. Zammad fetches every 30 seconds:
`sweep/profiles/zammad.env` sets `SWEEP_QUIET_WINDOW=60` and
`SWEEP_MAX_WAIT=240`. Set them from the helpdesk's own poll period.

The channel files every ticket under one group or mailbox, so the profile
names it as `SWEEP_EXPECT_ROUTE` (Zammad: `SWEEP_EXPECT_ROUTE=Users`), which
verify compares with the route the readback maps (`route = "group"`).

## Setup script

The setup script creates the channel pointing at the mail-in sidecar by its
compose service name (`zammad-mail-in:1110`), with the credential from the
compose file, and outbound at the mail-out sidecar. Prefer the helpdesk's own
provisioning path (Zammad's auto wizard, run through
`/api/v1/getting_started/auto_wizard/<token>`) over driving a UI, and fall back
to its console (`docker compose exec ... rails r`) when that path is gone, so a
second run converges on the same state.

A channel "verify" step that sends a test mail through outbound and waits for it
on inbound cannot pass with two mailboxes. Create the channel directly instead.

## Fake

A fake for a pull-mail helpdesk (`sweep/fakes/zammad.py`) folds the three parts
into one process: an SMTP server that keeps what it takes in a mailbox, a fetch
that turns the mailbox into tickets (immediately, or every `fetch_interval`
seconds to imitate the poll), and the helpdesk's API over those tickets. It
exposes `.smtp_port` for the target and `.port` for the readback.
