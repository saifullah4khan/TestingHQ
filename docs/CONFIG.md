<!-- GENERATED FILE. Do not edit by hand. -->
<!-- Produced by `python -m testinghq.config_doc`, from the schema in -->
<!-- testinghq/core/config_schema.py. tests/unit/test_config_schema.py fails -->
<!-- when this file does not match what that script would write, so the only -->
<!-- way to change it is to change the schema, which is the point. -->

# Configuration reference

The tables below, generated from the code so they cannot drift. A key added to
a loader without a line here fails the suite.

**Scope.** `[targets.<name>]`, `[targets.<name>.headers]`, `[readback]`,
`[readback.fields]` and `[readback.headers]` are read by the `core.config`
loader. `[loop]`,
`[loop.outbound]`, `[steady]` and `[sigcheck]` are read from the same file by
the tool they name, and `config validate` checks their key names but not their
values.

**Unknown keys are refused.** A key a table below does not list, or a
top-level table other than those below, stops every tool before it sends
anything (exit 1), and `config validate` reports the same error, naming the
file, the table, the key and the closest known key. A typo is never ignored,
because an ignored `formt = "mailgun"` would send in the default format.
Three exceptions, all by design: the names inside `[readback.fields]`,
`[readback.headers]` and `[targets.<name>.headers]` are data (field names are
checked against the readback fields when the adapter loads), and a `[readback]` or `[loop.outbound]` table
whose `kind` (or `spec`) names a custom `module:attribute` adapter may carry
keys of its own.

Validate a file and see what the loaders make of it:

```bash
testinghq config validate target.toml
```

`config validate` sends nothing and never prints a header value. Headers are
shown as `env:NAME`, so the output is safe to paste into an issue.

Regenerate this file after changing the schema:

```bash
python -m testinghq.config_doc
```

## `[targets.<name>]`

Named firing targets. Only a target declared here may be fired at, so this table is the whole of what the tool is allowed to send to. A target is only required once `--send` is passed; a dry run needs no config at all. A target whose host is public (not a reserved test domain, an internal name or a private address, and not resolving only to private addresses) is refused unless the run passes --allow-public-target. The rule is the same for an HTTP url and an SMTP host.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `transport` | string | `"http"` | no | How messages reach the target. 'http': POST each message to `url` in `format`. 'smtp': hand each message to the mail server at `host` in one SMTP transaction (MAIL, RCPT, DATA), the message bytes exactly what the mime format builds. An unknown name is refused at load. sigcheck refuses an smtp target, since a provider signature is an HTTP concept. |
| `url` | string | none | for transport = http | Where to POST. Must start with http:// or https://. A url carrying credentials (user:pass@host) is refused: put them in [targets.<name>.headers] instead. May be overridden per run by the environment variable TESTINGHQ_TARGET_<NAME>_URL, which replaces the URL and nothing else. Refused on an smtp target. |
| `name` | string | none | no | Redundant with the table name and ignored. |
| `format` | string | `"sendgrid"` | no | The wire format every send to this target uses. One of: sendgrid (Inbound Parse, parsed mode), sendgrid-raw (Inbound Parse, raw mode: the full message in the email field), mailgun, postmark, mime (the raw message as message/rfc822). An unknown name is refused at load. Field lists are in docs/FORMATS.md. An smtp target sends mime: absent means mime there, and any other value is refused. |
| `public` | boolean | `false` | no | Read only by barrage. A load test at a public host needs both this and --allow-public-target on the command line. Other tools need only the flag. |
| `host` | string | none | for transport = smtp | The mail server's host name or address, bare (no scheme, no port). Held to the same public-host rule as a url. |
| `port` | integer | `25` | no | The SMTP port. smtp only. |
| `starttls` | boolean | `false` | no | Upgrade the connection with STARTTLS after EHLO, verifying the server's certificate against `host`. A server that does not offer it is a TLS failure. smtp only. |
| `username` | string | none | no | 'env:<VARIABLE>' holding the SMTP AUTH user. With it, the connection logs in after EHLO (and STARTTLS). A literal is refused. smtp only. |
| `password` | string | none | no | 'env:<VARIABLE>' holding the SMTP AUTH password. Needs username. A literal is refused. smtp only. |
| `envelope_from` | string | none | no | MAIL FROM for every message. Absent means each payload's own envelope sender. Must be a synthetic address (a reserved domain or TLD), like every address in a payload. smtp only. |
| `envelope_to` | string or list | none | no | RCPT TO for every message, one address or a list: the mailbox a helpdesk polls, say. Absent means each payload's own envelope recipients. Must be synthetic addresses. On an http target it replaces the envelope recipients in SendGrid's `envelope` field, for a receiver that routes on the envelope, such as Action Mailbox's SendGrid ingress; the To header is left as it is. Only the sendgrid and sendgrid-raw formats carry an envelope, so any other http format refuses it. |

