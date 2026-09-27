"""The readback seam: the Probe, the Readback, and the normalisation rules.

The normalisation tests here are the load-bearing ones. Every rule in
`readback.py` is a claim that some difference is not a parse bug, and a claim
nobody has ever tried to falsify is a preference. Each rule below is pinned
with both the case it exists for and the case it must NOT excuse, because a
lenient comparison that is only tested against the lenient case is a check
that will accept anything.
"""
from __future__ import annotations

import pytest

from testinghq.pipeline.readback import (
    CHECKABLE_FIELDS,
    READBACK_FIELDS,
    FunctionAdapter,
    MultiAdapter,
    Probe,
    Readback,
    ReadbackError,
    can_enumerate,
    normalize_address,
    normalize_attachment_names,
    normalize_message_ids,
    normalize_route,
    normalize_text,
)


def _probe(**overrides) -> Probe:
    base = dict(
        record_id="clean-1-0000",
        tag="hq-1-0000",
        payload_sha256="a" * 64,
        from_addr="alice@example.com",
        subject="Your order",
        recipient="support@example.com",
        message_id="abc@widgets.example",
    )
    base.update(overrides)
    return Probe(**base)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def test_a_probe_carries_no_timestamp():
    """Probes must be reproducible. A wall-clock field here would make a
    verification run non-reproducible for the same reason a corpus must be, and
    the fix is harder later than the omission is now."""
    fields = Probe.__dataclass_fields__
    for banned in ("sent_at", "timestamp", "now", "received_at"):
        assert banned not in fields


def test_a_probe_round_trips_through_its_fields():
    probe = _probe(attachment_names=("a.pdf",))
    assert probe.tag == "hq-1-0000"
    assert probe.attachment_names == ("a.pdf",)
    assert probe.recipient == "support@example.com"


# ---------------------------------------------------------------------------
# Readback construction
# ---------------------------------------------------------------------------


def test_a_readback_that_exists_must_name_itself():
    """No identifier, no answer. An adapter that cannot name what it found
    cannot distinguish one record from two, and a duplicate is the finding."""
    with pytest.raises(ReadbackError) as caught:
        Readback(exists=True, from_addr="a@example.com")
    assert "ticket_id" in str(caught.value)


def test_a_readback_that_does_not_exist_needs_no_identifier():
    readback = Readback(exists=False)
    assert readback.ticket_id is None
    assert readback.has("ticket_id") is False


def test_a_readback_may_report_a_known_but_dead_record():
    readback = Readback(exists=False, ticket_id="T1")
    assert readback.ticket_id == "T1"


def test_unknown_field_names_are_refused():
    with pytest.raises(ReadbackError) as caught:
        Readback(exists=True, ticket_id="T1", fields=("colour",))
    assert "colour" in str(caught.value)


def test_every_declared_field_is_a_real_readback_field():
    """`fields` is how an adapter says what it could see, so a name that is not
    real would make a check silently skip forever instead of running."""
    for name in READBACK_FIELDS:
        assert hasattr(Readback(exists=True, ticket_id="T1"), name)


# ---------------------------------------------------------------------------
# has(): the difference between "wrong" and "could not see"
# ---------------------------------------------------------------------------


def test_an_explicit_field_list_decides_what_was_visible():
    readback = Readback(
        exists=True, ticket_id="T1", subject="x", fields=("subject", "from_addr")
    )
    assert readback.has("subject") is True
    # `from_addr` is declared visible but the value is None, which is a
    # different fact from "not declared". Either way the check must not treat
    # it as a value to compare.
    assert readback.has("from_addr") is True
    assert readback.has("body") is False


def test_an_empty_field_list_infers_from_the_values_present():
    readback = Readback(exists=True, ticket_id="T1", subject="x", body="y")
    assert readback.has("subject") is True
    assert readback.has("body") is True
    assert readback.has("route") is False


def test_empty_collections_count_as_observed_not_absent():
    """A message with no attachments genuinely has none. Inferring "not
    visible" from an empty tuple would make every attachment-less payload
    report as unchecked, which is a lie of exactly the kind this module exists
    to stop."""
    readback = Readback(exists=True, ticket_id="T1")
    assert readback.has("attachment_names") is True
    assert readback.has("references") is True


def test_to_json_records_which_fields_were_visible():
    readback = Readback(exists=True, ticket_id="T1", subject="s", fields=("subject",))
    payload = readback.to_json()
    assert payload["subject"] == "s"
    assert payload["fields"] == ["subject"]


def test_to_json_records_the_inferred_field_set_when_none_was_declared():
    readback = Readback(exists=True, ticket_id="T1", subject="s")
    inferred = readback.to_json()["fields"]
    assert "subject" in inferred
    assert "route" not in inferred
    # The two collection fields count as observed when empty, so they appear.
    # Asserted explicitly because that is the design decision the whole
    # "attachment-less payload" path rests on.
    assert "attachment_names" in inferred
    assert "references" in inferred


