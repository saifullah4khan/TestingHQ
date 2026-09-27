"""The readback recipes, loaded with the real parser, and their claims checked.

A recipe that does not parse is a broken example. A recipe that parses but maps
a field to a path the API does not have is worse: it looks authoritative, an
operator copies it, and every check it names reports NOT CHECKED for a reason
they will spend an afternoon on. So these tests do two things beyond parsing.

They run each recipe's field map against a sample record, modelled on the
vendor's published shape, and assert the values come out where the recipe says
they will. That is what catches a path that is wrong rather than merely unusual.

And they check the honesty claims. Each recipe states in its own first lines
which parts were run and which were written from documentation, and a test reads
that statement back. A recipe that starts implying it was verified against a
live account when it was not is the exact failure this repository has been
careful about elsewhere, so it gets a test rather than a code review comment.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from testinghq.core.config import ConfigError, load_config
from testinghq.pipeline.adapters import AdapterError, parse_readback_config
from testinghq.pipeline.readback import CHECKABLE_FIELDS, ReadbackError

REPO_ROOT = Path(__file__).resolve().parents[2]
RECIPES = REPO_ROOT / "examples" / "readback"

#: Every recipe the task asked for, and the kind each declares. Named here
#: rather than globbed so a recipe deleted from the directory fails the suite
#: instead of quietly reducing coverage.
EXPECTED = {
    "zendesk.toml": "http",
    "freshdesk.toml": "http",
    "generic-rest.toml": "http",
    "mail-sink.toml": "mailbox",
}


def _path(name: str) -> Path:
    return RECIPES / name


# ---------------------------------------------------------------------------
# They exist, and they parse
# ---------------------------------------------------------------------------


def test_the_directory_holds_exactly_the_recipes_the_backlog_asked_for():
    found = {p.name for p in RECIPES.glob("*.toml")}
    assert found == set(EXPECTED), (
        f"examples/readback holds {sorted(found)}, expected {sorted(EXPECTED)}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_parses_with_the_real_config_loader(name):
    """The real loader, not a TOML parse. A recipe that the config parser
    rejects is a recipe nobody can use, and a TOML parse would not notice."""
    config = load_config(_path(name))
    assert config.targets, f"{name} declares no firing target"
    for target in config.targets.values():
        assert target.url.startswith("http")


def _declared_env(name: str):
    """The environment a recipe needs, with placeholder values.

    A `[readback.headers]` entry naming a variable that is not set is refused by
    the parser, deliberately, so that a run cannot start without the credentials
    it was configured to use. That refusal is the correct behaviour and is
    tested separately; it does mean a recipe cannot be shape-checked against an
    empty environment, so the names it declares are supplied here with obviously
    fake values.
    """
    raw = load_config(_path(name)).readback_table() or {}
    return {
        value[len("env:"):]: "placeholder-not-a-credential"
        for value in (raw.get("headers") or {}).values()
        if isinstance(value, str) and value.startswith("env:")
    }


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_parses_with_the_real_readback_parser(name):
    """And with the real readback parser, at the point the tool uses it. This is
    where the field map, the headers and the adapter kind are actually
    validated."""
    raw = load_config(_path(name)).readback_table()
    assert raw is not None, f"{name} declares no [readback] table"
    config = parse_readback_config(raw, env=_declared_env(name))
    assert config.kind == EXPECTED[name]
    assert config.field_map(), f"{name} maps no fields"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_is_refused_when_its_named_variable_is_unset(name):
    """The credential guard, proven on the recipes themselves.

    A recipe naming a variable that is not set has to fail at load, before
    anything is sent, rather than discovering it at connect time. The adapter
    does this on purpose and the test says so, so a future change that relaxes
    it has to change this test and say why.
    """
    raw = load_config(_path(name)).readback_table()
    if not (raw or {}).get("headers"):
        pytest.skip(f"{name} declares no headers")
    with pytest.raises(AdapterError) as excinfo:
        parse_readback_config(raw, env={})
    message = str(excinfo.value)
    assert "not set" in message
    assert "before the run sends" in message or "must not start" in message


# ---------------------------------------------------------------------------
# No literal secrets, which is a load-time refusal not a convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_with_headers_refuses_a_literal_secret(name):
    """The recipes use `env:` references. Proving that a literal is refused is
    what makes the recipes safe to copy: someone who edits one to paste a token
    gets a refusal at load, not a credential in a file they were about to
    share."""
    raw = load_config(_path(name)).readback_table()
    headers = (raw or {}).get("headers")
    if not headers:
        pytest.skip(f"{name} declares no headers")

    for name_, value in headers.items():
        assert isinstance(value, str) and value.startswith("env:"), (
            f"{name}: [readback.headers].{name_} must be an env: reference, "
            f"got {value!r}"
        )

    with pytest.raises(AdapterError) as excinfo:
        parse_readback_config(
            {**raw, "headers": {"Authorization": "Bearer sk-live-realsecret"}},
            env={},
        )
    assert "literal" in str(excinfo.value).lower()


#: Credential shapes worth refusing in an example file. Written out per vendor
#: rather than as one loose pattern, because the first version of this was
#: `sk-[A-Za-z0-9]{16,}` and therefore missed `sk-live-...`, which is the shape
#: Stripe actually issues. A secret detector that misses the common format of the
#: secret is worse than none, because it reads as coverage.
CREDENTIAL_SHAPES = (
    r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b",
    r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9-]{14,}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_names_a_variable_and_not_a_credential(name):
    """Belt and braces on the same property, at the text level. The parser
    check above is the real one; this catches a token pasted into a comment
    where the parser would never see it.

    The assignment scan only looks at lines that are not comments, because a
    commented `export FOO_TOKEN="..."` is a documented shell command an operator
    is meant to run, not a secret in the config. The credential-shaped scan does
    look at comments, because that is exactly where someone would paste one.
    """
    text = _path(name).read_text(encoding="utf-8")
    for shape in CREDENTIAL_SHAPES:
        found = re.findall(shape, text)
        assert not found, (
            f"{name} appears to contain a literal credential matching {shape}: "
            f"{found[:2]}"
        )

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"(?i)(password|secret|token)\s*=\s*[\"'][^\"']{8,}", stripped):
            assert "env:" in stripped, f"{name}: a literal secret: {stripped!r}"


# ---------------------------------------------------------------------------
# The field maps, checked against records shaped like each vendor's
# ---------------------------------------------------------------------------


def _readback(name, record):
    raw = load_config(_path(name)).readback_table()
    config = parse_readback_config(raw, env=_declared_env(name))
    from testinghq.pipeline.adapters import readback_from_json

    return readback_from_json(record, config.field_map())


ZENDESK_RECORD = {
    "id": 35436,
    "subject": "Your order",
    "description": "Thanks for your order.",
    "via": {
        "followup_source": {
            "from": {
                "rel": None,
                "address": "alice@example.com",
                "name": "Alice",
            }
        }
    },
    "metadata": {"system": {"message_id": "<m1@example.com>"}},
    "tags": ["hq-1-0000"],
    "group_id": 1234,
    "attachments_count": 1,
}

FRESHDESK_RECORD = {
    "id": 42,
    "subject": "Your order",
    "description": "Thanks for your order.",
    "requester_id": 7,
    "group_id": 3,
    "tags": ["hq-1-0000"],
    "priority": 2,
    "type": "Billing",
}

GENERIC_RECORD = {
    "id": "R-1",
    "from": "Alice <alice@example.com>",
    "subject": "Your order",
    "body": "Thanks for your order.",
    "attachments": ["invoice.pdf"],
    "route": "queue:support",
    "message_id": "<m1@example.com>",
    "in_reply_to": None,
    "references": [],
    "category": "billing",
    "priority": "high",
    "tag": "hq-1-0000",
}


def test_the_zendesk_recipe_reads_the_paths_its_comments_name():
    """Each assertion here is a claim made in a comment in zendesk.toml. If one
    of these fails, the comment is wrong, and the comment is what an operator
    reads before copying the file."""
    readback = _readback("zendesk.toml", ZENDESK_RECORD)
    assert readback.ticket_id == "35436", "id is an int in the response"
    assert readback.from_addr == "alice@example.com", (
        "the recipe maps via.followup_source.from.address, a nested path with "
        "one more hop than the comment's first draft, because the object at "
        "`from` is not a string"
    )
    assert readback.subject == "Your order"
    assert readback.body == "Thanks for your order."
    assert readback.message_id == "<m1@example.com>"
    assert readback.route == "1234"
    assert readback.tag is None, (
        "the recipe deliberately does not map tag: a ticket's tags field is a "
        "list, and the search already found the ticket by the tag"
    )


def test_the_zendesk_recipe_says_the_body_is_only_the_plain_text_part():
    """Stated in the recipe as a real limitation. The assertion is that
    `description` is what got mapped, and that an HTML-only ticket reports the
    body as invisible rather than as matching."""
    plain = _readback("zendesk.toml", ZENDESK_RECORD)
    assert plain.body is not None

    html_only = dict(ZENDESK_RECORD)
    html_only.pop("description")
    html_only["description_html"] = "<p>Thanks for your order.</p>"
    assert _readback("zendesk.toml", html_only).body is None


def test_the_zendesk_recipe_leaves_attachment_names_unmapped():
    """The recipe's claim is that a ticket carries an attachment COUNT and
    mapping it would let a check compare the number 1 against ["invoice.pdf"].

    Asserted the honest way: the recipe does not map `attachments_count`, so the
    attachment check is reported as not visible rather than as passing. The
    default field map covers `attachment_names`, which Zendesk's search result
    simply does not have, so nothing is found and nothing is invented.
    """
    readback = _readback("zendesk.toml", ZENDESK_RECORD)
    assert readback.attachment_names == ()
    assert readback.has("attachment_names") is False, (
        "reported NOT CHECKED, which is the recipe's stated outcome"
    )


def test_a_numeric_attachment_field_is_refused_rather_than_read_as_a_name():
    """A real limitation, found while writing the recipe above, and recorded
    here rather than left to be discovered by an operator.

    An API that returns `"attachments": 1` rather than a list of names does not
    degrade to "the adapter cannot see this field". It raises
    `ReadbackError: attachment names must be a sequence of strings, got int`,
    which aborts the lookup.

    That is defensible as failing loud rather than reading a count as a name,
    and it is the safer of the two behaviours. But it is worth being written
    down, because the failure names a type rather than the field, and an
    operator hitting it will be looking at a type error rather than at their
    API's field naming. If this is ever relaxed, the relaxation has to decide
    what an int means, and it has to keep not treating it as a name.
    """
    record = dict(ZENDESK_RECORD)
    record["attachments"] = 1
    with pytest.raises(ReadbackError) as excinfo:
        _readback("zendesk.toml", record)
    assert "sequence of strings" in str(excinfo.value)


def test_the_freshdesk_recipe_reads_the_paths_its_comments_name():
    readback = _readback("freshdesk.toml", FRESHDESK_RECORD)
    assert readback.ticket_id == "42"
    assert readback.subject == "Your order"
    assert readback.body == "Thanks for your order."
    assert readback.route == "3"
    assert readback.priority == "2"
    assert readback.category == "Billing"


def test_the_freshdesk_recipe_does_not_map_requester_id_as_an_address():
    """The recipe's central claim: requester_id is an integer, and mapping it
    to from_addr would fail every check while reading like a broken pipeline."""
    readback = _readback("freshdesk.toml", FRESHDESK_RECORD)
    assert readback.from_addr is None
    assert readback.has("from_addr") is False, (
        "reported as NOT CHECKED, which is what the recipe says happens"
    )


def test_the_generic_recipe_reads_everything_the_comment_lists():
    readback = _readback("generic-rest.toml", GENERIC_RECORD)
    assert readback.ticket_id == "R-1"
    assert readback.from_addr == "Alice <alice@example.com>", (
        "the recipe says from_addr is normalized after mapping, so the raw value "
        "comes through unchanged here"
    )
    assert readback.subject == "Your order"
    assert readback.body == "Thanks for your order."
    assert readback.attachment_names == ("invoice.pdf",)
    assert readback.route == "queue:support"
    assert readback.category == "billing"
    assert readback.priority == "high"
    assert readback.tag == "hq-1-0000"


def test_the_generic_recipe_maps_every_checkable_field():
    """It is the reference recipe, so a field missing here is a field nobody
    learns to map."""
    config = parse_readback_config(
        load_config(_path("generic-rest.toml")).readback_table(),
        env=_declared_env("generic-rest.toml"),
    )
    missing = set(CHECKABLE_FIELDS) - set(config.field_map())
    assert not missing, f"the reference recipe does not map {sorted(missing)}"


def test_the_mail_sink_recipe_declares_no_credentials():
    """Stated in the recipe: no environment variables and no auth. Asserted
    rather than trusted, because a recipe that quietly grew a token would
    invalidate that claim."""
    raw = load_config(_path("mail-sink.toml")).readback_table()
    assert "headers" not in raw
    text = _path("mail-sink.toml").read_text(encoding="utf-8")
    assert "env:" not in text, (
        "the mail sink recipe claims it needs no environment variable"
    )


# ---------------------------------------------------------------------------
# The honesty claims, read back out of the files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_states_what_was_and_was_not_verified(name):
    """Every recipe has to carry a provenance statement, and the statement has
    to appear near the top where someone reads it before copying the file
    rather than after they have a broken run.

    The wording matters as much as the presence. A recipe written from a
    vendor's documentation and one run against a live account are different
    kinds of evidence, and a reader who cannot tell which they have will trust
    the wrong one.
    """
    text = _path(name).read_text(encoding="utf-8")
    head = text[:3000].upper()
    assert "VERIFIED" in head, f"{name} has no provenance statement"
    assert "NOT" in head and (
        "LIVE" in head or "RUN" in head
    ), f"{name} does not say what was not verified"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_never_claims_a_live_run_it_did_not_have(name):
    """The specific dishonesty this guards, stated as the two ways a recipe can
    be wrong about its own evidence.

    Claiming a run that did not happen is the serious one. The other direction
    matters too: a recipe that was genuinely run but hedges about it teaches a
    reader to discount the recipes that are honest about what they are.

    A recipe that says it was run must not also say it has not been run against
    a live account, and a recipe that does not say it was run must say what it
    was written from instead.
    """
    head = _path(name).read_text(encoding="utf-8")[:3000]
    claims_ran = re.search(r"\bthis one was run\b", head, re.IGNORECASE) is not None
    disclaims_live = (
        "not been run against a live" in head.lower()
        or "has not been run" in head.lower()
    )

    assert not (claims_ran and disclaims_live), (
        f"{name} says it was run and also that it was not run against a live "
        "account. One of the two statements is wrong."
    )
    if not claims_ran:
        assert "documentation" in head.lower(), (
            f"{name} does not claim a live run and does not say it was written "
            "from vendor documentation either. A reader is left guessing how much "
            "to trust it."
        )


def test_the_two_recipes_that_were_run_are_the_ones_claiming_to_be():
    """Both directions, so the check above cannot be satisfied by adding the
    claim everywhere. Only the mail sink and the generic REST recipe are
    exercised by this repository's own tests, so only those two may say they
    were run."""
    ran = {
        p.name for p in RECIPES.glob("*.toml")
        if "this one was run" in p.read_text(encoding="utf-8")[:3000].lower()
    }
    assert ran == {"mail-sink.toml", "generic-rest.toml"}, (
        f"{sorted(ran)} claim a live run; only the mail sink and the generic "
        "REST recipe are exercised by this repository's own tests"
    )


# ---------------------------------------------------------------------------
# The recipes stay honest as the code moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recipe_stays_parseable_after_a_field_is_renamed(name):
    """Not a synthetic check. This is the same parse as above, restated as the
    invariant that matters: when `CHECKABLE_FIELDS` gains or loses a name, every
    recipe is re-validated on the next run, so a recipe cannot keep mapping a
    field the tools no longer have.

    Kept as a separate test from the parse test on purpose, so a failure names
    the recipes rather than the parser.
    """
    config = parse_readback_config(
        load_config(_path(name)).readback_table(), env=_declared_env(name)
    )
    known = set(CHECKABLE_FIELDS) | {
        "ticket_id", "tag", "message_id", "attachment_names",
    }
    unknown = set(config.field_map()) - known
    assert not unknown, (
        f"{name} maps fields the readback contract does not have: "
        f"{sorted(unknown)}"
    )
