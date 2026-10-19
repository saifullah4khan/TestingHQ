"""The osTicket MySQL readback example adapter, against a SQLite copy of
osTicket's tables.

The tables are the ones sweep/fakes/osticket.py keeps, filled by the same
email intake the fake's HTTP endpoint runs, so a message reaches them the way
it reaches osTicket: parsed from the raw bytes TestingHQ's mime format sends.
No socket is opened; the adapter is handed the store's connection factory.
"""
from __future__ import annotations

import dataclasses
import importlib
import sqlite3
import sys

import pytest

from examples.readback.adapters import osticket_mysql
from examples.readback.adapters.osticket_mysql import (
    OsTicketMysqlAdapter,
    OsTicketReadbackError,
    parse_dsn,
)
from sweep.fakes.osticket import OsTicketStore
from testinghq.blast.generate import generate_corpus
from testinghq.core.formats import get_format
from testinghq.core.transport import DEFAULT_BOUNDARY
from testinghq.pipeline.adapters import AdapterError, build_adapter, parse_readback_config
from testinghq.pipeline.common import prepare_readback
from testinghq.pipeline.messages import make_tag, probe_for, stamp
from testinghq.pipeline.readback import can_enumerate

MODULE = "examples.readback.adapters.osticket_mysql"
SPEC = f"{MODULE}:OsTicketMysqlAdapter"
DSN = "mysql://reader:s%40cret@127.0.0.1:3206/osticket"
ENV = {"OSTICKET_MYSQL_DSN": DSN}
TABLE = {"kind": SPEC, "dsn": "env:OSTICKET_MYSQL_DSN"}

CORPUS = generate_corpus(7, 6)
MIME = get_format("mime", DEFAULT_BOUNDARY)


@pytest.fixture
def store(tmp_path):
    return OsTicketStore(str(tmp_path / "osticket.sqlite3"))


def _adapter(store, **table):
    return OsTicketMysqlAdapter({**TABLE, **table}, connect=store.connect, env=ENV)


def _send(store, index=0, tag=None, **pinned):
    """Stamp corpus message `index`, file it, return its probe."""
    tag = tag or make_tag("hq-test", 7, index)
    email = stamp(CORPUS[index], tag, **pinned)
    store.ingest(MIME.encode(email).body)
    return probe_for(email, tag, f"clean-7-{index:04d}", "0" * 64), email


def _sql(store, statement, params=()):
    with sqlite3.connect(store.db_path) as db:
        db.execute(statement, params)


# ---------------------------------------------------------------------------
# Loading: no PyMySQL needed, no literal DSN, nothing read before preflight
# ---------------------------------------------------------------------------


