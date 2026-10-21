# Handoff: 1.1 part 1, wave 2, the live sweep

Branch `sweep/1.1-live` on `saifullah4khan/TestingHQ-staging`, open as
[PR #4](https://github.com/saifullah4khan/TestingHQ-staging/pull/4) into `main`,
"1.1 part 1: live sweep". Work done 2026-10-19 and 2026-10-20 on Windows 10
with Docker Desktop. Nothing was pushed to or opened against
`saifullah4khan/TestingHQ`, and nothing was filed on any helpdesk's tracker:
every helpdesk finding is in `handoffs/SWEEP_FINDINGS_<helpdesk>.md`.

This file is the whole picture: what was run, what was found, what was
changed and why, the judgement calls, what is left, and how to run it again.
The per-helpdesk findings files hold the detail behind each finding (command,
what was sent, what was stored, a minimal reproduction, and why it is the
helpdesk's).

## 1. Where it stands

- Every TestingHQ tool ran against all five helpdesks on fresh stacks, and
  every step of every run completed (a tool exit of 0, or 3 for a finding).
  Nothing was dropped; FreeScout, the one the plan allowed dropping, ran.
- 17 findings are the helpdesks' own (table in section 3), plus one Frappe
  behaviour recorded as an observation.
- 7 bugs in TestingHQ itself were fixed, each with a test that fails before
  and passes after, plus 18 test-suite failures that existed on a clean
  Windows checkout of `main` (23 counting five later FreeScout integration
  tests with the same cause).
- The sweep's compose files, setup scripts, configs and recipes had many
  problems that only a live run shows; all are fixed (section 6).
- Each run is published in `examples/runs/<helpdesk>/` with a README, and
  each recipe's status line says `RUN against <Helpdesk> <version> on
  2026-10-19`. The Mailpit reference run was republished with the final code.
- 35 commits, all authored and committed as Saifullah Khan
  <saifullah4khan@gmail.com>, lowercase `type: subject` under 72 characters
  with a body, no em-dashes, no attribution lines.
- Checks, run locally on Windows on the final tree:

  | Check | Result |
  | --- | --- |
  | `pytest -q -m "not allow_network"` | 2515 passed, 2 skipped |
  | `pytest -q -m allow_network` | 54 passed, 5 skipped |
  | `ruff check --select F .` | all checks passed |
  | `python -m testinghq.config_doc --check` | `docs/CONFIG.md` is up to date |
  | `node --test tests/report_html/*.test.cjs` | 15 passed |

  The skips: five FreeScout integration tests and one FreeScout adapter test
  are skipped on Windows only, because the fake moves fetched mail to a
  Maildir name ending `:2,S`, which NTFS cannot hold; one `test_sweep_run`
  skip was already there. PR CI (Linux) is green on all 14 checks: tests on
  Python 3.10 to 3.13, end-to-end on 3.10 and 3.13, lint, report-html,
  security and dependency audit.

## 2. Environment and setup

- Windows 10 Home, Docker Desktop 4.95.0 (Engine 29.8.2), Git Bash, Python
  3.13.13 in `.venv` (`pip install -e ".[dev]" PyMySQL`), `gh` logged in as
  `saifullah4khan`, Node 24.16 for the report-html tests.
- Clone at `~/TestingHQ-staging`. Every sweep command was
  `PYTHON=python SWEEP_KEEP=1 sweep/run.sh <helpdesk>` with the venv active,
  followed by `docker compose -f sweep/compose/<helpdesk>.yml down -v`.
- Smoke test first: `PYTHON=python sweep/run.sh mailpit` ended `findings`,
  the demo pipeline's known malformed-envelope bug, as expected.
- Windows gotchas that cost time and are worth knowing:
  - Git Bash rewrites any argument that starts with `/` when it calls a
    native program. `docker compose exec -w /www/html` reached docker as a
    Windows path; `MSYS_NO_PATHCONV=1` set for a whole script stopped curl's
    `-o /dev/null` becoming `NUL`, and every request failed with curl error
    23. The fix is to set it for docker only (osTicket and FreeScout setup
    now do).
  - Python's `subprocess` on Windows finds `C:\Windows\System32\bash.exe`
    (WSL) before Git's bash for the bare name `bash`, and WSL's bash cannot
    read a Windows path. Use `shutil.which("bash")`.
  - `core.autocrlf` is true in Git for Windows' system config. The sweep's
    `.sh`, `.env` and `.yml` files were already pinned to LF by
    `sweep/.gitattributes`; the Dovecot `.conf` files now are too.
  - `pkill` does not exist in Git Bash; `ps -W` and `kill` do.

## 3. Results by helpdesk

| Helpdesk | Version run | Image | Final run | Result | Helpdesk findings |
| --- | --- | --- | --- | --- | --- |
| osTicket | 1.18.4 | `hlhd/osticket:v1.18.4` (replaces the missing `tiredofit/osticket:3.6.2`) | `20261019T074051Z` | findings | 5 |
| Chatwoot | 4.18.0 | `chatwoot/chatwoot:v4.18.0-ce` | `20261019T074646Z` | findings | 3 |
| Zammad | 7.2.2 | `ghcr.io/zammad/zammad:7.2.2-0000` | `20261019T075510Z` | findings | 3 |
| FreeScout | 1.8.248 | `nfrastack/freescout:2.2.19` | `20261019T080856Z` | findings | 3 |
| Frappe Helpdesk | 1.31.0 | `ghcr.io/frappe/helpdesk:v1.31.0` | `20261019T082602Z` | findings | 3, plus 1 observation |

Verdicts per tool in the final runs:

| Tool | osTicket | Chatwoot | Zammad | FreeScout | Frappe |
| --- | --- | --- | --- | --- | --- |
| verify | MISMATCHED (3) | VERIFIED (0) | VERIFIED (0) | MISMATCHED (3) | MISMATCHED (3) |
| ledger | MISPARSED (3) | BALANCED (0) | UNVERIFIED STRAYS (3) | MISPARSED (3) | UNACCOUNTED (3) |
| redeliver | DELIVERY-BUGS (3) | DELIVERY-BUGS (3) | DELIVERY-BUGS (3) | DELIVERY-SAFE (0) | DELIVERY-BUGS (3) |
| loop | LOOPS-DETECTED (3) | LOOP-SAFE, reply not checked (0) | LOOPS-DETECTED (3) | LOOPS-DETECTED (3) | LOOP-SAFE, reply not checked (0) |
| steady | STABLE (0) | STABLE (0) | STABLE (0) | STABLE (0) | STABLE (0) |
| blast --catalog | CLEAN (0) | CLEAN (0) | CLEAN (0) | CLEAN (0) | CLEAN (0) |
| blast --corpus | FLAGGED (3) | CLEAN (0) | CLEAN (0) | CLEAN (0) | CLEAN (0) |
| barrage | STEADY (0) | STEADY (0) | STEADY (0) | STEADY (0) | STEADY (0) |

steady's "every family read one label" warning is expected everywhere: none
of these helpdesks classifies mail, so the label read is the one routing
decision each makes (department, inbox, group, mailbox, Email Account), and
it should be constant. For the three pull-mail helpdesks reached over SMTP
(Zammad, FreeScout, Frappe), blast and barrage only judge the SMTP hand-off.

## 4. The helpdesks' findings

Each is written up in full in its findings file. Short form:

### osTicket 1.18.4 (`handoffs/SWEEP_FINDINGS_osticket.md`)

1. A zero-byte attachment is dropped. `AttachmentFile::create` saves the
   `ost_file` row, then treats a zero-length write as failure; the result is
   an orphan file row and a "File Import Error" note on the ticket.
   verify 4 of 20, ledger 6 of 50.
2. Auto-responses ("Support Ticket Opened") go to machine mail it should
   leave alone: a message with only `List-Id`, only `List-Unsubscribe`, a
   `noreply@` or `no-reply@` sender, and an out-of-office marked only by its
   subject. `Auto-Submitted`, `Precedence` and bounces are held back. Each
   header was tested alone from its own address.
3. Its own auto-response sends `Auto-Submitted: =?UTF-8?Q?auto-replied?=`
   and `Precedence: =?UTF-8?Q?auto=5Freply?=`: encoded words in structured
   fields, which RFC 2047 forbids, so another autoresponder will not see the
   reply as automatic. Found while reading the auto-responses, not reported
   by a tool.
4. A reply that arrives before its original opens its own ticket and is
   never joined to the original.
5. A message with no usable sender is answered HTTP 500 ("Incomplete client
   information"); `exerr($errors['errno'] ?: 500, ...)` makes every
   validation failure a 5xx, which a mail bridge retries forever. 4 of
   Zammad's 99 test mails.

### Chatwoot 4.18.0 (`handoffs/SWEEP_FINDINGS_chatwoot.md`)

1. An in-order thread sent quickly is split across conversations: root,
   reply 1 and reply 2 landed in conversations 74, 75 and 76 although each
   reply's stored In-Reply-To equals its parent's `source_id`. 6 of 8 runs at
   the tool's default pace (messages 40 to 70ms apart) split; none of 4 at
   `--rate 0.5`. Ingest is asynchronous and Sidekiq runs ten mailbox jobs at
   once, so a reply can be processed before or alongside its parent's commit.
2. Every incoming email is recorded as sent by the conversation's contact,
   whoever wrote it (`sender: @conversation.contact` in
   `MailboxSanitizer`); the real From survives only in
   `content_attributes.email.from`.
3. A reply that arrives before its original is never joined to it (the two
   replies share a conversation; the root opens another).

### Zammad 7.2.2 (`handoffs/SWEEP_FINDINGS_zammad.md`)

1. A redelivered message opens a second ticket: Zammad skips a known
   Message-ID only on IMAP and Microsoft Graph with keep-on-server, never on
   POP3 (`MessageValidator#already_imported?` says so). Every one of six
   messages in `duplicate` and `slow-retry`.
2. A reply that arrives before its original is never threaded (tickets 84
   and 85 for the replies, 86 for the root).
3. The shipped "auto reply (on new tickets)" trigger answers a message with
   only `List-Id` and an out-of-office marked only by its subject:
   `AutoResponseCheck` checks `List-Unsubscribe`, `X-Loop`, `Precedence`,
   `Auto-Submitted` and `X-Auto-Response-Suppress`, not `List-Id`.

### FreeScout 1.8.248 (`handoffs/SWEEP_FINDINGS_freescout.md`)

1. A zero-byte attachment is silently dropped: `Attachment::create` starts
   `if (!$content && !$uploaded_file) return false;` and `""` is false.
2. The auto-reply answers a bounce FreeScout itself flagged
   `is_bounce: true`, even sent with a null envelope sender as a real DSN is.
   RFC 3834 says never to answer a null Return-Path. Reproduced by hand with
   fresh addresses (FreeScout replies to a customer at most once a day).
3. The auto-reply answers `List-Id`, `List-Unsubscribe`, `noreply@`,
   `no-reply@` and subject-only out-of-office mail; `isAutoResponder` checks
   only `X-Autoreply*`, `Auto-Submitted`, `Delivered-To: autoresponder` and
   `Precedence`.

redeliver was DELIVERY-SAFE: FreeScout files a fetch in Date order and the
replies arrived in the same fetch as their original. By the rule for this
scenario (genuine only if the replies were fetched in an earlier poll), that
is not a finding.

### Frappe Helpdesk 1.31.0 (`handoffs/SWEEP_FINDINGS_frappe.md`)

1. An attachment whose content check raises loses the whole email. A `.jpg`
   that Pillow cannot read (`strip_exif_data` > `OSError: Truncated File
   Read`) or a `.pdf` pypdf cannot read (`pdf_contains_js` >
   `PdfStreamError`) escapes `InboundMail.process`; the email was already
   deleted from the mailbox by POP3, so it is gone with only an Error Log
   entry. verify 1 of 20, ledger 3 of 50 (every message with a `.jpg`).
2. A reply that arrives before its original is never threaded (as the plan
   expected: Frappe files a pull in arrival order).
3. A sender with a quoted local part (`"Test test"@example.com`, valid
   RFC 5322) loses the whole email: `decode_email` turns the quotes into
   spaces and `validate_email_address` then rejects `raised_by`. Traced from
   the corpus run's Error Log (`mail071.box`, `mail076.box`, `mail087.box`)
   and reproduced by hand; TestingHQ's address rewrite is not the cause.

Observation, not counted: a new message with the same sender and subject as
an open ticket joins it, with no In-Reply-To or References. All four
redeliver scenario roots (distinct Message-IDs) ended up on one ticket. This
is Frappe's design, but it is why its `duplicate` and `slow-retry` scenarios
pass.

Frappe also drops, on purpose, bounces, `X-Auto-Generated` and
`Auto-Submitted` other than `auto-replied`
(`helpdesk/overrides/email_account.py`, `auto_generated_reason`), and four
corpus files went that way (`mail019`, `mail053`, `mail089`, `mail090`).

## 5. TestingHQ's own bugs, fixed

Every one has a test that fails before the fix and passes after, and a
CHANGELOG `[Unreleased]` entry.

| Fix | What was wrong | How it showed | Commit |
| --- | --- | --- | --- |
| `config basic-auth` quotes its value | It printed `NAME=Basic <b64>` unquoted; `.env.example` loads `.env` with `set -a && . ./.env`, which reads that as a command named after the token with NAME set for that command only | A known candidate; confirmed | `39a63ee` |
| Planner counts both requests of a paged lookup | With `page_param` and no `page_size_param` the adapter cannot tell a short page from a full one, so a found tag costs two paced requests; `settle_time_needed`, the plan-sized `--max-wait`, the dry-run warning and the UNSETTLED advice counted one. New `requests_per_lookup` | A known candidate; confirmed on Chatwoot (20 tags took two 10s polls) | `ecbb31a` |
| `mime` and `sendgrid-raw` send a CR/LF header value as an encoded word | The catalog's header-injection case put CRLF in the subject; the stdlib refuses a line break in a header and `blast --catalog` crashed (exit 1) on every raw-message and SMTP target | osTicket blast-catalog traceback | `65db3fa` |
| Corpus keeps a sender the stdlib misreads | `"Clement.Si" <Claudia.Shu@yahoo.com.>` (trailing dot) parsed as one bare word; the payload went out with an empty From | osTicket answered 500 for `mail044.box` | `ac1c7f0` |
| `report` lists a verify artifact's failed checks | The verify reader built findings from skipped checks only; MISMATCHED with nothing listed | osTicket verify report | `a0119f1` |
| Nested MIME boundaries never contain each other | `<boundary>-1` put the outer delimiter at the start of every inner delimiter line (RFC 2046 rules that out); a first fix, `1_<boundary>`, still held the outer value, and FreeScout's parser splits on the bare string anywhere. Now the outer boundary with its last six characters replaced by a hash of it and the depth | FreeScout stored MIME headers and quoted-printable as the body plus a stray `66617ed4`, then `61cdb76b` (`boundary="`), attachment | `e7d10aa`, `c9ddc3f` |
| loop's bounce has a null envelope sender | The envelope sender came from From, so over SMTP the server wrote `Return-Path: <MAILER-DAEMON@...>` above the message's own `<>` | FreeScout's stored bounce | `5bbdb62` |
| Suite passes on Windows | 18 failures on clean `main`: WSL bash, an MSYS `$PWD` in a stub, a Windows path in a TOML string, POSIX mode bits, `os.chown`, `:2,S` file names; later 5 FreeScout integration tests for the same name | Local runs | `f35d3f9`, `ddde52b` |

Also in the package's orbit: the Maildir delivery sidecar now zero-pads the
microseconds in its file names, so names from one second sort in arrival
order (`1196c95`, with a test).

## 6. Sweep, recipe and setup changes

### osTicket

- `tiredofit/osticket` is no longer on Docker Hub (the repository is gone,
  not only the tag). `hlhd/osticket:v1.18.4` installs osTicket 1.18.4 from
  the same `DB_*` and `ADMIN_*` variables and listens on 8080. Its mariadb
  client requires TLS, which MariaDB 10.11 does not serve, so the entrypoint
  waited for the database forever: the database moved to `mariadb:11.4.8`,
  which makes its own certificate.
- Setup scoped `MSYS_NO_PATHCONV` to docker (see section 2).
- loop had no outbound sink. Added a Mailpit (`mail-out`, API on
  127.0.0.1:3286) as osTicket's SMTP; setup activates the SMTP account the
  image seeds inactive, turns on the new-ticket and new-message
  auto-responders (off on a fresh install), and turns off the staff
  new-ticket and new-message alerts, which quote the tag and were read as
  replies to the sender.
- Config: `[loop]` with `ticket_policy = "allowed"` (osTicket files machine
  mail and holds back only its own reply), `[loop.outbound]` on the Mailpit
  API, `[steady] label_field = "route"`; profile `SWEEP_EXPECT_ROUTE=Support`.
- Adapter: a reply osTicket threads onto a ticket has no title, and the
  adapter fell back to the ticket's subject; it now reads the Subject from
  `ost_thread_entry_email.headers` first. The fake keeps no reply title
  either now, so the fakes would have caught it.

### Chatwoot

- The recipe read Chatwoot back per conversation, so every message of a
  thread read back as the thread's first sender and latest message, and the
  first live run reported broken threads and wrong subjects for threads
  Chatwoot had filed correctly. Both the sweep config and
  `examples/readback/chatwoot.toml` now use
  `GET /api/v1/accounts/1/search/messages?q=[testinghq:<tag>]`, one record
  per message (15 to a page; listing by `q=testinghq:`). The example recipe
  used to look conversations up by a label equal to the tag, which nothing
  in Chatwoot creates. The fake serves the new endpoint, with a test.
- `[loop] ticket_policy = "allowed"`: Chatwoot opens a conversation for
  machine mail by design, marks it `auto_reply` and sends no automatic email.
- Setup writes `CHATWOOT_INGRESS_AUTH` quoted.

### Zammad

- Mailpit's POP3 server lists newest first, and Zammad imports a fetch in
  list order, so every fetch was imported in reverse and the first live run
  reported an in-order thread as broken (reply-first then passed, by
  accident). Checked directly: three messages listed as 3, 2, 1. The
  channel's mailbox is now Dovecot (`zammad-mail-in`, POP3 on 1110, the same
  name and port the channel already used) fed by the FreeScout stack's
  delivery sidecar (`zammad-deliver`, SMTP on 3325); checked to list in
  arrival order. `sweep/PULL_MAIL.md` now explains why Mailpit is only an
  outbound sink.
- Config: `[loop]` (`ticket_policy = "allowed"`) with the mail-out Mailpit
  as `[loop.outbound]`, `[steady]`. Setup writes `ZAMMAD_AUTH` quoted.
- The example recipe no longer suggests Mailpit as the mail server, and says
  its one blind spot: the ticket search answers per ticket, so a threaded
  reply reads back with the ticket's customer and title.

### FreeScout

- Setup failed silently after "configuring mailbox": `-w /www/html` was
  rewritten into a Windows path. Fixed by scoping `MSYS_NO_PATHCONV` to
  docker.
- `[steady]` and `SWEEP_EXPECT_ROUTE=support@freescout.test`.

### Frappe Helpdesk

- Same Mailpit problem as Zammad (Frappe pulls POP3 in list order, checked
  in `receive.py`); inbound is now Dovecot (`mail-in`) and the delivery
  sidecar (`deliver`, SMTP on 3525). The recipe no longer suggests Mailpit.
- Setup writes `FRAPPE_API_AUTH` quoted, and the reader that reuses the key
  strips the quotes.
- `[loop] reply_address = "support@frappe.test"`, `ticket_policy =
  "allowed"`; outgoing mail is muted on the site, so the reply check reads
  NOT CHECKED. `[steady]`, `SWEEP_EXPECT_ROUTE=Sweep Support`.
- `SWEEP_ARGS_REDELIVER=--seed 7`: at seed 0 the thread's original carries a
  `.jpg`, which finding 1 loses in every scenario, hiding the threading
  answers. Seed 7's thread has no attachment.

### Everything

- `sweep/.gitattributes` now pins `*.conf` to LF as well.
- CHANGELOG `[Unreleased]` records the package fixes under Fixed, and the
  sweep and recipe changes and published runs under Added and Changed.

## 7. Judgement calls and why

- Whose bug. A finding went to a helpdesk only after reading its source in
  the running container and, where cheap, reproducing it with a hand-made
  message. Several first reports turned out to be TestingHQ's (boundaries,
  header injection, corpus sender, bounce envelope) or the setup's (Mailpit
  ordering, staff alerts, recipe readback shape), and were fixed instead;
  each findings file lists those at the end.
- Reply-first. Counted as a finding for osTicket, Chatwoot, Zammad and
  Frappe, where the original arrived after its replies and nothing
  re-threaded them; written up as a design limit (arrival-order filing)
  rather than a parse bug. FreeScout passed, so nothing to judge there.
- `ticket_policy = "allowed"` everywhere. Every helpdesk here opens a ticket
  for machine mail on purpose and suppresses only its own reply, so a ticket
  is reported, not a finding; an auto-reply to machine mail is the finding.
- Zero-byte attachments. Counted for osTicket and FreeScout: verify's clean
  corpus deliberately includes them, an empty file is valid MIME, and both
  helpdesks drop it.
- TestingHQ's "clean" image attachments are magic bytes plus filler by
  documented design, not decodable images. Left as is; Frappe's loss of the
  whole email is its bug either way. A follow-up is proposed (section 9).
- Final reruns. Once the MIME and loop fixes landed, every helpdesk was run
  again on a fresh stack so the published artifacts (payload hashes
  included) are what this branch's code sends. Verdicts were unchanged.
- Secrets. Published artifacts were scanned for the values in each
  `sweep/.env.<helpdesk>` and the compose passwords, for absolute paths and
  for em-dashes; logs were never copied. The scan's only hits were false
  positives (`chatwoot.test`, the word `zammad`), each checked by hand.

## 8. Known limits that remain

- Zammad's ledger always ends UNVERIFIED STRAYS (exit 3): its ticket search
  cannot tie a listed ticket to a tag, so `enumerate = false`. Not a Zammad
  bug.
- Zammad's readback is per ticket: a threaded reply's sender and subject
  read back as the ticket's, which shows up as sender and subject lines in
  redeliver's `references` scenario although the thread is on one ticket.
- Chatwoot and Frappe have no outbound sink (Chatwoot sends no automatic
  email; Frappe's mail is muted), so loop's reply check is NOT CHECKED there.
- osTicket's and Zammad's auto-replies are listed as "unattributed" in loop:
  Mailpit's search API returns no In-Reply-To, though both helpdesks set it.
  The recipients identify them; section 9 proposes attributing by recipient.
- Chatwoot's ledger listing covers tagged messages only (search by
  `testinghq:`); a record the pipeline made with no marker would not be
  listed.

## 9. Proposed follow-ups (not started)

- loop: attribute an outbound message by recipient when exactly one sent
  message of the run has that sender, so osTicket's and Zammad's auto-replies
  are tied to the machine-mail kind they answered.
- blast: make "clean" image and PDF attachments decodable (a real minimal
  PNG, JPEG and PDF, padded after their end markers), keeping the corrupt
  shapes in the adversarial kinds. This changes payload bytes.
- Optional: rerun Frappe's corpus on a fresh site after each Frappe release;
  `File.save_file` reuses an existing File with the same content hash, so a
  repeat on the same site can hide finding 1.

## 10. How to run it again

```
git clone https://github.com/saifullah4khan/TestingHQ-staging.git
cd TestingHQ-staging && git checkout sweep/1.1-live
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]" PyMySQL
PYTHON=python sweep/run.sh mailpit
for h in osticket chatwoot zammad freescout frappe; do
  PYTHON=python SWEEP_KEEP=1 sweep/run.sh "$h"
  docker compose -f "sweep/compose/$h.yml" down -v
done
```

Approximate wall time on this machine: osTicket 6 minutes, Chatwoot 9,
Zammad 14, FreeScout 17, Frappe 20 (the first Frappe boot runs `bench
new-site` and takes several more). Artifacts land in
`sweep/runs/<helpdesk>/<UTC timestamp>/`; `summary.json` there has every
command and exit code. To read a helpdesk's database while its stack is up,
the findings files give the exact `docker compose exec` commands.

## 11. Commits on the branch, oldest first

| Commit | Subject |
| --- | --- |
| `39a63ee` | fix: quote the value config basic-auth prints |
| `ecbb31a` | fix: count every paced request a paged lookup makes |
| `65db3fa` | fix: send a header value holding a line break as an encoded word |
| `ac1c7f0` | fix: keep a corpus sender the stdlib parser misreads |
| `66f25f3` | docs: record the readback, mime and corpus fixes in the changelog |
| `f35d3f9` | test: make the suite pass on Windows |
| `a0119f1` | fix: list a verify artifact's failed checks in its report |
| `550575a` | fix: read an osTicket reply's subject from its stored headers |
| `500f87c` | fix: get the osticket sweep running against a live osTicket |
| `ad6d641` | fix: keep osTicket's staff alerts out of the loop sink |
| `dc574e9` | fix: quote the ingress credential chatwoot setup writes |
| `cb065d3` | docs: publish the live osTicket run and its findings |
| `e4f3f38` | fix: quote the authorization value zammad setup writes |
| `ddde52b` | test: skip the FreeScout fake's fetch tests on Windows |
| `3437933` | fix: read Chatwoot back per message, not per conversation |
| `25cb4a7` | docs: publish the live Chatwoot run and its findings |
| `1196c95` | fix: pad the microseconds in the sidecar's Maildir names |
| `0de0432` | fix: give the zammad sweep a mailbox that keeps arrival order |
| `6f19110` | docs: publish the live Zammad run and its findings |
| `91e7090` | fix: stop Git Bash rewriting paths in freescout setup's docker calls |
| `e7d10aa` | fix: keep a nested MIME boundary from beginning with its parent's |
| `dcffb55` | fix: give the freescout sweep a steady label and an expected route |
| `c9ddc3f` | fix: keep every nested MIME boundary out of the others entirely |
| `5bbdb62` | fix: send loop's bounce with a null envelope sender |
| `7ed24c1` | fix: give the frappe sweep a mailbox that keeps arrival order |
| `441cb69` | fix: quote frappe's API credential and set its loop and redeliver |
| `8e9090f` | docs: republish the osTicket run with the final TestingHQ |
| `04eb553` | docs: republish the Chatwoot run with the final TestingHQ |
| `f5429ae` | docs: republish the Zammad run with the final TestingHQ |
| `4e8ce07` | docs: publish the live FreeScout run and its findings |
| `887e2b7` | docs: publish the live Frappe Helpdesk run and its findings |
| `9546c6e` | docs: trace the Frappe corpus receive errors to their files |
| `d74d6f0` | chore: keep the sweep's Dovecot configs LF on a Windows checkout |
| `41533ae` | docs: count the traced Frappe sender fault as its third finding |
| `f0e3f47` | docs: republish the Mailpit reference run with the final TestingHQ |

## 12. Where things are

- `handoffs/SWEEP_FINDINGS_{osticket,chatwoot,zammad,freescout,frappe}.md`:
  the findings, one file per helpdesk.
- `examples/runs/<helpdesk>/`: a README per run with the command, version,
  date and a verdict table. Each run's JSON, report text, HTML and
  `summary.json` are in `testinghq-sweep-runs-2026-10-20.tar.gz`, published
  as a release asset on saifullah4khan/TestingHQ; `examples/runs/mailpit/`
  keeps its JSON and report text in the repository.
- `examples/readback/*.toml`: the recipes, with updated status lines.
- `sweep/compose/zammad/dovecot.conf`, `sweep/compose/frappe/dovecot.conf`:
  the new mailboxes; `sweep/compose/freescout/maildir_smtp.py` is the shared
  delivery sidecar.
- `sweep/PULL_MAIL.md`: the pull-mail pattern, now with the ordering rule.
- `CHANGELOG.md` `[Unreleased]`: every user-visible change.
