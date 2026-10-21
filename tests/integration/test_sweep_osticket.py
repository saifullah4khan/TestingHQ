"""The osTicket recipes, through the real CLI, against the osTicket fake.

sweep/fakes/osticket.py answers POST /api/tickets.email over a real loopback
socket and keeps what it files in SQLite under osTicket's table names. The
readback is the example adapter in examples/readback/adapters/osticket_mysql.py,
loaded from the recipe's `kind` exactly as a run loads it, with its connection
factory pointed at the fake's tables instead of PyMySQL.

The config files are used as they are. The target url comes from the
TESTINGHQ_TARGET_OSTICKET_URL override and the secrets from the environment,
which is how a run against a real osTicket gets them too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.readback.adapters import osticket_mysql
from sweep.fakes.osticket import FakeOsTicket
from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_OK

pytestmark = pytest.mark.allow_network

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = [
    REPO_ROOT / "sweep" / "configs" / "osticket.toml",
    REPO_ROOT / "examples" / "readback" / "osticket.toml",
]
POLL = ["--quiet-window", "0.5", "--max-wait", "30", "--poll-interval", "0.1"]


@pytest.fixture
def fake(monkeypatch):
    with FakeOsTicket() as server:
        monkeypatch.setattr(osticket_mysql, "default_connect", server.connect)
        monkeypatch.setenv("TESTINGHQ_TARGET_OSTICKET_URL", server.url)
        monkeypatch.setenv("OSTICKET_API_KEY", server.api_key)
        monkeypatch.setenv(
            "OSTICKET_MYSQL_DSN", "mysql://thq_readback:unused@127.0.0.1:3206/osticket"
        )
        yield server


def _run(tool, config, out, *extra):
    return cli_main([
        tool, "fire", "--target", "osticket", "--send", "--config", str(config),
        "--rate", "1000", "--out", str(out), *POLL, *extra,
    ])


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.parent.name)
def test_verify_passes_through_the_api_and_the_mysql_readback(fake, config, tmp_path, capsys):
    assert cli_main(["config", "validate", str(config)]) == EXIT_OK
    out = tmp_path / "verify.json"
    code = _run("verify", config, out, "--count", "8")
    assert code == EXIT_OK, capsys.readouterr().out

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    assert artifact["summary"]["verified"] == artifact["summary"]["sent"] == 8
    assert artifact["config"]["format"] == "mime"

    # Every send carried the key, every one was filed, and the number the API
    # answered with is the ticket the readback found.
    posts = [r for r in fake.requests if r.path == "/api/tickets.email"]
    assert len(posts) == 8
    assert all(r.headers.get("x-api-key") == fake.api_key for r in posts)
    for record in artifact["records"]:
        assert record["response"]["status"] == 201
        [found] = record["readback"]
        assert found["ticket_id"] == record["response"]["body_snippet"]
        assert found["tag"] == record["tag"]


def test_ledger_balances_and_counts_the_installer_ticket_as_untagged(fake, tmp_path, capsys):
    out = tmp_path / "ledger.json"
    code = _run("ledger", CONFIGS[0], out, "--count", "12")
    assert code == EXIT_OK, capsys.readouterr().out
    printed = capsys.readouterr().out
    assert "BALANCED" in printed
    assert "1 untagged record(s)" in printed


def test_a_key_osticket_does_not_know_is_refused_and_nothing_is_filed(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("OSTICKET_API_KEY", "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF")
    before = fake.store.ticket_numbers()
    out = tmp_path / "verify.json"
    code = _run("verify", CONFIGS[0], out, "--count", "2")
    assert code != EXIT_OK
    assert {r.status for r in fake.requests} == {401}
    assert fake.store.ticket_numbers() == before