An smtp target's status is the reply code that ended the transaction: 250 after DATA when the message was accepted, or the first 4xx or 5xx reply (to MAIL, RCPT or DATA), whose text is recorded as the error. No reply at all is a transport error, sorted into the same buckets as an HTTP one: refused, dns, timeout, tls or other. Every artifact records the target's `transport` under `config`.

## `[targets.<name>.headers]`

Request headers sent with every HTTP request to the target. Each key is a header name and each value MUST be an `env:NAME` reference, the same rule as `[readback.headers]`. For a target behind HTTP Basic auth, `Authorization = "env:TARGET_AUTH"` with TARGET_AUTH holding `Basic <base64 of user:password>`; `testinghq config basic-auth <user>` prints that value.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `headers` | table | none | no | Every value must be 'env:<VARIABLE>'. A literal is REFUSED when the file loads. A variable that is not set is refused before the run sends anything, and by `config validate`. Content-Type and Content-Length cannot be set: the transport computes them from the body. Not allowed on an smtp target. |

A run artifact records neither the header names nor the values, and `config validate` shows each header as `env:NAME`.

## `[readback]`

Where the pipeline's own output can be read back from. Optional for blast, barrage and compare, which judge a run by the HTTP status. Required for the five pipeline tools: verify, ledger, redeliver, loop and steady. Without a way to ask the system what it made of a message, they can only report what it answered, which is a 200.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `kind` | string | none | yes | 'http' to read a JSON API, 'mailbox' for an append-only JSON Lines mail sink, or 'module:attribute' to import your own adapter. May be given on the command line instead as --readback http\|mailbox\|module:attribute, and the two forms produce the same config. A custom adapter is not checked against the readback host guard: TestingHQ cannot see where arbitrary code connects, so the operator who wrote it decides where it reads from. |
| `url` | string | none | for kind = http | The endpoint asked once per tag per poll: a GET with the tag (or the filled `query` template) in `tag_param`, or a POST of the filled `body`. Gated by the same guardrail as a firing target: a public host is refused unless allow_public_hosts is set, because on a real deployment this is a ticket store that may hold other people's data. Every pagination link is gated too, and one on a different host is refused. |
| `path` | string | none | for kind = mailbox | Path to the JSON Lines sink, relative to the config file's directory. Each poll reads only the bytes appended since the last one; a last line still being written is left for the next poll. A file that does not exist yet reads as empty, and a directory that does not exist is refused before sending. |
| `tag_param` | string | `"tag"` | no | Query parameter carrying the tag, or the filled `query` template. Zendesk and Freshdesk search both take `query`. |
| `query` | string | none | no | A template for the value sent in `tag_param`, with `{tag}` where the tag goes: 'type:ticket tags:{tag}' for Zendesk, '"tag:{tag}"' for Freshdesk. Without it the bare tag is sent. |
| `method` | string | `"GET"` | no | GET or POST. POST sends `body` as JSON, for search APIs such as HubSpot's or Jira's that take the filter in the request body. |
| `body` | string | none | no | The JSON body of a POST lookup, with `{tag}` where the tag goes (JSON-escaped). Only valid with method = "POST". |
| `items_key` | string | `"items"` | no | The dotted path a list of records is nested under. A bare list, or a bare object with an identifier, also work. A wrapper whose list sits under a different key is refused with that key named. |
| `next_path` | string | none | no | JSON pointer (`/links/next`) or dotted path to the next page's url in a response. Pagination stops when it is absent or empty. Not combined with page_param. |
| `page_param` | string | none | no | Query parameter carrying a page number, counted from 1, for APIs paginated by page. Stops at a page smaller than the largest seen, or empty. |
| `page_size_param` | string | none | no | Query parameter carrying the page size (100) alongside page_param. A page with fewer records is the last. |
| `max_pages` | integer | `10` | no | Most pages one lookup or listing follows. A result cut short here is marked partial in the artifact. |
| `timeout` | number | `10.0` | no | Seconds before one request gives up, capped by what is left of --max-wait. |
| `retry_max` | integer | `5` | no | Retries after a 429, a 5xx, a timeout or a connection error, with exponential backoff and jitter, honouring Retry-After. A failure past the last retry stops the run with the reason. |
| `retry_base_seconds` | number | `0.5` | no | The first backoff, doubled per retry, capped at 30 seconds. Requests are also paced to --readback-rate per second (default 4) across lookups, pages and retries. |
| `allow_public_hosts` | boolean | `false` | no | Permits a readback URL on a public host. Setting it here is permanent for the file; --allow-public-readback on the command line is re-decided every run, which is the better habit. |
| `enumerate` | boolean | `true` | no | Also list every record, to look for records that belong to no payload at all. Set false if the API cannot list; the report then says strays were not searched for, rather than claiming there were none. A POST search with no list_path cannot list either. |
| `list_path` | string | none | no | A separate endpoint for the listing, absolute or relative to url. Without it the listing is url with the tag parameter removed and every other parameter kept. Only useful if that endpoint returns full records: the adapter cannot follow a second hop to fetch each one. |
| `spec` | string | none | no | An import path for a custom adapter. Usually given as kind = 'module:attribute' instead, which produces the same thing. A class is instantiated (with the [readback] table if its __init__ takes one argument), a function whose one parameter is named config is called with the same table, an object with fetch is used as is, and any other callable taking one argument is a fetch function. The table holds the adapter's own keys exactly as written (an 'env:NAME' value in them is passed through, not resolved) plus every built-in key as loaded, with [readback.headers] by name only, so a credential is read from the environment by the adapter itself. An import or construction failure is refused before sending. |

