"""The adapters: configuration, the two built-ins, and the safety gate.

The safety gate gets the most attention here. A readback URL is a place this
tool connects to and reads from, and on a real deployment it is a ticket store
or a mail sink that may hold other people's data. `require_readback_target` is
the single place that decides whether that is allowed, so the tests below are
mostly about proving it fires, and proving the same check cannot be bypassed by
the other ways an adapter can be built.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from testinghq.core import guardrails
from testinghq.core.transport import ClientResponse
from testinghq.pipeline import adapters
from testinghq.pipeline import messages
from testinghq.pipeline.adapters import (
    CHECKABLE_FIELDS,
    DEFAULT_FIELD_MAP,
    Readback,
    ReadbackError,
    build_adapter,
    parse_readback_config,
    require_readback_target,
)


def _probe(tag: str = "hq-1-0000"):
    return messages.Probe(
        record_id="clean-1-0000",
        tag=tag,
        payload_sha256="a" * 64,
        from_addr="alice@example.com",
        subject="Your order",
        recipient="support@example.com",
        message_id="m@widgets.example",
    )


class _StubClient:
    """An injectable HttpClient. Records the requests it was given and answers
    from a canned queue, so the http adapter is testable without a socket."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("the adapter made more requests than were stubbed")
        return self.responses.pop(0)


