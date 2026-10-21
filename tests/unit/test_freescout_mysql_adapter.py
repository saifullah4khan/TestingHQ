"""The FreeScout readback adapter, against FreeScout's tables in SQLite.

The rows come from sweep/fakes/freescout.py's store, which writes what
FreeScout's fetcher writes; the adapter reaches them through its injected
connection factory and runs its own SQL, so nothing here needs PyMySQL or a
socket.
"""
from __future__ import annotations

import importlib.util
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

from examples.readback.adapters import freescout_mysql
from examples.readback.adapters.freescout_mysql import FreescoutMysqlAdapter, parse_dsn
from testinghq.pipeline.adapters import AdapterError, build_adapter, parse_readback_config
from testinghq.pipeline.readback import Probe, can_enumerate

REPO_ROOT = Path(__file__).resolve().parents[2]
MAILBOX = "support@freescout.test"


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "thq_fake_freescout_unit", REPO_ROOT / "sweep" / "fakes" / "freescout.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = _load_fake()


def _mail(tag=None, *, subject="Printer on fire", sender="alice@hq.example.test",
          message_id=None, in_reply_to=None, attachment=None, reply_to=None,
          body="The third floor printer is smoking."):
    message = EmailMessage()
    message["From"] = f"Alice <{sender}>"
    message["To"] = MAILBOX
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id or (tag or 'untagged') + '@hq.example.test'}>"
    if reply_to:
        message["Reply-To"] = reply_to
    if in_reply_to:
        message["In-Reply-To"] = f"<{in_reply_to}>"
    if tag:
        message["X-TestingHQ-Tag"] = tag
        body = f"{body}\n[testinghq:{tag}]\n"
    message.set_content(body)
    if attachment:
        message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                               filename=attachment)
    return message.as_bytes()


def _probe(tag):
    return Probe(record_id="clean-0-0000", tag=tag, payload_sha256="0" * 64,
                 from_addr="alice@hq.example.test", subject="Printer on fire",
                 recipient=MAILBOX, message_id=f"{tag}@hq.example.test")


@pytest.fixture
def store():
    running = fake.FreescoutStore()
    yield running
    running.close()


def _adapter(store, **table):
    table.setdefault("dsn", "env:FREESCOUT_MYSQL_DSN")
    return FreescoutMysqlAdapter(
        table, connect=store.connect, env={"FREESCOUT_MYSQL_DSN": store.dsn}
    )


# ---------------------------------------------------------------------------
# Finding a message
# ---------------------------------------------------------------------------


def test_a_fetched_message_reads_back_as_its_conversation_and_thread(store):
    store.ingest(_mail("hq-1-0000", attachment="invoice.pdf"))
    adapter = _adapter(store, mailbox=MAILBOX)
    adapter.preflight()

    [record] = adapter.fetch(_probe("hq-1-0000"))
    assert record.exists is True
    assert record.ticket_id == "1"
    assert record.from_addr == "alice@hq.example.test"
    assert record.subject == "Printer on fire"
    assert "The third floor printer is smoking.<br />" in record.body
    assert record.attachment_names == ("invoice.pdf",)
    assert record.route == MAILBOX
    assert record.message_id == "hq-1-0000@hq.example.test"
    assert record.tag == "hq-1-0000"
    assert set(record.fields) == {
        "from_addr", "subject", "body", "attachment_names", "route", "message_id", "tag",
    }


def test_a_tag_that_begins_another_tag_finds_only_its_own_message(store):
    store.ingest(_mail("hq-5-1000"))
    store.ingest(_mail("hq-5-10000"))
    adapter = _adapter(store)
    assert [r.tag for r in adapter.fetch(_probe("hq-5-1000"))] == ["hq-5-1000"]
    assert [r.tag for r in adapter.fetch(_probe("hq-5-10000"))] == ["hq-5-10000"]


def test_a_message_stored_twice_is_two_records(store):
    store.ingest(_mail("hq-1-0001", message_id="first@hq.example.test"))
    store.ingest(_mail("hq-1-0001", message_id="second@hq.example.test"))
    found = _adapter(store).fetch(_probe("hq-1-0001"))
    assert [r.ticket_id for r in found] == ["1", "2"]


def test_the_tag_is_found_in_the_body_when_the_headers_lost_it(store):
    store.ingest(_mail("hq-1-0002"))
    store.query("UPDATE threads SET headers = 'Subject: Printer on fire'")
    [record] = _adapter(store).fetch(_probe("hq-1-0002"))
    assert record.tag == "hq-1-0002"


def test_nothing_found_is_an_empty_result(store):
    store.ingest(_mail("hq-1-0003"))
    assert list(_adapter(store).fetch(_probe("hq-1-9999"))) == []


def test_a_threaded_reply_has_its_conversation_number_and_no_subject_of_its_own(store):
    store.ingest(_mail("hq-2-0000"))
    store.ingest(_mail("hq-2-0001", subject="Re: Printer on fire",
                       in_reply_to="hq-2-0000@hq.example.test"))
    [reply] = _adapter(store).fetch(_probe("hq-2-0001"))
    assert reply.ticket_id == "1"
    assert reply.subject is None
    assert "subject" not in reply.fields
    assert reply.has("body")


