"""The sweep's FreeScout recipe, through the real CLI, against the fake stack.

sweep/fakes/freescout.py runs the deliver sidecar's SMTP server over a real
loopback socket, fetches what it writes into the Maildir the way FreeScout's
fetcher does, and keeps the rows in SQLite under FreeScout's table names. The
recipe's readback adapter runs its own SQL against them: the only stand-in is
the `pymysql` module, replaced by one whose `connect` checks the DSN's
credentials and hands back the fake's connection.
"""
from __future__ import annotations

import importlib.util
import json
import re
import smtplib
import sys
from pathlib import Path

import pytest

from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_OK

pytestmark = [
    pytest.mark.allow_network,
    # The fake fetches the way Dovecot does, moving each message into cur/
    # under a name ending ':2,S', which Windows cannot hold. The sidecar and
    # the fetch run on Linux in the real stack.
    pytest.mark.skipif(sys.platform == "win32",
                       reason="a fetched Maildir name ends ':2,S', which Windows cannot hold"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SWEEP_CONFIG = REPO_ROOT / "sweep" / "configs" / "freescout.toml"
MAILBOX = "support@freescout.test"


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "thq_fake_freescout", REPO_ROOT / "sweep" / "fakes" / "freescout.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_freescout = _load_fake()


@pytest.fixture
def fake(monkeypatch):
    with fake_freescout.FakeFreescout() as running:
        monkeypatch.setenv("FREESCOUT_MYSQL_DSN", running.dsn)
        monkeypatch.setitem(sys.modules, "pymysql", running.pymysql_module())
        yield running


def _config_for(fake, tmp_path) -> str:
    """The sweep config with its SMTP port pointed at the fake, and nothing
    else changed."""
    text = SWEEP_CONFIG.read_text(encoding="utf-8")
    pointed, count = re.subn(r"(?m)^port = \d+$", f"port = {fake.smtp_port}", text)
    assert count == 1, "the sweep config should set one SMTP port"
    path = tmp_path / "freescout.toml"
    path.write_text(pointed, encoding="utf-8")
    return str(path)


def _statuses(artifact):
    statuses = {}
    for record in artifact["records"]:
        for check in record["verification"]["checks"]:
            statuses.setdefault(check["check"], set()).add((check["status"], check["reason"]))
    return statuses


def test_verify_passes_through_smtp_the_maildir_and_the_database(fake, tmp_path, capsys):
    out = tmp_path / "verify.json"
    code = cli_main([
        "verify", "fire", "--send", "--target", "freescout",
        "--config", _config_for(fake, tmp_path), "--count", "8", "--rate", "200",
        "--expect-route", MAILBOX, "--quiet-window", "0.3", "--poll-interval", "0.05",
        "--out", str(out),
    ])
    printed = capsys.readouterr()
    assert code == EXIT_OK, printed.out + printed.err

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    assert artifact["config"]["transport"] == "smtp"
    assert all(r["response"]["status"] == 250 for r in artifact["records"])

    statuses = _statuses(artifact)
    for name in ("ticket_created", "sender", "subject", "body", "routing"):
        assert statuses[name] == {("passed", None)}, (name, statuses[name])
    assert ("passed", None) in statuses["attachments"]

    # Every message went through the Maildir and was taken from it, the way
    # Dovecot leaves a message FreeScout has fetched.
    assert not list((fake.maildir / "new").iterdir())
    seen = list((fake.maildir / "cur").iterdir())
    assert len(seen) == 8 and all(p.name.endswith(":2,S") for p in seen)
    # One conversation per message, numbered the way FreeScout numbers them.
    numbers = [row[0] for row in fake.store.query("SELECT number FROM conversations ORDER BY id")]
    assert numbers == list(range(1, 9))


def test_ledger_balances_and_searches_the_whole_store(fake, tmp_path, capsys):
    """The listing half of the adapter: every thread is enumerated, so the
    ledger can say nothing else was found rather than that it did not look."""
    out = tmp_path / "ledger.json"
    code = cli_main([
        "ledger", "fire", "--send", "--target", "freescout",
        "--config", _config_for(fake, tmp_path), "--count", "6", "--rate", "200",
        "--quiet-window", "0.3", "--poll-interval", "0.05", "--out", str(out),
    ])
    printed = capsys.readouterr()
    assert code == EXIT_OK, printed.out + printed.err
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
    assert summary["balanced"] is True
    assert summary["strays_searched"] is True
    assert summary["exactly_once"] == 6 and summary["extra_count"] == 0


def test_the_sidecar_unstuffs_dots_and_keeps_the_smtp_order(fake):
    """What the deliver service writes is the message the client meant: a
    body line starting with a dot arrives with its one dot, and commands out
    of order are refused rather than delivered."""
    with smtplib.SMTP("127.0.0.1", fake.smtp_port) as client:
        assert client.docmd("RCPT", "TO:<support@freescout.test>")[0] == 503
        client.rset()
        client.sendmail(
            "alice@hq.example.test", ["support@freescout.test"],
            b"From: alice@hq.example.test\r\nSubject: dots\r\n"
            b"Message-ID: <dots@hq.example.test>\r\n\r\n.leading dot\r\n..two\r\n",
        )
    assert fake.fetch() == 1
    [stored] = list((fake.maildir / "cur").iterdir())
    assert stored.read_bytes().endswith(b"\n\n.leading dot\n..two\n")


@pytest.mark.parametrize("seed", [0, 7])
def test_redeliver_threads_survive_freescouts_date_ordered_fetch(fake, tmp_path, capsys, seed):
    """FreeScout processes one fetch in Date order, so every message of a
    scenario is filed in Date order however it was delivered. With each
    payload's generated Date, a reply could be dated before its original, be
    filed first and open its own conversation, and the scenario blamed
    FreeScout for it. Dated in thread order, both threads land whole: the
    reply-first replies are delivered first and still filed after their root."""
    out = tmp_path / "redeliver.json"
    code = cli_main([
        "redeliver", "fire", "--send", "--target", "freescout",
        "--config", _config_for(fake, tmp_path), "--seed", str(seed),
        "--scenario", "reply-first", "--scenario", "references", "--rate", "200",
        "--quiet-window", "0.3", "--poll-interval", "0.05", "--out", str(out),
    ])
    printed = capsys.readouterr()
    assert code == EXIT_OK, printed.out + printed.err

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "DELIVERY-SAFE"
    assert [(s["scenario"], s["passed"], s["findings"]) for s in artifact["scenarios"]] == [
        ("reply-first", True, []), ("references", True, []),
    ]
    # Two conversations of three threads each, one per scenario.
    rows = fake.store.query(
        "SELECT conversation_id, COUNT(*) FROM threads GROUP BY conversation_id"
    )
    assert sorted(count for _id, count in rows) == [3, 3]