def _json_response(payload, status: int = 200) -> ClientResponse:
    return ClientResponse(status=status, body=json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# The safety gate
# ---------------------------------------------------------------------------


def test_a_loopback_readback_url_is_allowed():
    assert adapters.require_readback_target("http://127.0.0.1:8000/tickets")


def test_a_reserved_domain_readback_url_is_allowed():
    assert adapters.require_readback_target("https://tickets.example.test/api")


def test_a_public_readback_url_is_refused():
    """A readback on a real host is a place this tool would read other people's
    data from, and the refusal is the same shape as the firing guardrail's."""
    with pytest.raises(guardrails.GuardrailError) as caught:
        adapters.require_readback_target("https://tickets.acmecorp.com/api")
    assert "public host" in str(caught.value)


def test_a_public_readback_url_can_be_overridden_deliberately():
    assert adapters.require_readback_target(
        "https://tickets.acmecorp.com/api", allow_public_hosts=True
    )


def test_a_non_http_readback_url_is_refused():
    for bad in ("file:///etc/passwd", "ftp://x/y", "tickets.example.test"):
        with pytest.raises(adapters.AdapterError):
            adapters.require_readback_target(bad)


def test_the_http_adapter_gates_its_url_at_construction():
    """Not at fetch time. An adapter that is constructed against a public host
    has already had the decision made; refusing only when a request is about to
    go out would leave the bad URL sitting in a config file looking fine."""
    with pytest.raises(guardrails.GuardrailError):
        adapters.HttpJsonAdapter("https://tickets.acmecorp.com/api")


def test_the_mailbox_adapter_gates_nothing_because_it_reads_a_file():
    """It opens a local path, so the host guardrail has nothing to say. The
    path still has to be a path the operator named, which is all the control
    there is; saying so is better than pretending there is a check."""
    adapter = adapters.MailboxAdapter("does-not-exist.jsonl")
    assert adapter.fetch(_probe()) == []


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


def test_a_bare_kind_string_is_a_valid_config():
    config = adapters.parse_readback_config("http")
    assert config.kind == "http"
    assert config.url is None


def test_no_config_at_all_is_refused_with_an_actionable_message():
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.parse_readback_config(None)
    assert "--readback" in str(caught.value)


def test_a_readback_table_must_declare_a_kind():
    with pytest.raises(adapters.AdapterError):
        adapters.parse_readback_config({"url": "http://localhost:8000"})


def test_a_field_map_must_name_real_fields():
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.parse_readback_config({"kind": "http", "fields": {"colour": "colour"}})
    assert "colour" in str(caught.value)


def test_a_field_map_must_be_strings():
    with pytest.raises(adapters.AdapterError):
        adapters.parse_readback_config({"kind": "http", "fields": {"subject": 7}})


def test_a_field_map_overlays_the_default_rather_than_replacing_it():
    """An operator who renames one field should not have to restate the other
    nine, and a partial map that silently unset them would turn every other
    check into a skip."""
    config = adapters.parse_readback_config(
        {"kind": "http", "fields": {"subject": "data.title"}}
    )
    field_map = config.field_map()
    assert field_map["subject"] == "data.title"
    assert field_map["ticket_id"] == "id"
    assert field_map["from_addr"] == "from"


def test_a_non_numeric_timeout_is_refused():
    with pytest.raises(adapters.AdapterError):
        adapters.parse_readback_config({"kind": "http", "timeout": "soon"})


def test_a_boolean_is_not_a_number_for_timeout_purposes():
    """`True` is an int in Python. A timeout of True would be 1 second and the
    operator would never know why."""
    with pytest.raises(adapters.AdapterError):
        adapters.parse_readback_config({"kind": "http", "timeout": True})


# ---------------------------------------------------------------------------
# Building whatever the operator pointed at
# ---------------------------------------------------------------------------


def test_an_unknown_kind_is_refused_with_the_known_ones_named():
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.build_adapter(adapters.parse_readback_config("carrier-pigeon"))
    assert "http" in str(caught.value)


def test_the_http_adapter_needs_a_url():
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.build_adapter(adapters.parse_readback_config("http"))
    assert "url" in str(caught.value)


def test_the_mailbox_adapter_needs_a_path():
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.build_adapter(adapters.parse_readback_config("mailbox"))
    assert "path" in str(caught.value)


def test_a_relative_mailbox_path_resolves_against_the_config_directory():
    """A sink at `mailbox.jsonl` almost never means the same thing relative to
    the current working directory as it does relative to the config that names
    it, and the difference is a silent report of nothing found."""
    config = adapters.parse_readback_config(
        {"kind": "mailbox", "path": "sink.jsonl"}
    )
    adapter = adapters.build_adapter(config, config_dir="/srv/testinghq")
    assert adapter.path == Path("/srv/testinghq/sink.jsonl")


def test_an_absolute_mailbox_path_is_left_alone(tmp_path):
    sink = tmp_path / "sink.jsonl"
    config = adapters.parse_readback_config(
        {"kind": "mailbox", "path": str(sink)}
    )
    adapter = adapters.build_adapter(config, config_dir="/somewhere/else")
    assert adapter.path == sink


# ---------------------------------------------------------------------------
# readback_from_json
# ---------------------------------------------------------------------------


def test_a_json_record_maps_to_a_readback():
    readback = adapters.readback_from_json(
        {"id": "T1", "from": "a@example.com", "subject": "s", "body": "b"}, 
        adapters.DEFAULT_FIELD_MAP,
    )
    assert readback.ticket_id == "T1"
    assert readback.from_addr == "a@example.com"
    assert readback.has("subject") is True
    assert readback.has("route") is False


def test_a_json_record_records_precisely_which_fields_it_carried():
    """The whole point. An API that returned only an id must not have the body
    check compare against a null it can never match."""
    readback = adapters.readback_from_json({"id": "T1"}, adapters.DEFAULT_FIELD_MAP)
    assert set(readback.fields) == {"ticket_id"}


def test_a_dotted_field_path_reaches_into_nested_objects():
    readback = adapters.readback_from_json(
        {"data": {"id": "T1", "attributes": {"subject": "s"}}},
        {
            **adapters.DEFAULT_FIELD_MAP,
            "ticket_id": "data.id",
            "subject": "data.attributes.subject",
        },
    )
    assert readback.ticket_id == "T1"
    assert readback.subject == "s"


def test_a_record_with_no_identifier_is_refused():
    """The rule that makes a duplicate detectable at all."""
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.readback_from_json({"subject": "s"}, adapters.DEFAULT_FIELD_MAP)
    assert "identifier" in str(caught.value)


def test_a_record_whose_identifier_is_null_is_refused():
    with pytest.raises(adapters.AdapterError):
        adapters.readback_from_json({"id": None}, adapters.DEFAULT_FIELD_MAP)


def test_a_references_chain_may_arrive_as_a_string():
    readback = adapters.readback_from_json(
        {"id": "T1", "references": "<a@w> <b@w>"}, adapters.DEFAULT_FIELD_MAP
    )
    assert readback.references == ("a@w", "b@w")


def test_a_null_field_is_present_but_empty():
    """Present-and-null and absent are different facts, and the fields tuple
    keeps them apart."""
    readback = adapters.readback_from_json(
        {"id": "T1", "subject": None}, adapters.DEFAULT_FIELD_MAP
    )
    assert "subject" in readback.fields
    assert readback.subject is None


def test_a_non_object_record_is_refused():
    with pytest.raises(adapters.AdapterError):
        adapters.readback_from_json(["not", "an", "object"], adapters.DEFAULT_FIELD_MAP)


def test_a_non_scalar_field_is_refused_rather_than_stringified():
    with pytest.raises(adapters.AdapterError):
        adapters.readback_from_json(
            {"id": "T1", "subject": {"nested": "object"}}, adapters.DEFAULT_FIELD_MAP
        )


# ---------------------------------------------------------------------------
# HttpJsonAdapter
# ---------------------------------------------------------------------------


def test_the_http_adapter_asks_by_tag():
    client = _StubClient(_json_response([]))
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/tickets", client=client
    )
    adapter.fetch(_probe("hq-7-0003"))
    assert client.requests[0].url == "http://localhost:8000/tickets?tag=hq-7-0003"
    assert client.requests[0].method == "GET"
    assert client.requests[0].timeout == adapters.DEFAULT_HTTP_TIMEOUT


