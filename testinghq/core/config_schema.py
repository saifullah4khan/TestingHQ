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
Validation does NOT consult this schema. It calls the real loaders,
`load_config` and `parse_readback_config`, because those are what will actually
run, and a validator that checked the file against a description of the file
would happily pass a config the tools then refuse.

So this schema is documentation and rendering, and the anti-drift guarantee
comes from elsewhere: `tests/unit/test_config_schema.py` asserts every default
declared here equals the real default in the dataclass, and that every key the
loaders accept is described. If the two disagree, a test fails. That is the
only arrangement in which a generated doc can be trusted, because the thing
being generated from is checked against the thing that actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# [targets.<name>]
# ---------------------------------------------------------------------------

TARGETS = Table(
    name="targets",
    patterned=True,
    summary=(
        "Named firing targets. Only a target declared here may be fired at: "
        "`core.guardrails.require_configured_target` refuses any name not "
        "present, so this table is the whole of what the tool is allowed to "
        "send to. A target is only required once `--send` is passed; a dry run "
        "needs no config at all."
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
            meaning=(
                "Redundant with the table name and ignored if it disagrees. "
                "Present because the earlier example config had it and removing "
                "it would have broken a file people had copied."
            ),
        ),
        Key(
            "format",
            "string",
            default='"sendgrid"',
            meaning=(
                "Which wire format to serialize this target's messages in. One "
                "of: sendgrid (multipart/form-data, the SendGrid Inbound Parse "
                "shape), mailgun, postmark, mime. Absent means sendgrid, so a "
                "config written before formats were selectable is unchanged, "
                "and an unknown name is refused at load with the valid ones. "
                "KNOWN LIMITATION: the value is validated and stored, and no "
                "send path reads it, so the wire bytes are SendGrid-shaped "
                "whatever it says. The encoders themselves exist and are "
                "tested; threading the key through to the transport is the "
                "outstanding work. Selecting a different format today validates "
                "and then has no effect."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# [readback]
# ---------------------------------------------------------------------------

READBACK = Table(
    name="readback",
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
                "produce the same config."
            ),
        ),
        Key(
            "url",
            "string",
            required="for kind = http",
            meaning=(
                "The endpoint queried once per payload, with the tag appended "
                "as a query parameter. Gated by the same guardrail as a firing "
                "target: a public host is refused unless "
                "allow_public_hosts is set, because on a real deployment this "
                "is a ticket store that may hold other people's data."
            ),
        ),
        Key(
            "path",
            "string",
            required="for kind = mailbox",
            meaning=(
                "Path to the JSON Lines sink. Re-read on every lookup rather "
                "than cached, because the pipeline is still writing to it while "
                "the run is in flight. A file that does not exist yet reads as "
                "empty rather than as an error."
            ),
        ),
        Key(
            "tag_param",
            "string",
            default='"tag"',
            meaning=(
                "Query parameter carrying the tag, for APIs that expect a "
                "different name. Set it to 'query' for Zendesk and Freshdesk, "
                "whose search endpoints both take one."
            ),
        ),
        Key(
            "items_key",
            "string",
            default='"items"',
            meaning=(
                "The key a list of records is nested under. A bare list, or a "
                "bare object, also work, so this is only needed when the "
                "response wraps one."
            ),
        ),
        Key(
            "timeout",
            "number",
            default="10.0",
            meaning="Seconds before a lookup gives up.",
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
                "Also call the url with no tag, to look for records that belong "
                "to no payload at all. Set false if the API cannot list without "
                "a tag; the report will then say strays were not searched for, "
                "rather than claiming there were none."
            ),
        ),
        Key(
            "list_path",
            "string",
            meaning=(
                "A separate endpoint for the listing, when it is not the same "
                "one as the single lookup. Only useful if that endpoint returns "
                "full records: the adapter cannot follow a second hop to fetch "
                "each one."
            ),
        ),
        Key(
            "spec",
            "string",
            meaning=(
                "An import path for a custom adapter. Usually given as "
                "kind = 'module:attribute' instead, which produces the same "
                "thing."
            ),
        ),
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
             meaning="Normalized before comparison, so a display-name form and a bare address both work."),
        Key("subject", "string", free_form=True,
             meaning="Compared case- and whitespace-insensitively."),
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
                 "accepts a list. A count is not a name: mapping an "
                 "attachments_count field here raises rather than reading 1 as "
                 "a filename."
             )),
        Key("route", "string", free_form=True,
             meaning="Compared case-insensitively. Without --expect-route the expected value is the message's own recipient address."),
        Key("message_id", "string", free_form=True,
             meaning="The RFC 5322 Message-ID, for threading checks."),
        Key("in_reply_to", "string", free_form=True, meaning="For threading checks."),
        Key("references", "string", free_form=True,
             meaning="A chain of Message-IDs, or a space-separated string, both accepted."),
        Key("tag", "string", free_form=True,
             meaning=(
                 "Must resolve to a STRING. A ticket's tags field is a list, "
                 "so mapping tag to it is refused at load. Usually unnecessary: "
                 "the lookup already found the record by the tag."
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
        "numeric segment addresses a list (`attachments.0.name`).",
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


TABLES: Tuple[Table, ...] = (TARGETS, READBACK, READBACK_FIELDS, READBACK_HEADERS)


def table_named(name: str) -> Optional[Table]:
    for table in TABLES:
        if table.name == name:
            return table
    return None


def all_key_names() -> List[str]:
    return [key.name for table in TABLES for key in table.keys]