def test_the_sender_is_whatever_freescout_stored_for_the_thread(store):
    store.ingest(_mail("hq-3-0000", reply_to="desk@hq.example.test"))
    [record] = _adapter(store).fetch(_probe("hq-3-0000"))
    assert record.from_addr == "desk@hq.example.test"


def test_a_deleted_conversation_is_a_record_that_no_longer_exists(store):
    store.ingest(_mail("hq-4-0000"))
    store.delete_conversation(1)
    [record] = _adapter(store).fetch(_probe("hq-4-0000"))
    assert record.exists is False
    assert record.ticket_id == "1"


def test_the_mailbox_setting_keeps_other_mailboxes_out(store):
    store.ingest(_mail("hq-6-0000"))
    store.ingest(_mail("hq-6-0001"))
    store.query("INSERT INTO mailboxes (name, email) VALUES ('Sales', 'sales@freescout.test')")
    store.query("UPDATE conversations SET mailbox_id = 2 WHERE number = 2")
    adapter = _adapter(store, mailbox=MAILBOX)
    assert adapter.fetch(_probe("hq-6-0001")) == []
    assert [r.tag for r in adapter.list_all()] == ["hq-6-0000"]
    assert [r.route for r in _adapter(store).fetch(_probe("hq-6-0001"))] == ["sales@freescout.test"]


def test_listing_returns_every_message_tagged_or_not(store):
    store.ingest(_mail("hq-7-0000"))
    store.ingest(_mail(None, subject="Lunch?", message_id="lunch@hq.example.test"))
    adapter = _adapter(store)
    assert can_enumerate(adapter)
    assert [(r.ticket_id, r.tag) for r in adapter.list_all()] == [("1", "hq-7-0000"), ("2", None)]


def test_a_field_switched_off_is_not_visible(store):
    store.ingest(_mail("hq-8-0000", attachment="invoice.pdf"))
    [record] = _adapter(store, unmapped_fields=["attachment_names"]).fetch(_probe("hq-8-0000"))
    assert "attachment_names" not in record.fields
    assert not record.has("attachment_names")


# ---------------------------------------------------------------------------
# The DSN and the connection
# ---------------------------------------------------------------------------


def test_a_literal_dsn_is_refused_when_the_adapter_is_built():
    with pytest.raises(AdapterError, match="literal value"):
        FreescoutMysqlAdapter({"dsn": "mysql://u:hunter2@db/freescout"})


def test_a_missing_dsn_is_refused():
    with pytest.raises(AdapterError, match="needs \\[readback\\].dsn"):
        FreescoutMysqlAdapter({})


def test_an_unset_dsn_variable_is_refused_by_preflight_before_any_send(store):
    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:FREESCOUT_MYSQL_DSN"}, connect=store.connect, env={}
    )
    with pytest.raises(AdapterError, match="FREESCOUT_MYSQL_DSN.*not set"):
        adapter.preflight()
    assert store.connections == 0


def test_the_dsn_becomes_pymysql_keywords():
    params = parse_dsn("mysql://reader:p%40ss%3Aw@db.internal:3407/help?charset=utf8", timeout=4)
    assert params == {
        "host": "db.internal", "port": 3407, "user": "reader", "password": "p@ss:w",
        "database": "help", "charset": "utf8", "connect_timeout": 4, "read_timeout": 4,
        "write_timeout": 4, "autocommit": True,
    }
    assert parse_dsn("mariadb://reader@db/help")["port"] == 3306


@pytest.mark.parametrize("dsn", [
    "postgres://reader:hunter2@db/help",
    "mysql://reader:hunter2@/help",
    "mysql://:hunter2@db/help",
    "mysql://reader:hunter2@db",
    "mysql://reader:hunter2@db:port/help",
])
def test_a_malformed_dsn_is_refused_without_repeating_the_password(dsn):
    with pytest.raises(AdapterError) as caught:
        parse_dsn(dsn)
    assert "hunter2" not in str(caught.value)


def test_a_refused_login_is_an_adapter_error_naming_the_server_not_the_password(store):
    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:DSN"}, connect=store.connect,
        env={"DSN": "mysql://thq_readback:wrong-secret@127.0.0.1:3406/freescout"},
    )
    with pytest.raises(AdapterError) as caught:
        adapter.preflight()
    message = str(caught.value)
    assert "127.0.0.1:3406/freescout" in message and "Access denied" in message
    assert "wrong-secret" not in message


def test_without_pymysql_the_error_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:DSN"}, env={"DSN": "mysql://reader:x@127.0.0.1/freescout"}
    )
    with pytest.raises(AdapterError, match="pip install PyMySQL"):
        adapter.preflight()


def test_a_dropped_connection_is_reopened_once(store):
    store.ingest(_mail("hq-9-0000"))
    opened = []

    def connect(**params):
        connection = store.connect(**params)
        opened.append(connection)
        return connection

    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:DSN"}, connect=connect, env={"DSN": store.dsn}
    )
    adapter.fetch(_probe("hq-9-0000"))
    opened[0].close()
    assert [r.tag for r in adapter.fetch(_probe("hq-9-0000"))] == ["hq-9-0000"]
    assert len(opened) == 2
    adapter.close()
    assert opened[1].closed