# ---------------------------------------------------------------------------
# normalize_address
# ---------------------------------------------------------------------------


def test_address_normalization_accepts_the_shapes_a_system_stores():
    bare = "alice@example.com"
    display = "Alice Smith <alice@example.com>"
    assert normalize_address(bare) == normalize_address(display) == "alice@example.com"


def test_address_normalization_is_case_insensitive():
    assert normalize_address("Alice@EXAMPLE.COM") == normalize_address("alice@example.com")


def test_address_normalization_uses_getaddresses_not_parseaddr():
    """A multi-recipient header must reduce to its first address by an
    explicit rule, not by parseaddr's one-address limit. If this ever regressed
    to parseaddr, a routing bug on the second recipient would compare equal."""
    header = "Ops <ops@example.com>, Alice <alice@example.com>"
    assert normalize_address(header) == "ops@example.com"


def test_address_normalization_of_nonsense_is_empty_not_a_crash():
    assert normalize_address("") == ""
    assert normalize_address(None) == ""
    # RFC 5322 has no notion of an empty recipient group, and getaddresses
    # yields an empty address for one, which is the case this guard has to
    # catch rather than compare.
    assert normalize_address("undisclosed-recipients:;") == ""


def test_address_normalization_keeps_a_bare_local_part_as_an_addr_spec():
    """A token with no @ is still a syntactically valid addr-spec, so getaddresses
    returns it. Reducing it to "" would have made a system that stored a
    local-part-only address look identical to one that stored nothing."""
    assert normalize_address("postmaster") == "postmaster"
    assert normalize_address("postmaster") != normalize_address("")


def test_address_normalization_never_coerces_a_non_string():
    """A value this cannot read is a value it cannot vouch for. Stringifying
    it would manufacture a comparison out of nothing."""
    assert normalize_address(42) == ""
    assert normalize_address(["a@example.com"]) == ""


def test_address_normalization_distinguishes_really_different_addresses():
    assert normalize_address("alice@example.com") != normalize_address("bob@example.com")


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------


def test_text_normalization_folds_whitespace_so_a_reflow_is_not_a_mismatch():
    assert normalize_text("a\n\n  b\tc") == normalize_text("a b c")


def test_text_normalization_is_case_insensitive():
    assert normalize_text("Your Order") == normalize_text("your order")


def test_text_normalization_applies_nfkc_so_equivalent_forms_match():
    """A pipeline that extracts the HTML part hands back a ligature, a
    full-width character, or a non-breaking space for a message that parsed
    correctly. NFKC is what makes those compare equal."""
    assert normalize_text("the \ufb01le here") == normalize_text("the file here")
    assert normalize_text("a\uff21\uff22") == normalize_text("aab")
    assert normalize_text("a\u00a0b") == normalize_text("a b")


def test_text_normalization_does_not_excuse_a_mangled_string():
    """The case the fold must NOT swallow. A body that was mojibake'd or
    truncated has to stay different, or the body check is decoration."""
    assert normalize_text("This message is about your order.") != normalize_text(
        "This message is about your or"
    )
    assert normalize_text("caf\u00e9") != normalize_text("cafÃ©")


def test_text_normalization_of_nonsense_is_empty():
    assert normalize_text(None) == ""
    assert normalize_text(7) == ""


# ---------------------------------------------------------------------------
# normalize_route
# ---------------------------------------------------------------------------


def test_route_normalization_folds_case_and_whitespace():
    assert normalize_route("  Support  Queue ") == normalize_route("support queue")


def test_route_normalization_does_not_apply_nfkc():
    """A queue name is an identifier, not prose. Folding compatibility
    characters could make two genuinely different destinations compare
    equal, which is a routing bug reported as correct routing."""
    assert normalize_route("caf\u00e9") != normalize_route("cafe")


def test_route_normalization_distinguishes_really_different_routes():
    assert normalize_route("support") != normalize_route("billing")


# ---------------------------------------------------------------------------
# normalize_attachment_names
# ---------------------------------------------------------------------------


def test_attachment_names_are_compared_as_a_set():
    assert normalize_attachment_names(["b.txt", "a.txt"]) == normalize_attachment_names(
        ["a.txt", "b.txt"]
    )


def test_attachment_names_ignore_directory_components():
    assert normalize_attachment_names(["/var/tmp/a.pdf"]) == normalize_attachment_names(
        ["a.pdf"]
    )
    assert normalize_attachment_names(["C:\\tmp\\a.pdf"]) == normalize_attachment_names(
        ["a.pdf"]
    )


def test_attachment_names_are_case_insensitive():
    assert normalize_attachment_names(["Invoice.PDF"]) == normalize_attachment_names(
        ["invoice.pdf"]
    )


def test_a_single_attachment_name_may_be_a_bare_string():
    assert normalize_attachment_names("a.pdf") == ("a.pdf",)


def test_missing_attachments_normalize_to_empty():
    assert normalize_attachment_names(None) == ()
    assert normalize_attachment_names([]) == ()