def test_the_http_adapter_appends_to_an_existing_query_string():
    client = _StubClient(_json_response([]))
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/tickets?tenant=acme", client=client
    )
    adapter.fetch(_probe())
    assert client.requests[0].url == (
        "http://localhost:8000/tickets?tenant=acme&tag=hq-1-0000"
    )


def test_the_tag_parameter_name_is_configurable():
    client = _StubClient(_json_response([]))
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/tickets", tag_param="x_tag", client=client
    )
    adapter.fetch(_probe())
    assert "x_tag=hq-1-0000" in client.requests[0].url


def test_a_list_response_yields_one_readback_per_record():
    """The duplicates case. A response carrying two records for one tag is the
    finding, so the adapter must return both rather than picking one."""
    client = _StubClient(_json_response([{"id": "T1"}, {"id": "T2"}]))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    found = adapter.fetch(_probe())
    assert [r.ticket_id for r in found] == ["T1", "T2"]


def test_a_bare_object_response_yields_one_readback():
    client = _StubClient(_json_response({"id": "T1"}))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["T1"]


def test_a_wrapped_list_response_is_unwrapped():
    client = _StubClient(_json_response({"items": [{"id": "T1"}], "total": 1}))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["T1"]


def test_the_wrapper_key_is_configurable():
    client = _StubClient(_json_response({"data": [{"id": "T1"}]}))
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/t", items_key="data", client=client
    )
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["T1"]


def test_a_404_is_not_an_error_but_not_found():
    """The whole point of the tool is that 'not found' is a finding, so a
    lookup miss must come back as an empty answer rather than an exception."""
    client = _StubClient(_json_response({"error": "nope"}, status=404))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    assert adapter.fetch(_probe()) == []


def test_a_500_is_an_error_not_a_loss():
    """A server that could not answer is not a pipeline that lost a message,
    and reporting it as one would be the worst kind of wrong."""
    client = _StubClient(_json_response({"error": "boom"}, status=500))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    with pytest.raises(adapters.AdapterError) as caught:
        adapter.fetch(_probe())
    assert "500" in str(caught.value)


def test_a_non_json_response_is_refused():
    client = _StubClient(ClientResponse(status=200, body=b"<html>login</html>"))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    with pytest.raises(adapters.AdapterError):
        adapter.fetch(_probe())


def test_a_record_that_is_neither_object_nor_list_is_refused():
    client = _StubClient(_json_response("a string"))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    with pytest.raises(adapters.AdapterError):
        adapter.fetch(_probe())


def test_a_failing_enumeration_is_an_error_not_a_partial_list():
    """A partial enumeration reported as complete is how stray records go
    missing, so a 500 on the listing is a refusal rather than an empty list."""
    client = _StubClient(_json_response({"error": "boom"}, status=500))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    with pytest.raises(adapters.AdapterError):
        adapter.list_all()


# ---------------------------------------------------------------------------
# MailboxAdapter
# ---------------------------------------------------------------------------


