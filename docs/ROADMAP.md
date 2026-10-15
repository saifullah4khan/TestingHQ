# Roadmap

What TestingHQ is for, what 1.0 delivers, what comes next and in what order.
Dates are targets for a one-person project, not promises. Anything here that
changes a flag, an exit code, a config key or the `report --json` shape lands
in a major version, per the policy at the top of [CHANGELOG.md](../CHANGELOG.md).

## Goals

1. Answer the questions an inbound-email pipeline owner has and nothing else
   answers: did we lose a message, duplicate one, thread a reply wrong, loop
   with an auto-responder, or mislabel a ticket. The tools that answer those
   (`ledger`, `redeliver`, `loop`, `steady`, `verify`) are the centre of the
   suite; `blast` and `barrage` exist to produce the input and the load those
   tools need.
2. Be runnable against a real deployment by someone who did not write it, in
   under an hour, from `pip install testinghq` and one config file.
3. Never be dangerous by accident. Dry run by default, synthetic addresses
   only, configured targets only, explicit consent for a public host, hard
   ceilings on load.
4. Stay small. Standard library at runtime, one optional dependency below
   Python 3.11, no service to run, one file per report.

## Where 1.0 stands

1.0.0 is the first published release. Every tool is reachable from the
command line, every verdict is derived from what the pipeline actually did,
and every send path speaks the wire format its target declares. A release is
a `v*` tag; `.github/workflows/release.yml` checks the version, the changelog
and the built metadata agree before it publishes.

Still unproven at 1.0, stated plainly so nobody reads a green suite as more
than it is:

- No run against a live Zendesk, Freshdesk, Mailpit, Chatwoot, Mailgun or
  Postmark account. The recipes and formats are written from the vendors'
  documentation and tested against sample responses.
- No recorded run against a real staging deployment. `docs/DOGFOOD.md` is the
  procedure; the first recorded run is the first 1.x milestone.
- The `report --html` page renders one run. It does not compare runs or show
  a trend.

## Timeline

### 1.0.x (first two weeks after the tag)

Bug fixes only. The first two are the point of the release:

- A recorded dogfood run against a staging deployment, following
  `docs/DOGFOOD.md`, with the artifacts committed under `examples/runs/` and
  the Provenance section of the changelog updated to say what was run.
- A live run of the Mailpit recipe (Mailpit is free and local), so at least
  one `[readback]` recipe has been executed against the real software.
- Fixes for whatever those two runs find.

### 1.1 (about a month after 1.0)

The CI story and the first integrations.

- **GitHub Action.** A separate repository, `testinghq-action`, with an
  `action.yml` that takes a config path, a tool and a `fail-on` setting and
  turns the exit code convention into a workflow step. One `uses:` line is a
  far lower ask than the README.
- **Chatwoot and osTicket recipes run live**, against docker-compose
  installs. Chatwoot ingests through Rails Action Mailbox (SendGrid raw mode,
  Mailgun MIME, Postmark relay) and has a conversation search API for
  readback. osTicket takes raw MIME at `api/tickets.email`. Both are
  reachable now that `sendgrid-raw` and `mime` fire.
- **Zammad's corpus as a Blast source.** Zammad ships over a hundred real
  problem emails under `test/data/mail/`. A `--corpus <dir>` option on
  `blast fire` that reads `.eml` or `.box` files and fires them in the
  target's format, with the same tagging as the generated corpus, makes
  Blast's messiness credible without inventing it.
- **Second-hop readback.** A `[readback.follow]` table that fetches one more
  URL per record (Freshdesk requester to contact email, Zendesk ticket to its
  attachments endpoint, Jira issue to its attachment field) so the field
  checks work on APIs that split a ticket across resources.
- **Per-message route expectations.** `--expect-route-map file.toml` keyed by
  recipient or by intent, for pipelines that route by recipient to different
  queues. Today `--expect-route` is one value for the whole run.
- `--json` and `--quiet` on every tool, and shell completion for bash, zsh and
  fish generated from the parser.

### 1.2 (the quarter after)

Reaching the parts of the path a webhook cannot.

