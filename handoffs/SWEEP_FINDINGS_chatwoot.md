# Sweep findings: Chatwoot 4.18.0

Run on 2026-10-19 against Chatwoot v4.18.0 (`chatwoot/chatwoot:v4.18.0-ce`,
`config/app.yml` version 4.18.0, Sidekiq at its default concurrency of 10),
with the stack in `sweep/compose/chatwoot.yml`. The run is published in
`examples/runs/chatwoot/`.

Every command below runs from the repository root against that stack, after:

```
docker compose -f sweep/compose/chatwoot.yml up -d
bash sweep/setup/chatwoot.sh
set -a; . sweep/.env.chatwoot; set +a
```

Three findings are Chatwoot's. All three come from `redeliver` (exit 3,
DELIVERY-BUGS); verify, ledger, loop, steady, both blasts and barrage were
clean. The threading facts below are read from Chatwoot's own `messages` and
`conversations` tables, not only from the readback.

## 1. An in-order thread sent quickly is split across conversations

- Tool: `redeliver`, scenario `references`.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/chatwoot.toml --target chatwoot --send --scenario references --max-wait 120 --quiet-window 15
  ```

- Sent, in order and back to back: a root, a reply with `In-Reply-To` and
  `References` naming the root, and a second reply with `In-Reply-To` naming
  the first and `References` naming both. Each was answered 204 by the ingress
  before the next was sent.
- Stored, in the published run:

  ```
  message 152  conversation 74  ...-ref-0-0000 (root)      05:32:51.811
  message 153  conversation 75  ...-ref-0-0001 (reply 1)   05:32:51.882
  message 154  conversation 76  ...-ref-0-0002 (reply 2)   05:32:51.919
  ```

  Chatwoot parsed every header correctly: message 153's stored
  `content_attributes.email.in_reply_to` is exactly message 152's
  `source_id`. Yet each message opened a conversation of its own. In another
  run, reply 2 was stored before reply 1 (`.787` and `.812`), so it was
  processed out of the order it was sent.
- How often: the thread was split in 5 of 7 runs at the tool's default pace,
  which sends the three 40 to 70ms apart (the two sweep runs and five
  repeats), and in none of 4 runs with `--rate 0.5`, the same three messages
  two seconds apart. A pair sent by
  hand with curl about 90ms apart threaded.
- Why it is Chatwoot's: the ingress stores each message and answers before
  it is processed; an `ActionMailbox` job then routes it, and Sidekiq runs ten
  of those jobs at once. `Mailbox::ConversationFinderStrategies::InReplyToStrategy`
  finds the parent by `Message.find_by(source_id: in_reply_to)`, and
  `ReplyMailbox#process` creates the parent's message inside a transaction,
  so a reply whose job runs before, or alongside, its parent's commit finds
  nothing and `NewConversationStrategy` opens a new conversation. Nothing
  later merges them. Mail that a provider or an MTA delivers in a burst, the
  normal case for a thread with quick replies or a queued backlog, is filed
  as separate conversations depending on job timing. The recipe reads each
  message by its own tag and reports the conversation it is on, which is the
  `conversation_id` column above.

## 2. A reply is recorded as sent by the conversation's contact

- Tool: `redeliver`, scenario `reply-first` (and `references` whenever it
  threads).
- Sent: replies from `bmuller19@example.com` and `ivan.chen@example.net` in a
  thread opened by another address.
- Stored: in the published run, both replies are on conversation 72, whose
  contact is `bmuller19@example.com` (the first reply opened it), and both
  messages have `sender` contact 3, `bmuller19@example.com`, including the one
  whose `From` was `ivan.chen@example.net`. The real From survives only in
  `content_attributes.email.from`. In the `references` runs that threaded,
  every reply was recorded as sent by the root's sender,
  `grace.brown@internal.localhost`.
- Reproduce: the command in finding 1 with `--rate 0.5`, then

  ```
  curl -s -G -H "api_access_token: $CHATWOOT_API_TOKEN" http://127.0.0.1:3100/api/v1/accounts/1/search/messages --data-urlencode 'q=[testinghq:<tag of reply 1>]'
  ```

  `sender.email` is the root's sender; `content_attributes.email.from` is
  `bmuller19@example.com`.
- Why it is Chatwoot's: `MailboxSanitizer#sanitized_message_attributes`
  sets `sender: @conversation.contact` for every incoming email, whoever it
  is from. A thread answered by a colleague on Cc, or by the customer from a
  second address, shows every message as the first sender's in the dashboard
  and in the API, and an agent's reply goes to that contact. The readback's
  sender is the message's `sender`, the record Chatwoot keeps of who sent it.

## 3. A reply that arrives before its original is never threaded with it

- Tool: `redeliver`, scenario `reply-first`.
- Command:

  ```
  testinghq redeliver fire --config sweep/configs/chatwoot.toml --target chatwoot --send --scenario reply-first --max-wait 120 --quiet-window 15
  ```

- Sent: two replies, each with `In-Reply-To` and `References` naming the
  root, then the root. The Dates are in thread order.
- Stored: both replies on conversation 72 and the root on conversation 73,
  in every run. Chatwoot matched the second reply to the first because both
  name the same parent (`NewConversationStrategy` looks for a conversation
  whose `additional_attributes.in_reply_to` is the same), and when the root
  arrived it opened a new conversation and never looked for the replies
  waiting on it.
- Why it is Chatwoot's: the same class as the arrival-order finding expected
  from Frappe Helpdesk. Chatwoot already keeps the parent's Message-ID on the
  replies' conversation (`additional_attributes.in_reply_to`), so a root
  arriving later could be matched to it; it is not. A provider retrying a
  queued delivery reorders mail like this.

## Reported first, then traced to the recipe

Fixed on this branch; the published run is after the fix.

- The recipe read Chatwoot back per conversation, so in a thread every
  message read back as the thread's first sender, the conversation's subject
  and the latest message's headers. The first live run reported a broken
  References chain, wrong subjects and wrong bodies for threads Chatwoot had
  filed correctly. The recipe now uses Chatwoot's message search, one record
  per message. The sender mismatches it still reports are finding 2.
- loop counted every conversation opened for machine mail as a finding.
  Chatwoot does that by design, marks such a conversation `auto_reply` and
  sends no automatic email, so the config now sets `ticket_policy =
  "allowed"`, as FreeScout's does.
- What looked like a charset fault (`ZÃ¼rich`) was the Windows console
  decoding curl's output; Chatwoot stores `c3 bc`, the UTF-8 for `ü`.