def _sink_line(**overrides):
    line = {
        "id": "M1",
        "tag": "hq-1-0000",
        "from": "alice@example.com",
        "to": "support@example.com",
        "subject": "Your order",
        "body": "hello",
        "attachments": [],
    }
    line.update(overrides)
    return json.dumps(line)


def _write_sink(tmp_path, *lines) -> Path:
    sink = tmp_path / "sink.jsonl"
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sink


def test_the_mailbox_adapter_matches_on_the_tag_field(tmp_path):
    sink = _write_sink(tmp_path, _sink_line(id="M1"), _sink_line(id="M2", tag="hq-1-0001"))
    adapter = adapters.MailboxAdapter(str(sink))
    assert [r.ticket_id for r in adapter.fetch(_probe("hq-1-0001"))] == ["M2"]


def test_the_mailbox_adapter_recovers_a_tag_stamped_only_in_the_body(tmp_path):
    """The realistic sink case: a mail sink records what it delivered and has
    no idea TestingHQ put a tag anywhere."""
    body = "the real body\n\n[testinghq:hq-1-0000]\n"
    sink = _write_sink(
        tmp_path, _sink_line(tag=None, body=body), _sink_line(tag=None, id="M2", body="x")
    )
    adapter = adapters.MailboxAdapter(str(sink))
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["M1"]


def test_the_mailbox_adapter_re_reads_the_file_on_every_fetch(tmp_path):
    """A sink is written while TestingHQ is still running. A cached snapshot
    would report a healthy pipeline as having produced nothing."""
    sink = _write_sink(tmp_path, _sink_line(id="M1"))
    adapter = adapters.MailboxAdapter(str(sink))
    assert len(adapter.fetch(_probe())) == 1
    with sink.open("a", encoding="utf-8") as handle:
        handle.write(_sink_line(id="M2") + "\n")
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["M1", "M2"]


def test_the_mailbox_adapter_reports_a_missing_sink_as_empty(tmp_path):
    """The sink may not have written its first delivery yet. Treating an absent
    file as a configuration error would make --settle impossible."""
    assert adapters.MailboxAdapter(str(tmp_path / "nope.jsonl")).fetch(_probe()) == []


def test_the_mailbox_adapter_can_enumerate(tmp_path):
    sink = _write_sink(
        tmp_path, _sink_line(id="M1"), _sink_line(id="M2", tag="someone-elses")
    )
    adapter = adapters.MailboxAdapter(str(sink))
    assert len(adapter.list_all()) == 2


def test_the_mailbox_adapter_skips_blank_lines(tmp_path):
    sink = tmp_path / "sink.jsonl"
    sink.write_text(f"\n\n{_sink_line()}\n\n", encoding="utf-8")
    assert len(adapters.MailboxAdapter(str(sink)).list_all()) == 1


def test_a_corrupt_sink_line_is_refused_with_its_line_number(tmp_path):
    """A partially written last line is expected while a run is in flight. A
    corrupt line anywhere else is not, and the line number is what makes that
    distinction actionable."""
    sink = tmp_path / "sink.jsonl"
    sink.write_text(f"{_sink_line()}\nnot json\n", encoding="utf-8")
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.MailboxAdapter(str(sink)).list_all()
    assert ":2" in str(caught.value)


def test_a_sink_line_that_is_not_an_object_is_refused(tmp_path):
    sink = tmp_path / "sink.jsonl"
    sink.write_text('"just a string"\n', encoding="utf-8")
    with pytest.raises(adapters.AdapterError):
        adapters.MailboxAdapter(str(sink)).list_all()


def test_a_sink_line_with_no_identifier_still_yields_a_readback(tmp_path):
    """Unlike an API record, a mail sink line is a delivery, and a delivery is
    already its own identity. Refusing it would make the mailbox adapter
    useless against the very sinks it is for."""
    sink = _write_sink(tmp_path, _sink_line(id=None, message_id="<m@w>"))
    adapter = adapters.MailboxAdapter(str(sink))
    found = adapter.fetch(_probe())
    assert len(found) == 1
    assert found[0].ticket_id