A custom adapter that needs a library TestingHQ does not ship, such as a database driver, brings it as its own dependency: installed next to TestingHQ, never added to TestingHQ's. It imports the library inside the code that connects rather than at the top of its module, so a config naming it loads and `config validate` runs without the library, and the adapter's `preflight()` refuses a run that lacks it before anything is sent, naming the package to install. A secret it needs, such as a DSN, is one of its own keys written as `env:NAME`, which reaches it unresolved; it resolves the reference with `testinghq.core.config.resolve_env_references`, so a literal is refused by the same rule as `[readback.headers]`. The import path must be importable: an adapter kept in a checkout rather than installed needs that directory on PYTHONPATH. examples/readback/adapters/osticket_mysql.py is a worked example, reading osTicket's MySQL tables through PyMySQL.

Before anything is sent, the http adapter makes one lookup and, when listing is on, one listing request: a 401 or 403, a 404 list url, a response that is not JSON, or records with no identifier are refused then rather than discovered after the run.

The readback is SETTLED once what it sees (which tags are found, how many records each, and the field values the checks read) has not changed for --quiet-window seconds, counted from the start of the first of two identical complete polls; a tag still not found then is MISSING and is reported as lost. It is UNSETTLED only when --max-wait runs out while that state is still changing, or before every tag could be polled twice, which exits 3 with an UNSETTLED headline naming which and keeps every finding already proven. A run whose every lookup was a 404 is READBACK FAILED (exit 3): the readback url is almost certainly wrong, so no message is reported as lost. For a slow or async pipeline, raise --quiet-window above the pipeline's worst processing latency (a search-indexed store such as Zendesk or Freshdesk can take minutes to index a new ticket), and keep --max-wait well above --quiet-window.

## `[readback.fields]`

