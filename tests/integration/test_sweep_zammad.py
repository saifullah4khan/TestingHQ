"""The Zammad recipes, through the real CLI, against sweep/fakes/zammad.py.

The fake takes mail over SMTP the way the Mailpit sidecar does, turns each
message into a ticket the way Zammad's email channel does, and answers the
ticket search in Zammad's expanded JSON shape. Each recipe is copied into
tmp_path with its ports pointed at the fake, and `verify fire --send` must come
back VERIFIED: the token header reached the API, every message was found by its
tag, and the sender, subject and group read out of the expanded ticket match
what was sent.

Every socket is on 127.0.0.1.
"""
from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK, EXIT_REFUSED

pytestmark = pytest.mark.allow_network

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_PATH = REPO_ROOT / "sweep" / "fakes" / "zammad.py"
RECIPES = [
    REPO_ROOT / "sweep" / "configs" / "zammad.toml",
    REPO_ROOT / "examples" / "readback" / "zammad.toml",
]
TOKEN = "sweep-test-token"


def _load_fake():
    spec = importlib.util.spec_from_file_location("sweep_fake_zammad", FAKE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


zammad_fake = _load_fake()


def _pointed_at(recipe: Path, fake, tmp_path: Path) -> str:
    """The recipe with its SMTP port and API origin replaced by the fake's."""
    text = recipe.read_text(encoding="utf-8")
    text, ports = re.subn(r"(?m)^port = \d+$", f"port = {fake.smtp_port}", text)
    text, urls = re.subn(r"http://127\.0\.0\.1:\d+/", f"{fake.url}/", text)
    assert ports == 1 and urls == 1, (recipe, ports, urls)
    path = tmp_path / recipe.name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _verify(config: str, out: Path, *extra: str) -> int:
    return cli_main([
        "verify", "fire", "--send", "--target", "zammad", "--config", config,
        "--count", "6", "--rate", "200", "--readback-rate", "50", "--expect-route", "Users",
        "--quiet-window", "0.3", "--poll-interval", "0.05", "--max-wait", "20",
        "--out", str(out), *extra,
    ])


def _check_statuses(artifact: dict) -> dict:
    statuses: dict = {}
    for record in artifact["records"]:
        for check in record["verification"]["checks"]:
            statuses.setdefault(check["check"], set()).add((check["status"], check["reason"]))
    return statuses


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_verify_passes_against_the_fake(recipe, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZAMMAD_AUTH", f"Token token={TOKEN}")
    out = tmp_path / "verify.json"
    with zammad_fake.ZammadFake(TOKEN) as fake:
        config = _pointed_at(recipe, fake, tmp_path)
        assert cli_main(["config", "validate", config]) == EXIT_OK
        code = _verify(config, out)
        printed = capsys.readouterr()
        received, tickets, requests = fake.received, fake.tickets, fake.requests
    assert code == EXIT_OK, printed.out + printed.err

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    assert artifact["config"]["transport"] == "smtp"
    assert len(received) == len(tickets) == 6
    assert all(m.envelope_to == ("support@zammad.test",) for m in received)
    assert {m.peer for m in received} == {"127.0.0.1"}

    # Every API call carried the token; every lookup asked for the expanded
    # ticket and searched for one of the tags that was sent.
    assert requests and all(auth == f"Token token={TOKEN}" for _, _, auth in requests)
    sent_tags = {record["tag"] for record in artifact["records"]}
    searched = set()
    for _, path, _ in requests:
        assert path.startswith("/api/v1/tickets/search?")
        assert "expand=true" in path
        searched.update(re.findall(r"query=([^&]+)", path))
    assert sent_tags <= searched

    statuses = _check_statuses(artifact)
    for name in ("ticket_created", "sender", "subject", "routing"):
        assert statuses[name] == {("passed", None)}, (name, statuses[name])
    for name in ("body", "attachments"):
        assert statuses[name] == {("skipped", "adapter cannot see field")}, (name, statuses[name])


def test_a_wrong_customer_is_caught_through_the_expanded_field(tmp_path, monkeypatch, capsys):
    """The sender check reads `customer` from the expanded ticket, so a ticket
    filed under someone else fails it rather than passing by default."""

    class MisfilingZammad(zammad_fake.ZammadFake):
        def _customer(self, address):
            return 99, "someone.else@zammad.test"

    monkeypatch.setenv("ZAMMAD_AUTH", f"Token token={TOKEN}")
    out = tmp_path / "verify.json"
    with MisfilingZammad(TOKEN) as fake:
        code = _verify(_pointed_at(RECIPES[0], fake, tmp_path), out)
        capsys.readouterr()
    assert code == EXIT_FINDING
    statuses = _check_statuses(json.loads(out.read_text(encoding="utf-8")))
    assert {status for status, _ in statuses["sender"]} == {"failed"}
    assert statuses["subject"] == {("passed", None)}


def test_tickets_that_appear_only_on_the_next_fetch_still_verify(tmp_path, monkeypatch, capsys):
    """Pull mail: nothing exists until the channel fetches. A quiet window
    longer than the fetch period waits for it."""
    monkeypatch.setenv("ZAMMAD_AUTH", f"Token token={TOKEN}")
    out = tmp_path / "verify.json"
    with zammad_fake.ZammadFake(TOKEN, fetch_interval=0.4) as fake:
        code = _verify(_pointed_at(RECIPES[0], fake, tmp_path), out, "--quiet-window", "1.0")
        printed = capsys.readouterr()
        fetches = fake.fetches
    assert code == EXIT_OK, printed.out + printed.err
    assert fetches >= 1
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["verdict"] == "VERIFIED"


def test_a_refused_token_stops_the_run_before_anything_is_sent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZAMMAD_AUTH", "Token token=not-the-token")
    out = tmp_path / "verify.json"
    with zammad_fake.ZammadFake(TOKEN) as fake:
        code = _verify(_pointed_at(RECIPES[0], fake, tmp_path), out)
        printed = capsys.readouterr()
        received = fake.received
    assert code == EXIT_REFUSED
    assert "refused a preflight lookup with 401" in printed.out + printed.err
    assert received == []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_fake_runs_as_a_script(tmp_path):
    port, smtp_port = _free_port(), _free_port()
    process = subprocess.Popen(
        [sys.executable, str(FAKE_PATH), "--port", str(port), "--smtp-port", str(smtp_port),
         "--token", TOKEN],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/api/v1/tickets/search?query=x&expand=true"
        deadline = time.monotonic() + 10
        while True:
            try:
                request = urllib.request.Request(url, headers={"Authorization": f"Token token={TOKEN}"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    assert json.loads(response.read()) == []
                break
            except (ConnectionError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=2)
        assert refused.value.code == 401
        with socket.create_connection(("127.0.0.1", smtp_port), timeout=2) as smtp:
            assert smtp.recv(64).startswith(b"220 ")
    finally:
        process.terminate()
        process.wait(timeout=10)