def test_a_sink_line_records_which_fields_it_carried(tmp_path):
    sink = _write_sink(tmp_path, _sink_line(route=None, attachments=[]))
    found = adapters.MailboxAdapter(str(sink)).fetch(_probe())
    # `fields` names the Readback field, not the sink's JSON key, because that
    # is what a check asks about.
    assert "route" in found[0].fields
    assert "attachment_names" in found[0].fields
    assert "in_reply_to" not in found[0].fields


def test_a_sink_references_chain_may_arrive_as_a_string(tmp_path):
    sink = _write_sink(tmp_path, _sink_line(references="<a@w> <b@w>"))
    found = adapters.MailboxAdapter(str(sink)).fetch(_probe())
    assert found[0].references == ("a@w", "b@w")


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_the_http_adapter_can_enumerate_by_default():
    """The default, because most ticket APIs already answer their lookup url
    with everything when the tag parameter is absent, and a ledger that can
    never search for strays can never report "extra". It would report
    "not searched" on every single run."""
    client = _StubClient(_json_response([{"id": "T1"}, {"id": "T2"}]))
    adapter = adapters.HttpJsonAdapter("http://localhost:8000/tickets", client=client)
    assert [r.ticket_id for r in adapter.list_all()] == ["T1", "T2"]
    assert client.requests[0].url == "http://localhost:8000/tickets"
    assert "tag=" not in client.requests[0].url


def test_the_http_adapter_can_be_pointed_at_a_different_listing_url():
    client = _StubClient(_json_response([{"id": "T9"}]))
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/tickets",
        list_path="http://localhost:8000/tickets/all",
        client=client,
    )
    assert [r.ticket_id for r in adapter.list_all()] == ["T9"]
    assert client.requests[0].url == "http://localhost:8000/tickets/all"


def test_enumeration_can_be_turned_off_for_a_system_that_cannot_do_it():
    """And then the tools say strays were not searched, rather than reporting
    none found for a question never asked."""
    client = _StubClient()
    adapter = adapters.HttpJsonAdapter(
        "http://localhost:8000/tickets", enumerate_records=False, client=client
    )
    with pytest.raises(ReadbackError) as caught:
        adapter.list_all()
    assert "enumerate" in str(caught.value)
    assert client.requests == [], "a disabled enumeration still made a request"


def test_the_enumerate_flag_must_be_a_boolean():
    with pytest.raises(adapters.AdapterError):
        parse_readback_config({"kind": "http", "enumerate": "yes"})


def test_an_enumerating_adapter_says_so():
    from testinghq.pipeline.readback import can_enumerate

    client = _StubClient(_json_response([]))
    assert can_enumerate(
        adapters.HttpJsonAdapter("http://localhost:8000/t", client=client)
    ) is True
    assert can_enumerate(
        adapters.HttpJsonAdapter(
            "http://localhost:8000/t", enumerate_records=False, client=client
        )
    ) is True, (
        "the method still exists so callers can ask; what changes is that it "
        "raises rather than returning a partial list, which is what "
        "can_enumerate on a caller side would otherwise treat as searchable"
    )


# ---------------------------------------------------------------------------
# A kind that names an import path
# ---------------------------------------------------------------------------


def test_a_kind_that_names_an_import_path_is_treated_as_a_spec():
    """So the flag form and the config-table form produce the same config, and a
    custom adapter written against one works with the other."""
    config = parse_readback_config(
        {"kind": "mypkg.mine:build", "url": "http://localhost:8000/tickets"}
    )
    assert config.spec == "mypkg.mine:build"
    assert config.url == "http://localhost:8000/tickets"
    assert config.to_json()["spec"] == "mypkg.mine:build"


def test_a_bare_kind_string_with_a_colon_is_still_a_spec():
    assert parse_readback_config("mypkg.mine:fetch").spec == "mypkg.mine:fetch"


def test_an_explicit_spec_wins_over_a_kind_that_looks_like_one():
    config = parse_readback_config(
        {"kind": "http", "spec": "mypkg.mine:build", "url": "http://localhost/x"}
    )
    assert config.spec == "mypkg.mine:build"


