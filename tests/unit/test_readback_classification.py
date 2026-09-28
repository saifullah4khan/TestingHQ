"""`category` and `priority` on a readback: readable, and honest when absent.

The corpus generator has no triager's label to compare against, so there is no
verify check for these two and none is added here. What matters instead is the
distinction the `fields` contract exists to preserve: an intake system that
triages and reports a label, versus one that does not, versus one whose API
response happened not to include the field on this particular record. Only the
first is measurable, and a tool that cannot tell the three apart will report a
label it never saw.

These tests pin all three.
"""
from __future__ import annotations

import json

import pytest

from testinghq.pipeline.adapters import (
    DEFAULT_FIELD_MAP,
    MailboxAdapter,
    readback_from_json,
)
from testinghq.pipeline.readback import (
    CHECKABLE_FIELDS,
    READBACK_FIELDS,
    Readback,
    ReadbackError,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["category", "priority"])
def test_the_fields_are_part_of_the_readback_contract(name):
    """Anything an adapter may set has to be in READBACK_FIELDS, because
    `Readback.__post_init__` refuses a `fields` entry that is not, and a
    readback that cannot be constructed is worse than one missing a label."""
    assert name in READBACK_FIELDS


@pytest.mark.parametrize("name", ["category", "priority"])
def test_the_fields_are_checkable(name):
    """In CHECKABLE_FIELDS, so a tool with its own expectation can ask. The
    tools that ship today have none, which is the point: the field is
    available, not asserted."""
    assert name in CHECKABLE_FIELDS


@pytest.mark.parametrize("name", ["category", "priority"])
def test_the_default_map_names_a_key_for_the_field(name):
    assert name in DEFAULT_FIELD_MAP
    assert DEFAULT_FIELD_MAP[name] == name, (
        "the default should be the field's own name, so a flat API needs no "
        f"configuration at all; got {DEFAULT_FIELD_MAP[name]!r}"
    )


def test_the_closed_field_list_still_matches_the_dataclass():
    """READBACK_FIELDS is a hand-maintained tuple and the dataclass is the real
    thing. Nothing asserts they agree, so a field added to one and not the
    other is invisible until an adapter fails to construct at runtime."""
    import dataclasses

    declared = {f.name for f in dataclasses.fields(Readback)}
    assert set(READBACK_FIELDS) <= declared, (
        f"READBACK_FIELDS names fields the dataclass does not have: "
        f"{sorted(set(READBACK_FIELDS) - declared)}"
    )
    assert {"category", "priority"} <= declared


# ---------------------------------------------------------------------------
# The http adapter, via readback_from_json
# ---------------------------------------------------------------------------


def test_a_flat_api_needs_no_configuration_for_the_two_fields():
    record = readback_from_json(
        {"id": "T-1", "subject": "hi", "category": "billing", "priority": "high"},
        {"ticket_id": "id", "category": "category", "priority": "priority"},
    )
    assert record.category == "billing"
    assert record.priority == "high"
    assert record.has("category") is True
    assert record.has("priority") is True


def test_a_nested_dotted_path_reads_the_label():
    """The case that makes this worth having at all. A real triager puts its
    output under something like `attributes.triage`, and an operator should not
    have to flatten their own API to see it."""
    data = {
        "id": "T-2",
        "attributes": {
            "triage": {"category": "technical", "priority": "low"},
        },
    }
    field_map = {
        "ticket_id": "id",
        "category": "attributes.triage.category",
        "priority": "attributes.triage.priority",
    }
    record = readback_from_json(data, field_map)
    assert record.category == "technical"
    assert record.priority == "low"


def test_a_dotted_path_through_a_missing_parent_stays_absent():
    """`_dig` has to report the branch missing rather than raising or
    inventing a value. A parent that is not there means the system sent no
    label, and inventing one would be exactly the lie this design avoids."""
    record = readback_from_json(
        {"id": "T-3", "subject": "hi"},
        {
            "ticket_id": "id",
            "category": "attributes.triage.category",
            "priority": "attributes.triage.priority",
        },
    )
    assert record.category is None
    assert record.priority is None
    assert record.has("category") is False
    assert record.has("priority") is False


def test_an_absent_field_stays_none_and_out_of_the_field_list():
    """The honesty property, and the reason `fields` exists. A response with no
    category must not claim to have carried one, or a check that asked would
    compare against None and either pass everything or fail everything."""
    record = readback_from_json({"id": "T-4", "subject": "hi"},
                                {"ticket_id": "id", "category": "category"})
    assert record.category is None
    assert "category" not in record.fields


