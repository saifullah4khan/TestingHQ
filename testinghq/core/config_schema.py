"""One description of the config file, for the validator and the reference doc.

`testinghq/core/config.py` and `testinghq/pipeline/adapters.py` between them
accept about thirty keys across `[targets.*]`, `[readback]`, `[readback.fields]`
and `[readback.headers]`. That surface was documented in prose, in a comment
block inside an example TOML, which is exactly the kind of documentation that is
right until someone adds a key and forgets.

This module is the single description. `testinghq config validate` uses it to
render the effective config, and `docs/CONFIG.md` is generated from it, with a
test that fails when the checked-in file no longer matches.

THE IMPORTANT DIVISION OF LABOUR, because it is easy to get backwards.
Validation of VALUES does NOT consult this schema. It calls the real loaders,
`load_config` and `parse_readback_config`, because those are what will actually
run, and a validator that checked the file against a description of the file
would happily pass a config the tools then refuse.

Key NAMES are the one thing checked against this schema: a key no table here
declares is refused at load (`core.config.check_known_keys`), so a typo such
as `formt = "mailgun"` fails instead of silently running with the default.
That makes the schema a whitelist as well as documentation, so a key a loader
reads but the schema omits would be refused, and the tests below catch it.

So this schema is documentation, rendering and the key whitelist, and the
anti-drift guarantee comes from elsewhere: `tests/unit/test_config_schema.py`
asserts every default declared here equals the real default in the dataclass,
and that every key the loaders accept is described. If the two disagree, a
test fails. That is the only arrangement in which a generated doc can be
trusted, because the thing being generated from is checked against the thing
that actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

#: Values that mean "not given", rendered as such rather than as a literal, so
#: the generated doc and `config validate` agree on how absence is spelled.
UNSET = None


@dataclass(frozen=True)
class Key:
    """One configuration key.

    `required` is about the table as a whole: `url` is required for an http
    readback and meaningless for a mailbox one, so the note says which kind
    rather than claiming a blanket requirement that would be wrong half the time.
    """

    name: str
    type: str
    default: Optional[str] = None
    required: str = "no"
    meaning: str = ""
    #: True for keys whose value must be an `env:NAME` reference. The validator
    #: resolves these and renders the NAME, never the value.
    env_referenced: bool = False
    #: True for the free-form mapping tables, whose keys are data rather than a
    #: fixed set: `[readback.fields]` names readback fields, `[readback.headers]`
    #: names header names. Rendered as prose rather than as a table of rows,
    #: because a table of rows for an open-ended map is a list that is wrong the
    #: moment someone adds one.
    free_form: bool = False


@dataclass(frozen=True)
class Table:
    """One TOML table."""

    name: str
    summary: str
    keys: Tuple[Key, ...]
    notes: Tuple[str, ...] = ()
    #: True when the table name carries a user-chosen suffix, as `[targets.*]`.
    patterned: bool = False
    #: True for a table whose keys are data rather than a fixed set, such as
    #: `[readback.fields]`. Rendered as prose above a table of the names it may
    #: hold, because a table of rows for an open-ended map is a list that is
    #: wrong the moment someone adds one.
    free_form: bool = False
    #: The name of another table whose keys and sub-tables this one also
    #: accepts: `[loop.outbound]` takes every `[readback]` key.
    inherits: Optional[str] = None
    #: True for a table that configures a readback adapter. When its `kind`
    #: (or `spec`) names a custom `module:attribute` adapter, keys this schema
    #: does not declare are allowed, because they may be that adapter's own and
    #: the schema cannot know them. With a built-in kind they are refused.
    custom_adapter_keys: bool = False


# ---------------------------------------------------------------------------
# [targets.<name>]
# ---------------------------------------------------------------------------

TARGETS = Table(
    name="targets",
    patterned=True,
    summary=(
        "Named firing targets. Only a target declared here may be fired at, so "
        "this table is the whole of what the tool is allowed to send to. A "
        "target is only required once `--send` is passed; a dry run needs no "
        "config at all. A target whose host is public (not a reserved test "
        "domain, an internal name or a private address, and not resolving only "
        "to private addresses) is refused unless the run passes "
        "--allow-public-target."
    ),
    keys=(
        Key(
            "url",
            "string",
            required="yes",
            meaning=(
                "Where to POST. Must start with http:// or https://. May be "
                "overridden per run by the environment variable "
                "TESTINGHQ_TARGET_<NAME>_URL, which replaces the URL and "
                "nothing else."
            ),
        ),
        Key(
            "name",
            "string",
            required="no",
            meaning="Redundant with the table name and ignored.",
        ),
        Key(
            "format",
            "string",
            default='"sendgrid"',
            meaning=(
                "The wire format every send to this target uses. One of: "
                "sendgrid (Inbound Parse, parsed mode), sendgrid-raw (Inbound "
                "Parse, raw mode: the full message in the email field), "
                "mailgun, postmark, mime (the raw message as message/rfc822). "
                "An unknown name is refused at load. Field lists are in "
                "docs/FORMATS.md."
            ),
        ),
        Key(
            "public",
            "boolean",
            default="false",
            meaning=(
                "Read only by barrage. A load test at a public host needs both "
                "this and --allow-public-target on the command line. Other tools "
                "need only the flag."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# [readback]
# ---------------------------------------------------------------------------

READBACK = Table(
    name="readback",
    custom_adapter_keys=True,
    summary=(
        "Where the pipeline's own output can be read back from. Optional for "
        "blast, barrage and compare, which judge a run by the HTTP status. "
        "Required for the five pipeline tools: verify, ledger, redeliver, loop "
        "and steady. Without a way to ask the system what it made of a message, "
        "they can only report what it answered, which is a 200."
    ),
    keys=(
        Key(
            "kind",
            "string",
            required="yes",
            meaning=(
                "'http' to read a JSON API, 'mailbox' for an append-only JSON "
                "Lines mail sink, or 'module:attribute' to import your own "
                "adapter. May be given on the command line instead as "
                "--readback http|mailbox|module:attribute, and the two forms "
                "produce the same config. A custom adapter is not checked "
                "against the readback host guard: TestingHQ cannot see where "
                "arbitrary code connects, so the operator who wrote it decides "
                "where it reads from."
            ),
        ),
        Key(
            "url",
            "string",
            required="for kind = http",
            meaning=(
                "The endpoint asked once per tag per poll: a GET with the tag "
                "(or the filled `query` template) in `tag_param`, or a POST of "
                "the filled `body`. Gated by the same guardrail as a firing "
                "target: a public host is refused unless allow_public_hosts is "
                "set, because on a real deployment this is a ticket store that "
                "may hold other people's data. Every pagination link is gated "
                "too, and one on a different host is refused."
            ),
        ),
        Key(
            "path",
            "string",
            required="for kind = mailbox",
            meaning=(
                "Path to the JSON Lines sink, relative to the config file's "
                "directory. Each poll reads only the bytes appended since the "
                "last one; a last line still being written is left for the next "
                "poll. A file that does not exist yet reads as empty, and a "
                "directory that does not exist is refused before sending."
            ),
        ),
        Key(
            "tag_param",
            "string",
            default='"tag"',
            meaning=(
                "Query parameter carrying the tag, or the filled `query` "
                "template. Zendesk and Freshdesk search both take `query`."
            ),
        ),
        Key(
            "query",
            "string",
            meaning=(
                "A template for the value sent in `tag_param`, with `{tag}` "
                "where the tag goes: 'type:ticket tags:{tag}' for Zendesk, "
                "'\"tag:{tag}\"' for Freshdesk. Without it the bare tag is sent."
            ),
        ),
        Key(
            "method",
            "string",
            default='"GET"',
            meaning=(
                "GET or POST. POST sends `body` as JSON, for search APIs such as "
                "HubSpot's or Jira's that take the filter in the request body."
            ),
        ),
        Key(
            "body",
            "string",
            meaning=(
                "The JSON body of a POST lookup, with `{tag}` where the tag "
                "goes (JSON-escaped). Only valid with method = \"POST\"."
            ),
        ),
        Key(
            "items_key",
            "string",
            default='"items"',
            meaning=(
                "The dotted path a list of records is nested under. A bare list, "
                "or a bare object with an identifier, also work. A wrapper whose "
                "list sits under a different key is refused with that key named."
            ),
        ),
        Key(
            "next_path",
            "string",
            meaning=(
                "JSON pointer (`/links/next`) or dotted path to the next page's "
                "url in a response. Pagination stops when it is absent or empty. "
                "Not combined with page_param."
            ),
        ),
        Key(
            "page_param",
            "string",
            meaning=(
                "Query parameter carrying a page number, counted from 1, for "
                "APIs paginated by page. Stops at a page smaller than the "
                "largest seen, or empty."
            ),
        ),
        Key(
            "page_size_param",
            "string",
            meaning=(
                "Query parameter carrying the page size (100) alongside "
                "page_param. A page with fewer records is the last."
            ),
        ),
        Key(
            "max_pages",
            "integer",
            default="10",
            meaning=(
                "Most pages one lookup or listing follows. A result cut short "
                "here is marked partial in the artifact."
            ),
        ),
        Key(
            "timeout",
            "number",
            default="10.0",
            meaning=(
                "Seconds before one request gives up, capped by what is left "
                "of --max-wait."
            ),
        ),
        Key(
            "retry_max",
            "integer",
            default="5",
            meaning=(
                "Retries after a 429, a 5xx, a timeout or a connection error, "
                "with exponential backoff and jitter, honouring Retry-After. "
                "A failure past the last retry stops the run with the reason."
            ),
        ),
        Key(
            "retry_base_seconds",
            "number",
            default="0.5",
            meaning=(
                "The first backoff, doubled per retry, capped at 30 seconds. "
                "Requests are also paced to --readback-rate per second (default "
                "4) across lookups, pages and retries."
            ),
        ),
        Key(
            "allow_public_hosts",
            "boolean",
            default="false",
            meaning=(
                "Permits a readback URL on a public host. Setting it here is "
                "permanent for the file; --allow-public-readback on the command "
                "line is re-decided every run, which is the better habit."
            ),
        ),
        Key(
            "enumerate",
            "boolean",
            default="true",
            meaning=(
                "Also list every record, to look for records that belong to no "
                "payload at all. Set false if the API cannot list; the report "
                "then says strays were not searched for, rather than claiming "
                "there were none. A POST search with no list_path cannot list "
                "either."
            ),
        ),
        Key(
            "list_path",
            "string",
            meaning=(
                "A separate endpoint for the listing, absolute or relative to "
                "url. Without it the listing is url with the tag parameter "
                "removed and every other parameter kept. Only useful if that "
                "endpoint returns full records: the adapter cannot follow a "
                "second hop to fetch each one."
            ),
        ),
        Key(
            "spec",
            "string",
            meaning=(
                "An import path for a custom adapter. Usually given as "
                "kind = 'module:attribute' instead, which produces the same "
                "thing. A class is instantiated (with the [readback] table if "
                "its __init__ takes one argument), a function whose one "
                "parameter is named config is called with the same table, an "
                "object with fetch is used as is, and any other callable taking "
                "one argument is a fetch function. The table holds the "
                "adapter's own keys exactly as written (an 'env:NAME' value in "
                "them is passed through, not resolved) plus every built-in key "
                "as loaded, with [readback.headers] by name only, so a "
                "credential is read from the environment by the adapter "
                "itself. An import or construction failure is refused before "
                "sending."
            ),
        ),
    ),
    notes=(
        "Before anything is sent, the http adapter makes one lookup and, when "
        "listing is on, one listing request: a 401 or 403, a 404 list url, a "
        "response that is not JSON, or records with no identifier are refused "
        "then rather than discovered after the run.",
        # The same paragraph as pipeline.common.SETTLING_RULE, which the
        # --quiet-window/--max-wait help prints. Copied rather than imported so
        # this schema keeps no dependency on the pipeline package.
        "The readback is SETTLED once what it sees (which tags are found, how "
        "many records each, and the field values the checks read) has not "
        "changed for --quiet-window seconds, counted from the start of the "
        "first of two identical complete polls; a tag still not found then is "
        "MISSING and is reported as lost. It is UNSETTLED only when --max-wait "
        "runs out while that state is still changing, or before every tag could "
        "be polled twice, which exits 3 with an UNSETTLED headline naming which "
        "and keeps every finding already proven. A run whose every lookup was a "
        "404 is READBACK FAILED (exit 3): the readback url is almost certainly "
        "wrong, so no message is reported as lost. For a slow "
        "or async pipeline, raise --quiet-window above the pipeline's worst "
        "processing latency (a search-indexed store such as Zendesk or "
        "Freshdesk can take minutes to index a new ticket), and keep --max-wait "
        "well above --quiet-window.",
    ),
)


# ---------------------------------------------------------------------------
# The two free-form tables
# ---------------------------------------------------------------------------

READBACK_FIELDS = Table(
    name="readback.fields",
    free_form=True,
    summary=(
        "How a JSON record maps onto the things TestingHQ checks. Each key is "
        "a readback field name and each value is a dotted path into the "
        "response. Only map what your API actually returns: an unmapped field "
        "is reported as NOT CHECKED with a reason, which is honest. A mapped "
        "field your API does not return is also NOT CHECKED, so a typo in a "
        "path reads exactly like a field your API lacks."
    ),
    keys=(
        Key(
            "ticket_id", "string", free_form=True,
            meaning=(
                "REQUIRED. A record without it is refused, because one message "
                "producing two records has to be distinguishable from one "
                "producing one, and that is what ledger and redelivery are for."
            ),
        ),
        Key("from_addr", "string", free_form=True,
             meaning=(
                 "Normalized before comparison, so a display-name form and a "
                 "bare address both work. An object with an `email` or "
                 "`address` key is read through that key."
             )),
        Key("subject", "string", free_form=True,
             meaning="Compared by `--subject-match`: `contains` (the default), `suffix` or `exact`."),
        Key("body", "string", free_form=True,
             meaning=(
                 "The payload's substantive text has to APPEAR inside it. A "
                 "system that reformats the body fails this, which is usually "
                 "the finding you wanted. Use --body-exact only for a system "
                 "that stores the field verbatim."
             )),
        Key("attachment_names", "string", free_form=True,
             meaning=(
                 "A list of names. The only field besides references that "
                 "accepts a list. A list of objects is reported NOT CHECKED "
                 "with its shape and a hint: map `attachments.*.file_name` to "
                 "read one key from each. A count is not a name, and is "
                 "reported NOT CHECKED rather than read as one."
             )),
        Key("route", "string", free_form=True,
             meaning=(
                 "Compared case-insensitively against --expect-route (verify, "
                 "ledger, redeliver). Without that flag the routing check is "
                 "SKIPPED with reason 'no expected route'."
             )),
        Key("message_id", "string", free_form=True,
             meaning="The RFC 5322 Message-ID, for threading checks."),
        Key("in_reply_to", "string", free_form=True, meaning="For threading checks."),
        Key("references", "string", free_form=True,
             meaning="A chain of Message-IDs, or a space-separated string, both accepted."),
        Key("tag", "string", free_form=True,
             meaning=(
                 "Must resolve to one string. A list holding exactly one string "
                 "is read as that string; a longer list is reported NOT CHECKED. "
                 "Usually unnecessary: the lookup already found the record by "
                 "the tag."
             )),
        Key("category", "string", free_form=True,
             meaning=(
                 "What a classifier decided. No verify check uses it, because "
                 "the corpus has no triager's label to compare against; it is "
                 "readable so a tool that does have an expectation can measure "
                 "it. A check depending on it is SKIPPED with reason "
                 "'adapter cannot see field' where the system does not record it."
             )),
        Key("priority", "string", free_form=True,
             meaning="As category, for a priority or severity label."),
    ),
    notes=(
        "Dotted paths address nested objects (`data.attributes.subject`), and a "
        "numeric segment addresses a list (`attachments.0.name`). One `*` "
        "segment collects from every element of a list (`attachments.*.name`); "
        "ticket_id cannot use one.",
        "A value of `false` unmaps a default, so a field your API carries under "
        "the default name but with a different meaning is NOT CHECKED rather "
        "than compared. ticket_id cannot be unmapped.",
        "A field present in a shape the checks cannot read (an object or a list "
        "where one value is expected) is NOT CHECKED, and the artifact's "
        "`field_notes` says what shape was there.",
        "Omit the whole table and you get exactly the defaults, which assume an "
        "API whose fields are named as TestingHQ names them.",
    ),
)


READBACK_HEADERS = Table(
    name="readback.headers",
    free_form=True,
    summary=(
        "Request headers for a readback endpoint that needs them. Each key is a "
        "header name and each value MUST be an `env:NAME` reference."
    ),
    keys=(
        Key(
            "headers", "table", free_form=True, env_referenced=True,
            meaning=(
                "Every value must be 'env:<VARIABLE>'. A literal is REFUSED, "
                "not warned about: a config file is a file that gets shared, "
                "and a token in one ends up in a git history, a bug report, or "
                "a CI log. A variable that is not set is refused too, and "
                "before the run sends anything, rather than at connect time."
            ),
        ),
    ),
    notes=(
        "A run artifact records the header NAMES and never the values, so an "
        "artifact can be uploaded without carrying a credential.",
        "`config validate` shows resolved headers as `env:NAME` for the same "
        "reason, so printing an effective config is safe to paste somewhere.",
    ),
)


# ---------------------------------------------------------------------------
# [loop], [loop.outbound], [steady], [sigcheck]: read by the tools themselves
# ---------------------------------------------------------------------------

LOOP = Table(
    name="loop",
    summary=(
        "Settings for `loop fire`. A flag given on the command line wins over "
        "the value here."
    ),
    keys=(
        Key(
            "reply_address", "string", default='"no-reply@example.com"',
            meaning=(
                "The pipeline's own reply address. The loop bait is an "
                "autoresponder message sent to it. --reply-address overrides it."
            ),
        ),
        Key(
            "ticket_policy", "string", default='"none"',
            meaning=(
                "'none': a ticket opened for machine mail is a finding. "
                "'allowed': it is reported but not a finding. The answered loop "
                "bait is a finding under both. --ticket-policy overrides it."
            ),
        ),
    ),
)

LOOP_OUTBOUND = Table(
    name="loop.outbound",
    inherits="readback",
    summary=(
        "Where `loop` reads what the pipeline sent: the same keys as "
        "[readback], usually a mailbox sink. Without it the auto-reply check is "
        "NOT CHECKED, never passed."
    ),
    keys=(
        Key(
            "kind", "string", required="yes, when the table is present",
            meaning=(
                "Any [readback] kind. An adapter that can enumerate lets loop "
                "see replies that carry no tag; one that cannot qualifies the "
                "verdict."
            ),
        ),
    ),
    notes=("Every other [readback] key is accepted here with the same meaning.",),
)

STEADY = Table(
    name="steady",
    summary="Settings for `steady fire`. A flag wins over the value here.",
    keys=(
        Key(
            "label_field", "string", default=UNSET,
            meaning=(
                "The readback field carrying the classifier's label: category, "
                "route or priority. With neither this nor --label-field, route "
                "is read, and a run where route would be one value for every "
                "family is refused before sending."
            ),
        ),
        Key(
            "intents_path", "string", default=UNSET,
            meaning=(
                "A JSON file of intents to use instead of the shipped fixture, "
                "relative to this config file. Shape: {\"closings\": {lang: "
                "[...]}, \"intents\": [{\"id\", \"category_hint\", "
                "\"subjects\": {lang: [...]}, \"variants\": {lang: body}}]}. "
                "Every language with a body needs a subject list."
            ),
        ),
    ),
)

SIGCHECK = Table(
    name="sigcheck",
    summary="Settings for `sigcheck fire`. A flag wins over the value here.",
    keys=(
        Key(
            "scheme", "string", default=UNSET,
            meaning="mailgun, sendgrid or postmark-basic. Required here or as --scheme.",
        ),
        Key(
            "secret", "string", default=UNSET, env_referenced=True,
            meaning=(
                "'env:<VARIABLE>' holding the Mailgun signing key, the SendGrid "
                "ECDSA private key, or Postmark's user:password. A literal is "
                "refused. The artifact records the reference, never the value."
            ),
        ),
        Key(
            "timestamp_window_seconds", "number", default="300.0",
            meaning=(
                "How old a signed timestamp the endpoint is expected to refuse. "
                "The stale-timestamp variant is signed just outside it."
            ),
        ),
    ),
)


TABLES: Tuple[Table, ...] = (
    TARGETS, READBACK, READBACK_FIELDS, READBACK_HEADERS,
    LOOP, LOOP_OUTBOUND, STEADY, SIGCHECK,
)


def table_named(name: str) -> Optional[Table]:
    for table in TABLES:
        if table.name == name:
            return table
    return None


def all_key_names() -> List[str]:
    return [key.name for table in TABLES for key in table.keys]


def top_level_names() -> List[str]:
    """The top-level tables a config file may hold: `targets`, `readback`,
    `loop`, `steady`, `sigcheck`. Nothing else reads the file, so nothing else
    is allowed in it."""
    names: List[str] = []
    for table in TABLES:
        head = table.name.split(".", 1)[0]
        if head not in names:
            names.append(head)
    return names


def child_table(table: Table, key: str) -> Optional[Table]:
    """The sub-table `key` of `table` (`fields` under `readback`), looking
    through `inherits`, or None when `key` names no sub-table."""
    found = table_named(f"{table.name}.{key}")
    if found is None and table.inherits:
        found = table_named(f"{table.inherits}.{key}")
    return found


def known_keys(table: Table) -> List[str]:
    """Every key `table` may hold, sorted: its declared keys, those of the
    table it inherits from, and the names of its sub-tables."""
    sources = [table]
    parent = table_named(table.inherits) if table.inherits else None
    if parent is not None:
        sources.append(parent)
    names = {key.name for source in sources for key in source.keys}
    for source in sources:
        prefix = source.name + "."
        for other in TABLES:
            if other.name.startswith(prefix) and "." not in other.name[len(prefix):]:
                names.add(other.name[len(prefix):])
    return sorted(names)


def accepts_custom_adapter_keys(table: Table) -> bool:
    """True when `table`, or the table it inherits from, configures a
    readback adapter and so may carry a custom adapter's own keys."""
    if table.custom_adapter_keys:
        return True
    parent = table_named(table.inherits) if table.inherits else None
    return bool(parent is not None and parent.custom_adapter_keys)