def test_a_spec_overrides_a_builtin_kind_even_when_the_url_is_present():
    """The flag is how you bring your own; a config file that also names a url
    must not quietly win over it."""
    import sys
    import types

    module = types.ModuleType("mypkg_spec_probe")
    module.adapter = _ProbeAdapter()
    sys.modules["mypkg_spec_probe"] = module
    try:
        adapter = adapters.build_adapter(
            parse_readback_config(
                {"kind": "mypkg_spec_probe:adapter", "url": "http://localhost:1/x"}
            )
        )
        assert isinstance(adapter, _ProbeAdapter)
    finally:
        del sys.modules["mypkg_spec_probe"]


# ---------------------------------------------------------------------------
# Header auth
# ---------------------------------------------------------------------------

TOKEN_ENV = {"HQ_READBACK_TOKEN": "s3cr3t-value"}


def _http_config(**overrides):
    raw = {"kind": "http", "url": "http://localhost:8000/tickets"}
    raw.update(overrides)
    return raw


def test_a_header_value_is_read_from_the_environment():
    config = parse_readback_config(
        _http_config(headers={"Authorization": "env:HQ_READBACK_TOKEN"}), env=TOKEN_ENV
    )
    assert config.headers == {"Authorization": "s3cr3t-value"}
    assert config.header_names() == ["Authorization"]


def test_a_literal_header_value_is_refused():
    """A config file gets shared, and a token in one ends up in a git history, a
    bug report, or a CI log. There is no literal form, not even a placeholder."""
    with pytest.raises(adapters.AdapterError) as caught:
        parse_readback_config(
            _http_config(headers={"Authorization": "Bearer s3cr3t"}), env=TOKEN_ENV
        )
    message = str(caught.value)
    assert "literal value" in message
    assert "env:" in message
    assert "s3cr3t" not in message, "the refused value was echoed back"


def test_a_missing_environment_variable_is_refused():
    with pytest.raises(adapters.AdapterError) as caught:
        parse_readback_config(
            _http_config(headers={"Authorization": "env:HQ_ABSENT"}), env=TOKEN_ENV
        )
    message = str(caught.value)
    assert "HQ_ABSENT" in message
    assert "not set" in message


def test_a_header_name_with_no_variable_after_the_prefix_is_refused():
    with pytest.raises(adapters.AdapterError) as caught:
        parse_readback_config(_http_config(headers={"X-Tenant": "env:"}), env=TOKEN_ENV)
    assert "no environment variable" in str(caught.value)


def test_a_non_string_header_value_is_refused():
    with pytest.raises(adapters.AdapterError):
        parse_readback_config(_http_config(headers={"X-Tenant": 7}), env=TOKEN_ENV)


def test_a_headers_value_that_is_not_a_table_is_refused():
    with pytest.raises(adapters.AdapterError):
        parse_readback_config(_http_config(headers="Authorization: x"), env=TOKEN_ENV)


def test_headers_default_to_none_and_not_an_error():
    config = parse_readback_config(_http_config(), env={})
    assert config.headers == {}
    assert config.header_names() == []


def test_the_resolved_value_never_reaches_to_json():
    """`to_json` is what lands in every run artifact, so a token in it is a
    token in whatever the artifact is uploaded to."""
    config = parse_readback_config(
        _http_config(headers={"Authorization": "env:HQ_READBACK_TOKEN"}), env=TOKEN_ENV
    )
    serialized = json.dumps(config.to_json())
    assert "s3cr3t-value" not in serialized
    assert "Authorization" in serialized


def test_the_http_adapter_sends_the_resolved_headers():
    client = _StubClient(_json_response([]))
    config = parse_readback_config(
        _http_config(headers={"Authorization": "env:HQ_READBACK_TOKEN"}), env=TOKEN_ENV
    )
    adapter = build_adapter(config, client=client)
    adapter.fetch(_probe())
    assert client.requests[0].headers["Authorization"] == "s3cr3t-value"


def test_the_http_adapter_sends_them_on_enumeration_too():
    client = _StubClient(_json_response([]))
    config = parse_readback_config(
        _http_config(headers={"Authorization": "env:HQ_READBACK_TOKEN"}), env=TOKEN_ENV
    )
    build_adapter(config, client=client).list_all()
    assert client.requests[0].headers["Authorization"] == "s3cr3t-value"


def test_a_config_with_no_headers_still_sends_none():
    client = _StubClient(_json_response([]))
    build_adapter(parse_readback_config(_http_config(), env={}), client=client).fetch(
        _probe()
    )
    assert client.requests[0].headers == {}