How a JSON record maps onto the things TestingHQ checks. Each key is a readback field name and each value is a dotted path into the response. Only map what your API actually returns: an unmapped field is reported as NOT CHECKED with a reason, which is honest. A mapped field your API does not return is also NOT CHECKED, so a typo in a path reads exactly like a field your API lacks.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `ticket_id` | string | none | no | REQUIRED. A record without it is refused, because one message producing two records has to be distinguishable from one producing one, and that is what ledger and redelivery are for. |
| `from_addr` | string | none | no | Normalized before comparison, so a display-name form and a bare address both work. An object with an `email` or `address` key is read through that key. |
| `subject` | string | none | no | Compared by `--subject-match`: `contains` (the default), `suffix` or `exact`. |
| `body` | string | none | no | The payload's substantive text has to APPEAR inside it. A system that reformats the body fails this, which is usually the finding you wanted. Use --body-exact only for a system that stores the field verbatim. |
| `attachment_names` | string | none | no | A list of names. The only field besides references that accepts a list. A list of objects is reported NOT CHECKED with its shape and a hint: map `attachments.*.file_name` to read one key from each. A count is not a name, and is reported NOT CHECKED rather than read as one. |
| `route` | string | none | no | Compared case-insensitively against --expect-route (verify, ledger, redeliver). Without that flag the routing check is SKIPPED with reason 'no expected route'. |
| `message_id` | string | none | no | The RFC 5322 Message-ID, for threading checks. |
| `in_reply_to` | string | none | no | For threading checks. |
| `references` | string | none | no | A chain of Message-IDs, or a space-separated string, both accepted. |
| `tag` | string | none | no | Must resolve to one string. A list holding exactly one string is read as that string; a longer list is reported NOT CHECKED. Usually unnecessary: the lookup already found the record by the tag. |
| `category` | string | none | no | What a classifier decided. No verify check uses it, because the corpus has no triager's label to compare against; it is readable so a tool that does have an expectation can measure it. A check depending on it is SKIPPED with reason 'adapter cannot see field' where the system does not record it. |
| `priority` | string | none | no | As category, for a priority or severity label. |

Dotted paths address nested objects (`data.attributes.subject`), and a numeric segment addresses a list (`attachments.0.name`). One `*` segment collects from every element of a list (`attachments.*.name`); ticket_id cannot use one.

A value of `false` unmaps a default, so a field your API carries under the default name but with a different meaning is NOT CHECKED rather than compared. ticket_id cannot be unmapped.

A field present in a shape the checks cannot read (an object or a list where one value is expected) is NOT CHECKED, and the artifact's `field_notes` says what shape was there.

Omit the whole table and you get exactly the defaults, which assume an API whose fields are named as TestingHQ names them.

## `[readback.headers]`

Request headers for a readback endpoint that needs them. Each key is a header name and each value MUST be an `env:NAME` reference.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `headers` | table | none | no | Every value must be 'env:<VARIABLE>'. A literal is REFUSED, not warned about: a config file is a file that gets shared, and a token in one ends up in a git history, a bug report, or a CI log. A variable that is not set is refused too, and before the run sends anything, rather than at connect time. |

A run artifact records the header NAMES and never the values, so an artifact can be uploaded without carrying a credential.

`config validate` shows resolved headers as `env:NAME` for the same reason, so printing an effective config is safe to paste somewhere.

## `[loop]`

Settings for `loop fire`. A flag given on the command line wins over the value here.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `reply_address` | string | `"no-reply@example.com"` | no | The pipeline's own reply address. The loop bait is an autoresponder message sent to it. --reply-address overrides it. |
| `ticket_policy` | string | `"none"` | no | 'none': a ticket opened for machine mail is a finding. 'allowed': it is reported but not a finding. The answered loop bait is a finding under both. --ticket-policy overrides it. |

## `[loop.outbound]`

Where `loop` reads what the pipeline sent: the same keys as [readback], usually a mailbox sink. Without it the auto-reply check is NOT CHECKED, never passed.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `kind` | string | none | yes, when the table is present | Any [readback] kind. An adapter that can enumerate lets loop see replies that carry no tag; one that cannot qualifies the verdict. |

Every other [readback] key is accepted here with the same meaning.

## `[steady]`

Settings for `steady fire`. A flag wins over the value here.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `label_field` | string | none | no | The readback field carrying the classifier's label: category, route or priority. With neither this nor --label-field, route is read, and a run where route would be one value for every family is refused before sending. |
| `intents_path` | string | none | no | A JSON file of intents to use instead of the shipped fixture, relative to this config file. Shape: {"closings": {lang: [...]}, "intents": [{"id", "category_hint", "subjects": {lang: [...]}, "variants": {lang: body}}]}. Every language with a body needs a subject list. |

## `[sigcheck]`

Settings for `sigcheck fire`. A flag wins over the value here.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `scheme` | string | none | no | mailgun, sendgrid or postmark-basic. Required here or as --scheme. |
| `secret` | string | none | no | 'env:<VARIABLE>' holding the Mailgun signing key, the SendGrid ECDSA private key, or Postmark's user:password. A literal is refused. The artifact records the reference, never the value. |
| `timestamp_window_seconds` | number | `300.0` | no | How old a signed timestamp the endpoint is expected to refuse. The stale-timestamp variant is signed just outside it. |