def test_an_explicit_null_is_a_seen_field_with_no_value():
    """Distinct from absent, and the distinction is the point.

    Some APIs serialize a missing string as null rather than omitting the key.
    That is not the adapter being unable to see the field: the adapter looked,
    the field was there, and the system said the ticket has no category. Those
    are different facts with different remedies, so `fields` records the field
    as carried and the value stays None.

    Getting this backwards in either direction is wrong. Treating a null as
    absent would report "the API does not expose a category" for a system that
    exposes one perfectly well and simply had nothing to put in it, and would
    SKIP a check that could have run.
    """
    record = readback_from_json(
        {"id": "T-5", "category": None, "priority": "p1"},
        {"ticket_id": "id", "category": "category", "priority": "priority"},
    )
    assert record.category is None
    assert record.has("category") is True, (
        "the response carried the key, so the adapter could see the field"
    )
    assert record.priority == "p1"


def test_a_non_string_label_is_coerced_to_a_string():
    """A real API that returns a number for priority returns an int, and
    refusing to read it would make a working integration look broken."""
    record = readback_from_json(
        {"id": "T-6", "priority": 3},
        {"ticket_id": "id", "priority": "priority"},
    )
    assert record.priority == "3"
    assert isinstance(record.priority, str)


def test_both_fields_reach_the_artifact():
    """They are readable, so a report has to be able to show them. The
    artifact is the thing a CI system parses, and a label that stopped at the
    adapter would be invisible to it."""
    record = readback_from_json(
        {"id": "T-7", "category": "sales", "priority": "urgent"},
        {"ticket_id": "id", "category": "category", "priority": "priority"},
    )
    payload = json.loads(json.dumps(record.to_json()))
    assert payload["category"] == "sales"
    assert payload["priority"] == "urgent"


def test_an_unknown_field_name_in_a_field_map_is_still_refused():
    """Adding two names must not have loosened the check that catches a typo in
    `[readback].fields`.

    Validated where the config is read rather than where a record is mapped, so
    a typo fails at startup with a message naming the known fields, instead of
    waiting for a real API to answer and silently contributing nothing.
    """
    from testinghq.pipeline.adapters import AdapterError, _as_str_table

    with pytest.raises(AdapterError) as excinfo:
        _as_str_table({"catgeory": "category"}, "fields")
    assert "catgeory" in str(excinfo.value)
    assert "category" in str(excinfo.value), (
        "the error has to name the right spelling, which is the only thing that"
        " makes it useful at 2am"
    )


def test_a_fields_tuple_naming_the_new_fields_is_accepted():
    """The other direction: `__post_init__` validates against READBACK_FIELDS,
    so an adapter that says it saw a category is no longer constructing an
    invalid readback."""
    record = Readback(
        exists=True,
        ticket_id="T-9",
        category="billing",
        fields=("category", "priority"),
    )
    assert record.has("category") is True
    assert record.has("priority") is True, (
        "a field can be declared carried while its value is None; the "
        "declaration is what the check consults"
    )


def test_a_fields_tuple_naming_a_field_outside_the_contract_is_refused():
    with pytest.raises(ReadbackError) as excinfo:
        Readback(exists=True, ticket_id="T-10", fields=("sentiment",))
    assert "sentiment" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The mailbox adapter
# ---------------------------------------------------------------------------


def _probe(tag):
    from testinghq.pipeline.messages import Probe, build_chain_message_id

    return Probe(
        record_id="r1",
        tag=tag,
        payload_sha256="a" * 64,
        from_addr="alice@example.com",
        subject="Your order",
        recipient="support@example.com",
        message_id=build_chain_message_id(tag),
    )


def _sink(tmp_path, **record):
    record.setdefault("id", "D-1")
    record.setdefault("tag", "hq-1-0000")
    path = tmp_path / "sink.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return MailboxAdapter(str(path))


def test_the_mailbox_adapter_populates_the_labels(tmp_path):
    found = _sink(
        tmp_path, id="D-1", subject="Your order",
        category="billing", priority="high",
    ).fetch(_probe("hq-1-0000"))
    assert len(found) == 1
    assert found[0].category == "billing"
    assert found[0].priority == "high"
    assert found[0].has("category") is True
    assert found[0].has("priority") is True


def test_the_mailbox_adapter_reports_a_sink_with_no_triage(tmp_path):
    """The common case: a mail sink records what it delivered and knows nothing
    about labels. The adapter has to say so, so a check depending on a category
    is SKIPPED with "adapter cannot see field" rather than compared against
    nothing."""
    found = _sink(tmp_path, id="D-2").fetch(_probe("hq-1-0000"))
    assert len(found) == 1
    assert found[0].category is None
    assert found[0].has("category") is False
    assert "category" not in found[0].fields


def test_the_mailbox_adapter_does_not_claim_a_field_it_never_set(tmp_path):
    """Stated per field rather than per record: a sink that triages by category
    but leaves priority unset must not report both as present, or a check on
    priority would silently run against nothing."""
    found = _sink(tmp_path, id="D-3", category="sales").fetch(_probe("hq-1-0000"))
    assert len(found) == 1
    assert found[0].has("category") is True
    assert found[0].has("priority") is False
    assert "category" in found[0].fields
    assert "priority" not in found[0].fields