def test_a_custom_adapter_is_not_handed_the_secret():
    """The factory receives `to_json`, which carries header names and not
    values. A custom adapter that needs auth reads the environment itself."""
    module = types.ModuleType("hdr_spec_probe")
    module.seen = {}

    def build(config):
        module.seen["config"] = config
        return _ProbeAdapter()

    module.build = build
    sys.modules["hdr_spec_probe"] = module
    try:
        build_adapter(
            parse_readback_config(
                {
                    "kind": "hdr_spec_probe:build",
                    "headers": {"Authorization": "env:HQ_READBACK_TOKEN"},
                },
                env=TOKEN_ENV,
            )
        )
        assert module.seen["config"]["headers"] == ["Authorization"]
        assert "s3cr3t-value" not in json.dumps(module.seen["config"])
    finally:
        del sys.modules["hdr_spec_probe"]


# ---------------------------------------------------------------------------
# The shipped example stays loadable
# ---------------------------------------------------------------------------


def test_the_shipped_example_config_still_builds_a_readback_adapter():
    """The example is the first thing an operator copies, and a documentation
    block that no longer parses is worse than no documentation block: it
    produces a refusal that names a file the reader has no reason to distrust.

    Parsed and built, not merely read as text, because a table that is valid
    TOML can still be missing a url."""
    from testinghq.core.config import load_config

    root = Path(__file__).resolve().parents[2]
    config = load_config(str(root / "examples" / "target.example.toml"))

    assert "local" in config.allowed_target_names()
    table = config.readback_table()
    assert table is not None, "the example no longer documents a readback"

    built = build_adapter(parse_readback_config(table))
    assert isinstance(built, adapters.HttpJsonAdapter)
    assert built.tag_param == "tag"
    # The example's field map must actually be usable, so every name in it has
    # to be one the checker knows how to ask about.
    for name in built.field_map:
        assert name in CHECKABLE_FIELDS or name in DEFAULT_FIELD_MAP


def test_the_shipped_example_readback_url_passes_the_guardrail():
    """Otherwise the example would document a configuration its own tool
    refuses, which is the most confusing failure this repo could ship."""
    from testinghq.core.config import load_config

    root = Path(__file__).resolve().parents[2]
    config = load_config(str(root / "examples" / "target.example.toml"))
    url = config.readback_table()["url"]
    assert adapters.require_readback_target(url) == url


def test_a_mailbox_readback_config_from_the_example_paragraph_builds(tmp_path):
    """The commented-out mailbox block in the example is prose, so it cannot be
    tested by reading the file. Built from the same shape it describes, so the
    shape in the docs and the shape the loader accepts cannot drift apart."""
    sink = tmp_path / "mail-sink.jsonl"
    sink.write_text("", encoding="utf-8")
    config = parse_readback_config(
        {"kind": "mailbox", "path": str(sink)}
    )
    adapter = build_adapter(config)
    assert isinstance(adapter, adapters.MailboxAdapter)
    assert adapter.fetch(_probe()) == []


# ---------------------------------------------------------------------------
# Custom adapters by import path
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_module():
    """A throwaway module registered in `sys.modules`, so a `module:attribute`
    spec can be resolved to something a test controls.

    Registering the module object directly is what makes this hermetic: no file
    is written and nothing depends on which directory pytest happened to put on
    `sys.path`, so the test means the same thing in every import mode. The
    module is removed afterwards because leaving it registered would let a later
    test resolve a spec to this file's leftovers.
    """
    import sys
    import types

    module = types.ModuleType("testinghq_spec_probe")

    def fetch(probe):
        return [Readback(exists=True, ticket_id=f"T-{probe.tag}", tag=probe.tag)]

    module.fetch = fetch
    module.adapter = _ProbeAdapter()
    module.zero_arg_factory = lambda: _ProbeAdapter()
    module.config_factory = lambda config: _ProbeAdapter(config.get("kind"))
    module.exploding_factory = lambda config: 1 / 0
    module.not_an_adapter = "just a string"

    sys.modules["testinghq_spec_probe"] = module
    yield module
    del sys.modules["testinghq_spec_probe"]