def test_the_module_imports_and_the_adapter_builds_without_pymysql(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    fresh = importlib.import_module(MODULE)
    adapter = build_adapter(parse_readback_config(dict(TABLE)))
    assert type(adapter).__name__ == "OsTicketMysqlAdapter"
    assert type(adapter).__module__ == fresh.__name__


def test_a_run_without_pymysql_is_refused_at_preflight_naming_the_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.setenv("OSTICKET_MYSQL_DSN", DSN)
    with pytest.raises(OsTicketReadbackError, match="pip install PyMySQL"):
        prepare_readback(dict(TABLE))


def test_a_literal_dsn_is_refused_when_the_config_loads():
    table = {**TABLE, "dsn": DSN}
    with pytest.raises(AdapterError, match="literal"):
        build_adapter(parse_readback_config(table))


def test_a_missing_dsn_is_refused():
    with pytest.raises(AdapterError, match="dsn is required"):
        build_adapter(parse_readback_config({"kind": SPEC}))


def test_an_unset_dsn_variable_is_refused_before_connecting():
    calls = []

    def connect(dsn, timeout):  # pragma: no cover - must not be reached
        calls.append(dsn)

    adapter = OsTicketMysqlAdapter(dict(TABLE), connect=connect, env={})
    with pytest.raises(OsTicketReadbackError, match="not set"):
        adapter.preflight()
    assert calls == []


@pytest.mark.parametrize("prefix", ["ost_; DROP TABLE x", "ost-", "o st_"])
def test_a_table_prefix_that_is_not_a_plain_name_is_refused(prefix):
    with pytest.raises(OsTicketReadbackError, match="table_prefix"):
        OsTicketMysqlAdapter({**TABLE, "table_prefix": prefix})


def test_the_dsn_is_parsed_with_its_escapes_and_a_default_port():
    dsn = parse_dsn(DSN)
    assert (dsn.host, dsn.port, dsn.user, dsn.password, dsn.database) == (
        "127.0.0.1", 3206, "reader", "s@cret", "osticket"
    )
    assert parse_dsn("mysql://r@db/osticket").port == 3306
    assert "s@cret" not in repr(dsn)


@pytest.mark.parametrize(
    "dsn", ["postgres://r@db/osticket", "mysql://r@db/", "mysql:///osticket"]
)
def test_a_dsn_that_is_not_one_mysql_database_is_refused(dsn):
    with pytest.raises(OsTicketReadbackError):
        parse_dsn(dsn)


def test_preflight_names_the_missing_tables_and_the_prefix(store):
    adapter = _adapter(store, table_prefix="osx_")
    with pytest.raises(OsTicketReadbackError, match="osx_ticket.*table_prefix"):
        adapter.preflight()


def test_preflight_passes_on_an_installed_store(store):
    _adapter(store).preflight()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_a_filed_message_reads_back_with_every_field(store):
    probe, email = _send(store, index=1)
    [record] = _adapter(store).fetch(probe)
    assert record.ticket_id in store.ticket_numbers()
    assert record.subject == email.subject
    assert record.from_addr == email.ground_truth.from_addr.lower()
    assert f"[testinghq:{probe.tag}]" in record.body
    assert record.route == "Support"
    assert record.message_id == probe.message_id
    assert record.tag == probe.tag
    assert set(record.attachment_names) == {a.filename for a in email.attachments}
    assert {"subject", "from_addr", "body", "route", "message_id", "tag",
            "attachment_names"} <= set(record.fields)


def test_a_tag_osticket_never_filed_is_not_found(store):
    probe, _ = _send(store, index=0)
    unknown = dataclasses.replace(
        probe, tag="hq-test-7-9999", message_id="hq-test-7-9999@hq.example.test"
    )
    assert _adapter(store).fetch(unknown) == []


def test_a_message_filed_twice_reads_back_twice(store):
    probe, email = _send(store, index=0)
    duplicate = stamp(CORPUS[0], probe.tag, message_id="<second-copy@hq.example.test>")
    store.ingest(MIME.encode(duplicate).body)
    found = _adapter(store).fetch(probe)
    assert len(found) == 2
    assert len({record.ticket_id for record in found}) == 2


def test_a_redelivered_message_id_reads_back_once(store):
    probe, email = _send(store, index=0)
    store.ingest(MIME.encode(email).body)
    assert len(_adapter(store).fetch(probe)) == 1


def test_a_reply_reads_back_as_its_own_entry_on_the_original_ticket(store):
    original, _ = _send(store, index=0)
    reply_tag = make_tag("hq-test", 7, 1)
    reply, _ = _send(
        store, index=1, tag=reply_tag,
        in_reply_to=f"<{original.message_id}>", references=f"<{original.message_id}>",
    )
    adapter = _adapter(store)
    [first] = adapter.fetch(original)
    [second] = adapter.fetch(reply)
    assert second.ticket_id == first.ticket_id
    assert f"[testinghq:{reply_tag}]" in second.body
    assert f"[testinghq:{original.tag}]" not in second.body
    assert second.in_reply_to == original.message_id
    assert second.references == (original.message_id,)


def test_a_message_whose_body_lost_the_marker_is_found_by_its_message_id(store):
    probe, _ = _send(store, index=0)
    _sql(store, "UPDATE ost_thread_entry SET body = 'rewritten' WHERE body LIKE ?",
         (f"%{probe.tag}%",))
    [record] = _adapter(store).fetch(probe)
    assert record.body == "rewritten"


def test_like_wildcards_in_a_tag_match_only_that_tag(store):
    _send(store, index=0, tag="hq-axb-7-0000")
    probe, _ = _send(store, index=1, tag="hq-a_b-7-0000")
    found = _adapter(store).fetch(probe)
    assert [record.tag for record in found] == ["hq-a_b-7-0000"]


def test_the_ticket_subject_is_used_when_the_entry_has_no_title(store):
    probe, email = _send(store, index=0)
    _sql(store, "UPDATE ost_thread_entry SET title = NULL")
    [record] = _adapter(store).fetch(probe)
    assert record.subject == email.subject


# ---------------------------------------------------------------------------
# Listing, transactions, reconnecting
# ---------------------------------------------------------------------------


def test_the_listing_holds_every_message_entry_including_untagged_ones(store):
    probes = [_send(store, index=i)[0] for i in range(3)]
    listed = _adapter(store).list_all()
    assert not listed.partial
    assert {r.tag for r in listed} == {p.tag for p in probes} | {None}


def test_a_listing_past_list_limit_says_it_is_partial(store):
    for i in range(3):
        _send(store, index=i)
    listed = _adapter(store, list_limit=2).list_all()
    assert len(listed) == 2 and listed.partial


def test_enumerate_false_turns_the_listing_off(store):
    assert can_enumerate(_adapter(store))
    assert not can_enumerate(_adapter(store, enumerate=False))


class _Recording:
    """A connection that records how each read ended."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def cursor(self):
        return self._inner.cursor()

    def rollback(self):
        self._log.append("rollback")
        self._inner.rollback()

    def close(self):
        self._inner.close()


def test_every_read_ends_its_transaction_so_a_later_poll_sees_new_tickets(store):
    log = []

    def connect(dsn, timeout):
        return _Recording(store.connect(), log)

    connect.paramstyle = "qmark"
    adapter = OsTicketMysqlAdapter(dict(TABLE), connect=connect, env=ENV)
    probe, _ = _send(store, index=0)
    reads_before = len(log)
    adapter.fetch(probe)
    assert len(log) > reads_before
    later, _ = _send(store, index=1)
    assert len(adapter.fetch(later)) == 1


def test_a_dropped_connection_is_reopened_once(store):
    opened = []

    class _Dead:
        def cursor(self):
            raise sqlite3.OperationalError("server has gone away")

        def close(self):
            return None

    def connect(dsn, timeout):
        opened.append(dsn)
        return _Dead() if len(opened) == 1 else store.connect()

    connect.paramstyle = "qmark"
    probe, _ = _send(store, index=0)
    adapter = OsTicketMysqlAdapter(dict(TABLE), connect=connect, env=ENV)
    assert len(adapter.fetch(probe)) == 1
    assert len(opened) == 2


def test_the_default_connection_factory_is_what_the_loader_uses(store, monkeypatch):
    monkeypatch.setattr(osticket_mysql, "default_connect", store.connect)
    monkeypatch.setenv("OSTICKET_MYSQL_DSN", DSN)
    probe, _ = _send(store, index=0)
    adapter = prepare_readback(dict(TABLE))
    assert len(adapter.fetch(probe)) == 1
