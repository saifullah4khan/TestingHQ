# Running TestingHQ against a real deployment

Every other run in this repository is against something in the tree: a loopback
server, a JSON Lines file, a fake client. This is the one document about running
it against a deployment somebody else operates.

**It has not been done.** There is no recording of a successful dogfood run, no
timing, and no verified field map. The harness below exists so that doing it is
one command once two values are supplied, and so that the person who does it is
explicitly told what has and has not been checked.

## What you need from the deployment's owner

One value, an environment variable rather than something written into a tracked
file:

| Variable | What it is |
| --- | --- |
| `HANDLEHQ_TOKEN` | a token that deployment issued for this run |

You also need the base URL of the deployment, but that is not a secret and it is
not an environment variable: `[readback].url` is a literal, the config loader has
no way to interpolate one, and pretending otherwise in a template would be
misleading. It goes in your own config copy.

The name `HANDLEHQ_TOKEN` is the one this repository chose, referenced from the
config as `env:HANDLEHQ_TOKEN`. If the deployment issues tokens under a
different name, changing it is one line in the recipe and one in `.env.example`.

## Running it

```bash
# 1. Copy the recipe and the environment. Both copies are gitignored, so a real
#    host and a real token never reach a commit.
cp examples/readback/handlehq.toml examples/readback/handlehq.local.toml
cp .env.example .env
$EDITOR examples/readback/handlehq.local.toml     # set the url
$EDITOR .env                                      # set the token

# 2. Load the environment. TestingHQ reads the process environment, not a file,
#    so something has to do this.
set -a && . ./.env && set +a

# 3. Check the config before sending anything. Sends nothing, and prints no
#    header value, so the output is safe to paste into an issue.
testinghq config validate examples/readback/handlehq.local.toml

# 4. See what it would send. Dry run by default, and nothing leaves the machine.
testinghq verify fire --config examples/readback/handlehq.local.toml --count 5 --seed 1

# 5. Send, and check what the deployment actually produced.
testinghq verify fire --config examples/readback/handlehq.local.toml --count 5 --seed 1 --send

# 6. Confirm every message was accounted for exactly once.
testinghq ledger fire --config examples/readback/handlehq.local.toml --count 5 --seed 1 --send

# 7. Read the artifact back afterwards, without needing the original command.
testinghq report run.json
```

Step 3 is the one to do first. It resolves `[readback.headers]` against the
environment, so a missing or unset token fails there rather than after messages
have been sent.

The recipe's URL is `https://replace-me.invalid/...`, which is deliberate. If you
forget to set it, the run fails at DNS. It is not set to `localhost`, because a
loopback URL is reachable and answers 404, and 404 means "no record of this
message", which is a finding: an unedited template would report a pipeline
losing every message.

## What to expect from the readback guardrail

A real deployment is a public host, and the guardrail refuses a public readback
URL unless it is told not to. The recipe leaves `allow_public_hosts` commented
out, so:

```bash
testinghq verify fire --config examples/readback/handlehq.toml --send \
  --allow-public-readback
```

That is the better form. Setting `allow_public_hosts = true` in the file makes
the permission permanent, and the URL being read is a store that may hold other
people's data. Passing it per run means a person re-decides it every time. See
`docs/SECURITY.md` for why the guard exists.

## Waiting long enough

Staging is slower than a loopback sink. `--quiet-window` defaults to 5 seconds
and `--max-wait` to 60, and those defaults are tuned for a local process. If runs
report messages as lost, raise them rather than concluding the pipeline is
losing them:

```bash
  --quiet-window 20 --max-wait 180
```

A run that gives up reports GAVE UP, which is a different claim from "everything
was found". Read the artifact rather than the exit code alone.

## Before trusting a result

The recipe names only `ticket_id`, but that does **not** mean only `ticket_id` is
checked. The field map is merged over the defaults, so every default path is
still live: a response carrying `subject` will have its subject checked, even
though the recipe never mentions it. The defaults assume the API names its
fields as TestingHQ names them, which is exactly the assumption that has not
been checked against a real deployment.

That matters in both directions. An operator who assumes nothing else is checked
will trust a green run that checked more than they thought, and one who assumes
more is checked than is will spend an afternoon on a field the tool was reading
at a default path all along.

To check more, point `curl` at the readback endpoint with a tag from step 4 and
look at what actually comes back, then uncomment the paths that are right.

`ticket_id` alone is enough for `ledger`, which is the check that matters most:
did every message produce exactly one record. The field checks are about whether
the deployment *parsed* the message correctly, and that needs the real response
shape.

## If a run finds something

`verify` exits 3 for a finding, 1 for a refusal, 2 for a dry run. The exit codes
are documented in the README and defined once in
`testinghq/core/exit_codes.py`.

Read the artifact with `testinghq report run.json` before concluding anything
about the deployment. Two failure modes are worth separating up front, because
they look the same in a summary:

**The tool did not send it.** `summary.transport_unanswered` or
`transport_non_2xx` non-zero means the request never got a 2xx. That is a
network or endpoint problem, not a pipeline problem.

**The deployment did not record it.** The message was sent and the readback
found nothing. That is the finding this tool exists for.

## What is still unverified after a successful run

A green dogfood run establishes that one deployment, one configuration and one
payload size behaved as expected. It does not establish:

- the `mailgun`, `postmark` and `mime` wire formats (written from vendor
  documentation, never fired at a live account)
- the Zendesk and Freshdesk readback recipes (same)
- the field map beyond `ticket_id`, until one is added against a real response

`CHANGELOG.md` carries a Provenance section saying the same, and it should be
updated rather than left stale once this has actually been run.