class _ProbeAdapter:
    def __init__(self, label: str = "default") -> None:
        self.label = label

    def fetch(self, probe):
        return []

    def close(self) -> None:
        return None


def _spec(spec: str) -> adapters.ReadbackAdapter:
    return adapters.build_adapter(
        adapters.parse_readback_config({"kind": "python", "spec": spec})
    )


def test_a_spec_must_be_module_colon_attribute():
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("nocolon")
    assert "module:attribute" in str(caught.value)


def test_a_spec_with_an_empty_half_is_refused():
    for bad in (":build", "mypkg:"):
        with pytest.raises(adapters.AdapterError):
            _spec(bad)


def test_an_unimportable_spec_is_refused_with_the_module_named():
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("testinghq.no.such.module:build")
    assert "testinghq.no.such.module" in str(caught.value)


def test_a_spec_naming_a_missing_attribute_is_refused():
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("testinghq.pipeline.messages:nope")
    assert "nope" in str(caught.value)


def test_a_spec_resolving_to_an_adapter_is_used_directly(spec_module):
    """No wrapping, no calling. If it already answers `fetch`, it is an
    adapter, and a factory call would be a surprising side effect."""
    assert _spec("testinghq_spec_probe:adapter").label == "default"


def test_a_spec_resolving_to_a_fetch_function_becomes_a_function_adapter(spec_module):
    """The cheap case, and the reason writing an adapter for a one-off system
    is a function rather than a class."""
    adapter = _spec("testinghq_spec_probe:fetch")
    found = adapter.fetch(_probe("hq-9-0009"))
    assert [r.ticket_id for r in found] == ["T-hq-9-0009"]


def test_a_spec_resolving_to_a_zero_arg_factory_is_called(spec_module):
    assert isinstance(_spec("testinghq_spec_probe:zero_arg_factory"), _ProbeAdapter)


def test_a_spec_resolving_to_a_config_factory_receives_the_table(spec_module):
    adapter = _spec("testinghq_spec_probe:config_factory")
    assert adapter.label == "python"


def test_a_factory_that_raises_is_reported_as_a_factory_failure(spec_module):
    """Not as a signature problem. The first version of this called the target
    and caught TypeError, so a factory that raised TypeError from inside itself
    was called a second time with no arguments, and one that raised anything
    else was told it had the wrong arity. Both messages named a problem the
    caller did not have."""
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("testinghq_spec_probe:exploding_factory")
    assert "factory raised" in str(caught.value)
    assert "division by zero" in str(caught.value)


def test_a_spec_resolving_to_a_non_callable_non_adapter_is_refused(spec_module):
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("testinghq_spec_probe:not_an_adapter")
    assert "fetch" in str(caught.value)


def test_a_spec_target_is_classified_by_its_parameter_name():
    def as_factory(config):
        return None

    def as_fetch(probe):
        return None

    def as_zero_arg():
        return None

    def as_richer_fetch(probe, extra):
        return None

    def as_mystery(thing):
        return None

    assert adapters._classify_spec_target(as_factory, "m:f") == adapters._FACTORY_WITH_CONFIG
    assert adapters._classify_spec_target(as_fetch, "m:f") == adapters._FETCH_FUNCTION
    assert adapters._classify_spec_target(as_zero_arg, "m:f") == adapters._FACTORY_NO_ARGS
    assert adapters._classify_spec_target(as_richer_fetch, "m:f") == adapters._FETCH_FUNCTION
    with pytest.raises(adapters.AdapterError) as caught:
        adapters._classify_spec_target(as_mystery, "m:f")
    assert "probe" in str(caught.value) and "config" in str(caught.value)


def test_a_spec_target_named_ambiguously_is_refused_with_both_spellings(spec_module):
    spec_module.mystery = lambda thing: None
    with pytest.raises(adapters.AdapterError) as caught:
        _spec("testinghq_spec_probe:mystery")
    assert "'probe'" in str(caught.value)
    assert "'config'" in str(caught.value)


def test_required_positional_ignores_defaults_and_star_args():
    def with_default(a, b=None):
        return a

    def with_star(*args):
        return args

    def plain(a, b, c):
        return a

    assert adapters._required_positional(with_default) == ["a"]
    assert adapters._required_positional(with_star) == []
    assert adapters._required_positional(plain) == ["a", "b", "c"]
