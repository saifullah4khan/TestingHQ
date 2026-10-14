# Running TestingHQ against a real staging deployment

This is the procedure for pointing TestingHQ at a deployment you operate but
that is not on your machine: a staging HandleHQ, or any other inbound-email
pipeline. Everything else in the repository runs against loopback.

Status: no run against a live deployment has been recorded yet. When one is,
add the date, the tool versions and the verdicts to CHANGELOG.md.

## What you need

| Item | Where it goes |
| --- | --- |
| The staging webhook URL (where the email provider POSTs) | `url` in `[targets.handlehq]` of your local config copy |
| The URL that lists the records the deployment made, filterable by tag | `url` in `[readback]` of the same copy |
| A token the deployment issued for this run | `HANDLEHQ_TOKEN` in `.env`, referenced as `env:HANDLEHQ_TOKEN` |
| Which provider format the webhook expects | `format` in `[targets.handlehq]`, see [FORMATS.md](FORMATS.md) |
| For sigcheck: the provider's signing secret | another variable in `.env`, referenced as `env:NAME` |

URLs are literals in the config file; only header values and secrets come from
the environment.

## Set up

```bash
cp examples/readback/handlehq.toml examples/readback/handlehq.local.toml
cp .env.example .env
$EDITOR examples/readback/handlehq.local.toml   # set both urls, and format if not sendgrid
$EDITOR .env                                    # set HANDLEHQ_TOKEN
set -a && . ./.env && set +a                    # TestingHQ reads the environment, not .env
```

Both copies are gitignored, so a real host and a real token never reach a
commit. The tracked recipe's readback URL ends in `.invalid` on purpose: an
unedited copy fails at DNS instead of reading a loopback 404 as "every message
lost".

Check the config before sending anything:

```bash
testinghq config validate examples/readback/handlehq.local.toml
```

It resolves `[readback.headers]` against the environment, so a missing token
fails here rather than after messages have gone out. It also prints, for the
target, whether its host is public and what firing at it needs. It prints no
header value, so the output is safe to paste into an issue.

## Public hosts need consent on every run

A staging deployment is a public host: not a reserved test domain, not a
private address, and not a name that resolves only to private addresses. Firing
at it needs `--allow-public-target` on the run, and reading back from it needs
`--allow-public-readback`. Both are per run on purpose, so a person decides
again each time. Barrage at a public host also needs `public = true` in
`[targets.handlehq]`. See [SECURITY.md](SECURITY.md).

## Run

Dry run first; it sends nothing and shows what would be sent and read:

```bash
testinghq ledger fire --target handlehq --config examples/readback/handlehq.local.toml --count 5
```

Then, in this order, each with the two consent flags:

```bash
testinghq ledger fire --target handlehq --config examples/readback/handlehq.local.toml --count 5 --send --allow-public-target --allow-public-readback --max-wait 180 --out ledger.json
testinghq verify fire --target handlehq --config examples/readback/handlehq.local.toml --count 5 --send --allow-public-target --allow-public-readback --max-wait 180 --out verify.json
testinghq redeliver fire --target handlehq --config examples/readback/handlehq.local.toml --send --allow-public-target --allow-public-readback --retry-after 30 --max-wait 180 --out redeliver.json
testinghq report ledger.json --html ledger.html
```

`ledger` first because it needs only `ticket_id` from the readback: did every
message produce exactly one record. `verify` then checks the parsed fields,
which needs the real response shape (below). `redeliver` checks Message-ID
deduplication and threading.

If the deployment records a triage label, `steady` measures how stable it is;
name the field that holds it:

```bash
testinghq steady fire --target handlehq --config examples/readback/handlehq.local.toml --send --allow-public-target --allow-public-readback --label-field category --out steady.json
```

`loop` also needs to read what the deployment sends: add a `[loop.outbound]`
table pointing at its outbound mail log or sink, and set `[loop].reply_address`
to its real reply address. Without it the auto-reply check is reported as NOT
CHECKED.

If the webhook verifies the provider's signature, check that it refuses what it
should:

```bash
testinghq sigcheck fire --target handlehq --config examples/readback/handlehq.local.toml --scheme mailgun --secret env:MAILGUN_SIGNING_KEY --send --allow-public-target
```

## Waiting long enough

`--quiet-window` (default 5s) and `--max-wait` (default 60s) suit a local
process. A deployment that creates the record on a queue consumer needs a quiet
window longer than the gap between the record appearing and being filled in,
and a search-indexed store needs `--max-wait` of a few minutes. A run that does
not settle reports UNSETTLED, which is not a loss: raise `--max-wait` before
concluding anything. The readback paces itself to `--readback-rate` (default 4
per second) and backs off on 429.

## Before trusting a result

The recipe maps only `ticket_id`, but the field map is merged over the
defaults, so every default path (`from`, `subject`, `body`, `attachments`,
`route`, ...) is still read. If the deployment names a field differently, that
check reports NOT CHECKED, "adapter cannot see field". Fetch one record by hand
with a tag from the dry run, compare its shape with [CONFIG.md](CONFIG.md), and
map the paths in `[readback.fields]` before reading `verify`'s field checks.

## If a run finds something

Read the artifact with `testinghq report ledger.json` before concluding
anything. Separate two cases:

- **The request never got a 2xx.** The artifact's transport counts are
  non-zero. That is a network, auth or endpoint problem, not a pipeline one.
- **The request got a 2xx and the record is missing, duplicated or wrong.**
  That is the finding this tool exists for.