def test_a_query_that_keeps_failing_is_an_adapter_error(store):
    class Broken:
        def cursor(self):
            raise RuntimeError("server has gone away")

        def close(self):
            pass

    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:DSN"}, connect=lambda **_: Broken(), env={"DSN": store.dsn}
    )
    with pytest.raises(AdapterError, match="server has gone away"):
        adapter.fetch(_probe("hq-9-0001"))


# ---------------------------------------------------------------------------
# Preflight and the table prefix
# ---------------------------------------------------------------------------


def test_preflight_refuses_a_mailbox_freescout_does_not_have(store):
    adapter = _adapter(store, mailbox="nobody@freescout.test")
    with pytest.raises(AdapterError, match="no mailbox with the address"):
        adapter.preflight()


def test_preflight_refuses_a_database_without_freescout_tables(store):
    adapter = _adapter(store, table_prefix="fs_")
    with pytest.raises(AdapterError, match="table_prefix"):
        adapter.preflight()


def test_the_table_prefix_is_put_on_every_table(store):
    seen = []

    class Recording:
        def cursor(self):
            return self

        def execute(self, sql, args=()):
            seen.append(sql)

        def fetchall(self):
            return []

        def close(self):
            pass

    adapter = FreescoutMysqlAdapter(
        {"dsn": "env:DSN", "table_prefix": "fs_"}, connect=lambda **_: Recording(),
        env={"DSN": store.dsn},
    )
    adapter.fetch(_probe("hq-1-0000"))
    for table in ("threads", "conversations", "mailboxes"):
        assert f"fs_{table} " in seen[0]


@pytest.mark.parametrize("prefix", ["fs-", "fs_; DROP TABLE users", 7])
def test_a_table_prefix_that_is_not_an_identifier_is_refused(prefix):
    with pytest.raises(AdapterError, match="table_prefix"):
        FreescoutMysqlAdapter({"dsn": "env:DSN", "table_prefix": prefix})


# ---------------------------------------------------------------------------
# Through the loader, as a run builds it
# ---------------------------------------------------------------------------


def test_the_loader_builds_it_from_a_readback_table_with_its_own_keys(store, monkeypatch):
    raw = {
        "kind": "examples.readback.adapters.freescout_mysql:FreescoutMysqlAdapter",
        "dsn": "env:FREESCOUT_MYSQL_DSN",
        "mailbox": MAILBOX,
        "fields": {"attachment_names": False},
    }
    monkeypatch.setenv("FREESCOUT_MYSQL_DSN", store.dsn)
    monkeypatch.setattr(freescout_mysql, "pymysql_connect", store.connect)
    adapter = build_adapter(parse_readback_config(raw))
    assert isinstance(adapter, FreescoutMysqlAdapter)
    assert adapter.mailbox == MAILBOX
    store.ingest(_mail("hq-1-0004", attachment="invoice.pdf"))
    [record] = adapter.fetch(_probe("hq-1-0004"))
    assert "attachment_names" not in record.fields


# ---------------------------------------------------------------------------
# What the fake stores, as FreeScout's fetcher would
# ---------------------------------------------------------------------------


def _dated(raw, date):
    return raw.replace(b"Subject:", f"Date: {date}\nSubject:".encode(), 1)


def test_a_message_id_seen_before_is_skipped_unless_the_date_differs_too(store):
    first = _dated(_mail("hq-10-0000"), "Mon, 19 Oct 2026 09:00:00 +0000")
    assert store.ingest(first) is not None
    assert store.ingest(first) is None
    reused = _dated(_mail("hq-10-0000", subject="Another"), "Mon, 19 Oct 2026 10:00:00 +0000")
    assert store.ingest(reused) is not None
    assert len(_adapter(store).fetch(_probe("hq-10-0000"))) == 2


@pytest.mark.skipif(sys.platform == "win32",
                    reason="a fetched Maildir name ends ':2,S', which Windows cannot hold")
def test_one_fetch_is_stored_in_date_order(store, tmp_path):
    maildir = tmp_path / "Maildir"
    for sub in ("cur", "new", "tmp"):
        (maildir / sub).mkdir(parents=True)
    (maildir / "new" / "1.a").write_bytes(
        _dated(_mail("hq-11-0001"), "Mon, 19 Oct 2026 11:00:00 +0000"))
    (maildir / "new" / "2.b").write_bytes(
        _dated(_mail("hq-11-0000"), "Mon, 19 Oct 2026 10:00:00 +0000"))
    assert store.fetch_maildir(maildir) == 2
    assert [(r.ticket_id, r.tag) for r in _adapter(store).list_all()] == [
        ("1", "hq-11-0000"), ("2", "hq-11-0001"),
    ]
    assert sorted(p.name for p in (maildir / "cur").iterdir()) == ["1.a:2,S", "2.b:2,S"]
