<!-- GENERATED FILE. Do not edit by hand. -->
<!-- Produced by `python -m testinghq.config_doc`, from the schema in -->
<!-- testinghq/core/config_schema.py. tests/unit/test_config_schema.py fails -->
<!-- when this file does not match what that script would write, so the only -->
<!-- way to change it is to change the schema, which is the point. -->

# Configuration reference

Every table TestingHQ reads from a config file, generated from the code so it
cannot drift. A key added to a loader without a line here fails the suite.

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
| `format` | string | `"sendgrid"` | no | Which wire format to serialize this target's messages in. One of: sendgrid (multipart/form-data, the SendGrid Inbound Parse shape), mailgun, postmark, mime. Absent means sendgrid, so a config written before formats were selectable is unchanged. An unknown name is refused at load, with the valid names. |

## `[readback]`

Where the pipeline's own output can be read back from. Optional for blast, barrage and compare, which judge a run by the HTTP status. Required for verify, ledger and redeliver: without a way to ask the system what it made of a message, they can only report what it answered, which is a 200.

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `kind` | string | none | yes | 'http' to read a JSON API, 'mailbox' for an append-only JSON Lines mail sink, or 'module:attribute' to import your own adapter. May be given on the command line instead as --readback http\|mailbox\|module:attribute, and the two forms produce the same config. |
| `url` | string | none | for kind = http | The endpoint queried once per payload, with the tag appended as a query parameter. Gated by the same guardrail as a firing target: a public host is refused unless allow_public_hosts is set, because on a real deployment this is a ticket store that may hold other people's data. |
| `path` | string | none | for kind = mailbox | Path to the JSON Lines sink. Re-read on every lookup rather than cached, because the pipeline is still writing to it while the run is in flight. A file that does not exist yet reads as empty rather than as an error. |
| `tag_param` | string | `"tag"` | no | Query parameter carrying the tag, for APIs that expect a different name. Set it to 'query' for Zendesk and Freshdesk, whose search endpoints both take one. |
| `items_key` | string | `"items"` | no | The key a list of records is nested under. A bare list, or a bare object, also work, so this is only needed when the response wraps one. |
| `timeout` | number | `10.0` | no | Seconds before a lookup gives up. |
| `allow_public_hosts` | boolean | `false` | no | Permits a readback URL on a public host. Setting it here is permanent for the file; --allow-public-readback on the command line is re-decided every run, which is the better habit. |
| `enumerate` | boolean | `true` | no | Also call the url with no tag, to look for records that belong to no payload at all. Set false if the API cannot list without a tag; the report will then say strays were not searched for, rather than claiming there were none. |
| `list_path` | string | none | no | A separate endpoint for the listing, when it is not the same one as the single lookup. Only useful if that endpoint returns full records: the adapter cannot follow a second hop to fetch each one. |
| `spec` | string | none | no | An import path for a custom adapter. Usually given as kind = 'module:attribute' instead, which produces the same thing. |

## `[readback.fields]`

How a JSON record maps onto the things TestingHQ checks. Each key is a readback field name and each value is a dotted path into the response. Only map what your API actually returns: an unmapped field is reported as NOT CHECKED with a reason, which is honest. A mapped field your API does not return is also NOT CHECKED, so a typo in a path reads exactly like a field your API lacks.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `ticket_id` | string | none | no | REQUIRED. A record without it is refused, because one message producing two records has to be distinguishable from one producing one, and that is what ledger and redelivery are for. |
| `from_addr` | string | none | no | Normalized before comparison, so a display-name form and a bare address both work. |
| `subject` | string | none | no | Compared case- and whitespace-insensitively. |
| `body` | string | none | no | The payload's substantive text has to APPEAR inside it. A system that reformats the body fails this, which is usually the finding you wanted. Use --body-exact only for a system that stores the field verbatim. |
| `attachment_names` | string | none | no | A list of names. The only field besides references that accepts a list. A count is not a name: mapping an attachments_count field here raises rather than reading 1 as a filename. |
| `route` | string | none | no | Compared case-insensitively. Without --expect-route the expected value is the message's own recipient address. |
| `message_id` | string | none | no | The RFC 5322 Message-ID, for threading checks. |
| `in_reply_to` | string | none | no | For threading checks. |
| `references` | string | none | no | A chain of Message-IDs, or a space-separated string, both accepted. |
| `tag` | string | none | no | Must resolve to a STRING. A ticket's tags field is a list, so mapping tag to it is refused at load. Usually unnecessary: the lookup already found the record by the tag. |
| `category` | string | none | no | What a classifier decided. No verify check uses it, because the corpus has no triager's label to compare against; it is readable so a tool that does have an expectation can measure it. A check depending on it is SKIPPED with reason 'adapter cannot see field' where the system does not record it. |
| `priority` | string | none | no | As category, for a priority or severity label. |

Dotted paths address nested objects (`data.attributes.subject`), and a numeric segment addresses a list (`attachments.0.name`).

Omit the whole table and you get exactly the defaults, which assume an API whose fields are named as TestingHQ names them.

## `[readback.headers]`

Request headers for a readback endpoint that needs them. Each key is a header name and each value MUST be an `env:NAME` reference.

Keys are data rather than a fixed set. The fields or headers this table may name are:

| Key | Type | Default | Required | Meaning |
| --- | --- | --- | --- | --- |
| `headers` | table | none | no | Every value must be 'env:<VARIABLE>'. A literal is REFUSED, not warned about: a config file is a file that gets shared, and a token in one ends up in a git history, a bug report, or a CI log. A variable that is not set is refused too, and before the run sends anything, rather than at connect time. |

A run artifact records the header NAMES and never the values, so an artifact can be uploaded without carrying a credential.

`config validate` shows resolved headers as `env:NAME` for the same reason, so printing an effective config is safe to paste somewhere.
