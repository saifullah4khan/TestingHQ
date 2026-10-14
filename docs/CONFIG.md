<!-- GENERATED FILE. Do not edit by hand. -->
<!-- Produced by `python -m testinghq.config_doc`, from the schema in -->
<!-- testinghq/core/config_schema.py. tests/unit/test_config_schema.py fails -->
<!-- when this file does not match what that script would write, so the only -->
<!-- way to change it is to change the schema, which is the point. -->

# Configuration reference

The tables below, generated from the code so they cannot drift. A key added to
a loader without a line here fails the suite.

**Scope, because a generated reference that claims more than it covers is worse
than one that does not.** This covers `[targets.<name>]`, `[readback]`,
`[readback.fields]` and `[readback.headers]`, which is every table the
`core.config` loader reads. It does not cover `[loop]` or `[loop.outbound]`,
which `testinghq loop fire` reads from the same file: those keys are
`reply_address` and `ticket_policy`, and the outbound table is
`[readback]`-shaped. They are absent from the schema rather than silently
misdescribed here.

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

Named firing targets. Only a target declared here may be fired at: `core.guardrails.require_configured_target` refuses any name not present, so this table is the whole of what the tool is allowed to send to. A target is only required once `--send` is passed; a dry run needs no config at all.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `url` | string | none | yes | Where to POST. Must start with http:// or https://. May be overridden per run by the environment variable TESTINGHQ_TARGET_<NAME>_URL, which replaces the URL and nothing else. |
| `name` | string | none | no | Redundant with the table name and ignored if it disagrees. Present because the earlier example config had it and removing it would have broken a file people had copied. |
| `format` | string | `"sendgrid"` | no | Which wire format to serialize this target's messages in. One of: sendgrid (multipart/form-data, the SendGrid Inbound Parse shape), mailgun, postmark, mime. Absent means sendgrid, so a config written before formats were selectable is unchanged, and an unknown name is refused at load with the valid ones. KNOWN LIMITATION: the value is validated and stored, and no send path reads it, so the wire bytes are SendGrid-shaped whatever it says. The encoders themselves exist and are tested; threading the key through to the transport is the outstanding work. Selecting a different format today validates and then has no effect. |

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
| `spec` | string | none | no | An import path for a custom adapter. Usually given as kind = 'module:attribute' instead, which produces the same thing. A class is instantiated (with the [readback] table if its __init__ takes one argument), an object with fetch is used as is, and a callable taking one argument is a fetch function. An import or construction failure is refused before sending. |

Before anything is sent, the http adapter makes one lookup and, when listing is on, one listing request: a 401 or 403, a 404 list url, a response that is not JSON, or records with no identifier are refused then rather than discovered after the run.

A run whose readback has not settled within --max-wait is UNSETTLED, not lost. For a pipeline that writes on a queue consumer, make --quiet-window longer than the gap between a record being created and being filled in; for a search-indexed store (Zendesk, Freshdesk) start with --max-wait 300.

## `[readback.fields]`

How a JSON record maps onto the things TestingHQ checks. Each key is a readback field name and each value is a dotted path into the response. Only map what your API actually returns: an unmapped field is reported as NOT CHECKED with a reason, which is honest. A mapped field your API does not return is also NOT CHECKED, so a typo in a path reads exactly like a field your API lacks.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `ticket_id` | string | none | no | REQUIRED. A record without it is refused, because one message producing two records has to be distinguishable from one producing one, and that is what ledger and redelivery are for. |
| `from_addr` | string | none | no | Normalized before comparison, so a display-name form and a bare address both work. An object with an `email` or `address` key is read through that key. |
| `subject` | string | none | no | Compared case- and whitespace-insensitively. |
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