- **`heartbeat`**, the synthetic end-to-end monitor designed in issue #72:
  send one real message through SMTP to the operator's declared intake
  address, read it back through the seam, record end-to-end latency, exit 3
  on an SLA miss. SMTP only in the first cut, `once` before `watch`, the
  destination allow-listed in config, `--i-understand-this-sends-real-mail`
  on top of `--send`, a five-minute floor on the interval. The five open
  questions in that issue are answered before the send path is written.
- **An SMTP and IMAP injection transport** for pipelines that pull mail
  rather than receive webhooks (Zammad, FreeScout, UVdesk, Peppermint). The
  readback seam is unchanged; only the send side gains a transport, behind
  the same consent flags as a public target.
- **Ownership proof for public targets.** Before the first send at a public
  host, fetch `https://<host>/.well-known/testinghq` and require it to carry
  the token from the target's config table. A cleaner story than two flags,
  and the same shape as domain verification elsewhere. Optional; the flag
  and the Barrage config opt-in remain.
- **Hypothesis strategies.** A `testinghq[hypothesis]` extra exposing the
  generator and the mutators as strategies, so a parser author can use Blast
  inside their own unit tests, which is where most email parsers are tested.
- **Raw MIME depth for Blast.** Quoted-printable and base64 bodies, nested
  `multipart/alternative` inside `multipart/mixed`, inline images with
  `Content-ID`, folded and RFC 2047 encoded headers, `winmail.dat`, broken
  boundaries. These only matter to a target consuming `sendgrid-raw` or
  `mime`, which is why they wait for the formats to be exercised live.
- **Barrage ramp-down and soak.** A descending stage after the hold, and a
  `--soak` mode that holds for hours at a low rate and reports drift, for the
  question "does it degrade over a day" rather than "where is the knee".

### 2.0

Only if 1.x shows there is demand for it. Anything in this section changes a
contract.

- **Route D: the operator console.** The web UI removed in 1.0 exposed one
  tool through a fire surface on a loopback server with no origin checks. A
  console worth building is a different thing:
  - `testinghq serve`, a local server that lists every run artifact in a
    directory, renders each with the `report --html` renderer, and shows
    trends across runs of the same tool and target (p99 over time, flip rate
    over time, lost and duplicated counts per run).
  - A run launcher for every tool, not one, that builds the exact command
    line it is about to run, shows it, and requires the same `--send` and
    public-target consent as the CLI. The server never holds a credential;
    `env:` references resolve in the server's process only.
  - Origin and Host checks on every POST, `application/json` only, a per
    session token tied to the page, request size and count caps, and the
    same `TokenBucket` as the CLI on every fire.
  - Shipped in the wheel as package data, served from `127.0.0.1` only unless
    `--host` is passed, with the warning that `http.server` is not a
    production server.
  - A `--watch` mode that re-renders when a new artifact appears, so a CI job
    writing artifacts to a shared directory is visible without a browser
    refresh.
- **A readback `kind = "sql"`** for the homegrown Postgres or MySQL table,
  with the query in config and the DSN as an `env:` reference, so the most
  common "we built our own" case does not need a Python adapter.
- **Multi-run comparison in `compare`** for every tool, and in
  `report --html`: two or more artifacts in, one page out, regressions in
  red.
- **Localised Blast corpora.** Whole bodies written by people, not
  translated, starting with the Urdu and Roman Urdu the steady fixture
  already carries and adding Arabic, Hindi, Bahasa and Turkish.

## Distribution

Not code, but on the critical path for the goals above:

1. PyPI release of 1.0.0 through the existing trusted-publishing workflow.
2. One recorded demo of `ledger` finding a duplicate under `barrage` load,
   linked from the README.
3. A Show HN and an r/Python post framed around the finding, not the suite.
4. The GitHub Action listed on the Marketplace.
5. A post on provider retry behaviour (Postmark's documented schedule,
   SendGrid's, Mailgun's) and how `redeliver` reproduces it.

## Deferred on purpose

Things considered and set aside, so they are not re-litigated:

- A hosted service. The suite is a CLI against systems the operator
  controls; hosting it would mean holding other people's credentials.
- Real mail delivery to arbitrary inboxes. Out of scope permanently; see
  `docs/SECURITY.md`.
- A plugin system for mutators. The mutator table in `blast/corrupt.py` is
  the plugin system; a new mutator is a function and a line in that table.
- Spam-filter evasion or sender-authentication forgery. Never.