def test_attachment_names_refuse_a_non_sequence():
    with pytest.raises(ReadbackError):
        normalize_attachment_names(7)
    with pytest.raises(ReadbackError):
        normalize_attachment_names([7])


def test_attachment_names_distinguish_a_missing_file_from_an_extra_one():
    assert normalize_attachment_names(["a.txt"]) != normalize_attachment_names(["b.txt"])


# ---------------------------------------------------------------------------
# normalize_message_ids
# ---------------------------------------------------------------------------


def test_message_ids_lose_their_angle_brackets():
    assert normalize_message_ids("<a@b>") == normalize_message_ids("a@b")


def test_a_references_chain_may_arrive_as_one_string():
    assert normalize_message_ids("<a@b> <c@d>") == normalize_message_ids(["a@b", "c@d"])


def test_chain_order_is_not_a_property_any_system_preserves():
    assert normalize_message_ids(["a@b", "c@d"]) == normalize_message_ids(["c@d", "a@b"])


def test_a_chain_membership_check_is_possible_from_the_normalized_form():
    normalized = normalize_message_ids(["<a@b>", "<c@d>"])
    assert "a@b" in normalized
    assert "z@z" not in normalized


def test_message_ids_refuse_a_non_string_element():
    with pytest.raises(ReadbackError):
        normalize_message_ids([7])


# ---------------------------------------------------------------------------
# FunctionAdapter
# ---------------------------------------------------------------------------


def test_a_function_adapter_wraps_a_plain_callable():
    adapter = FunctionAdapter(lambda probe: Readback(exists=True, ticket_id=probe.tag))
    assert adapter.fetch(_probe())[0].ticket_id == "hq-1-0000"


def test_a_function_adapter_accepts_none_as_not_found():
    assert FunctionAdapter(lambda probe: None).fetch(_probe()) == []


def test_a_function_adapter_accepts_a_sequence():
    adapter = FunctionAdapter(
        lambda probe: [Readback(exists=True, ticket_id="T1"), Readback(exists=True, ticket_id="T2")]
    )
    assert len(adapter.fetch(_probe())) == 2


def test_a_function_adapter_refuses_a_return_type_it_cannot_grade():
    with pytest.raises(ReadbackError):
        FunctionAdapter(lambda probe: "not a readback").fetch(_probe())
    with pytest.raises(ReadbackError):
        FunctionAdapter(lambda probe: [Readback(exists=True, ticket_id="T1"), 7]).fetch(_probe())


def test_a_function_adapter_refuses_a_non_callable():
    with pytest.raises(ReadbackError):
        FunctionAdapter("not callable")


def test_a_function_adapter_can_be_closed():
    adapter = FunctionAdapter(lambda probe: None)
    assert adapter.closed is False
    adapter.close()
    assert adapter.closed is True


def test_a_function_adapter_cannot_enumerate():
    """The absence is the point: `can_enumerate` must be false, so the tools
    report strays as not-searched rather than as none found."""
    assert can_enumerate(FunctionAdapter(lambda probe: None)) is False


# ---------------------------------------------------------------------------
# MultiAdapter
# ---------------------------------------------------------------------------


def test_a_multi_adapter_concatenates_what_its_delegates_found():
    one = FunctionAdapter(lambda p: [Readback(exists=True, ticket_id="T1")])
    two = FunctionAdapter(lambda p: [Readback(exists=True, ticket_id="T2")])
    assert [r.ticket_id for r in MultiAdapter([one, two]).fetch(_probe())] == ["T1", "T2"]


def test_a_multi_adapter_can_enumerate_only_when_every_delegate_can():
    enumerable = MultiAdapter([_StubEnumerating()])
    assert can_enumerate(enumerable) is True
    assert can_enumerate(MultiAdapter([_StubEnumerating(), FunctionAdapter(lambda p: None)])) is False


def test_a_multi_adapter_refuses_a_partial_enumeration():
    """A partial enumeration reported as complete is how stray records go
    missing, so it is an error rather than a shorter list."""
    with pytest.raises(ReadbackError) as caught:
        MultiAdapter([_StubEnumerating(), FunctionAdapter(lambda p: None)]).list_all()
    assert "enumerate" in str(caught.value)


def test_a_multi_adapter_closes_every_delegate():
    delegates = [FunctionAdapter(lambda p: None) for _ in range(3)]
    MultiAdapter(delegates).close()
    assert all(d.closed for d in delegates)


class _StubEnumerating:
    def fetch(self, probe):
        return []

    def list_all(self):
        return []

    def close(self):
        return None


# ---------------------------------------------------------------------------
# The module's own contract
# ---------------------------------------------------------------------------


def test_checkable_fields_are_a_subset_of_the_real_fields():
    assert set(CHECKABLE_FIELDS) <= set(READBACK_FIELDS)
    # ticket_id is excluded on purpose: the ticket_created check is built on
    # it and may never skip.
    assert "ticket_id" not in CHECKABLE_FIELDS
