"""The sweep's Chatwoot recipe, through the real CLI, against sweep/fakes/chatwoot.py.

The fake takes mail the way Chatwoot's Action Mailbox ingress and mailboxes do
(Basic auth, X-Original-To from the envelope, routing by inbox address, the
label rule's activity message) and renders conversations the way Chatwoot's
views do. These run sweep/configs/chatwoot.toml unchanged except for the port,
so what passes here is the recipe the sweep runs.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK

pytestmark = pytest.mark.allow_network

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "sweep" / "configs" / "chatwoot.toml"
FAKE_PATH = REPO / "sweep" / "fakes" / "chatwoot.py"
CONFIG_PORT = "127.0.0.1:3100"


def _load_fake():
    spec = importlib.util.spec_from_file_location("sweep_fake_chatwoot", FAKE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fake_chatwoot = _load_fake()


def _config(tmp_path, fake, *, drop=()):
    """The shipped config pointed at the fake, with the lines in `drop` left
    out."""
    text = CONFIG.read_text(encoding="utf-8").replace(CONFIG_PORT, f"127.0.0.1:{fake.port}")
    assert f"127.0.0.1:{fake.port}" in text
    lines = [line for line in text.splitlines() if line.strip() not in drop]
    path = tmp_path / "chatwoot.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def fake():
    with fake_chatwoot.FakeChatwoot() as server:
        yield server


@pytest.fixture
def secrets(monkeypatch, fake):
    auth = "Basic " + base64.b64encode(
        f"actionmailbox:{fake.password}".encode("utf-8")).decode("ascii")
    monkeypatch.setenv("CHATWOOT_INGRESS_AUTH", auth)
    monkeypatch.setenv("CHATWOOT_API_TOKEN", fake.api_token)
    return auth


def _verify(config, out, count=8):
    return cli_main([
        "verify", "fire", "--send", "--target", "chatwoot", "--config", config,
        "--count", str(count), "--rate", "200", "--expect-route", "1",
        "--quiet-window", "0.3", "--poll-interval", "0.05",
        "--readback-rate", "200", "--out", str(out),
    ])


def test_verify_passes_through_the_recipe(tmp_path, fake, secrets, capsys):
    out = tmp_path / "verify.json"
    code = _verify(_config(tmp_path, fake), out)
    printed = capsys.readouterr()
    assert code == EXIT_OK, printed.out + printed.err

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    statuses = {}
    for record in artifact["records"]:
        assert record["response"]["status"] == 204
        for check in record["verification"]["checks"]:
            statuses.setdefault(check["check"], set()).add(check["status"])
    for name in ("ticket_created", "sender", "subject", "body", "routing"):
        assert statuses[name] == {"passed"}, (name, statuses[name])

    # Every send carried the ingress credential and reached the inbox.
    assert len(fake.inbound) == 8
    assert all(i.authorized and i.routed for i in fake.inbound)
    conversations = fake.conversations
    assert len(conversations) == 8
    for conversation in conversations:
        # The label rule fired, and its activity message is now the latest
        # message: the reason the recipe reads the body elsewhere.
        assert conversation.labels == ["testinghq"]
        assert conversation.messages[-1].message_type == fake_chatwoot.MESSAGE_ACTIVITY
        assert "[testinghq:" in conversation.messages[0].content


def test_a_send_without_the_ingress_header_is_rejected(tmp_path, fake, secrets, capsys):
    config = _config(tmp_path, fake, drop=(
        "[targets.chatwoot.headers]", 'Authorization = "env:CHATWOOT_INGRESS_AUTH"',
    ))
    out = tmp_path / "blast.json"
    code = cli_main([
        "blast", "fire", "--send", "--target", "chatwoot", "--config", config,
        "--count", "4", "--rate", "200", "--out", str(out),
    ])
    assert code in (EXIT_OK, EXIT_FINDING), capsys.readouterr().err
    records = json.loads(out.read_text(encoding="utf-8"))["records"]
    assert [r["response"]["status"] for r in records] == [401] * 4
    assert fake.conversations == []
    assert all(not i.authorized for i in fake.inbound)


def test_a_wrong_ingress_password_is_rejected(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("CHATWOOT_INGRESS_AUTH", "Basic " + base64.b64encode(
        b"actionmailbox:not-the-password").decode("ascii"))
    monkeypatch.setenv("CHATWOOT_API_TOKEN", fake.api_token)
    out = tmp_path / "blast.json"
    code = cli_main([
        "blast", "fire", "--send", "--target", "chatwoot",
        "--config", _config(tmp_path, fake), "--count", "2", "--rate", "200",
        "--out", str(out),
    ])
    assert code in (EXIT_OK, EXIT_FINDING), capsys.readouterr().err
    records = json.loads(out.read_text(encoding="utf-8"))["records"]
    assert {r["response"]["status"] for r in records} == {401}
    assert fake.conversations == []


def test_without_envelope_to_no_message_reaches_the_inbox(tmp_path, fake, secrets, capsys):
    """The generated recipients are nobody's inbox: Chatwoot accepts the POST
    and files the message nowhere, which is what verify then reports."""
    config = _config(tmp_path, fake, drop=('envelope_to = "desk@inbound.chatwoot.test"',))
    out = tmp_path / "verify.json"
    code = cli_main([
        "verify", "fire", "--send", "--target", "chatwoot", "--config", config,
        "--count", "3", "--rate", "200", "--max-wait", "1", "--quiet-window", "0.2",
        "--poll-interval", "0.05", "--readback-rate", "200", "--out", str(out),
    ])
    capsys.readouterr()
    assert code != EXIT_OK
    assert [i.status for i in fake.inbound] == [204] * 3
    assert not any(i.routed for i in fake.inbound)
    assert fake.conversations == []


def test_the_readback_refuses_a_missing_token(fake):
    request = urllib.request.Request(
        f"{fake.url}/api/v1/accounts/1/conversations?status=all")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 401


def test_the_fake_runs_as_a_script():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(FAKE_PATH), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=2) as reply:
                    assert json.loads(reply.read())["data_services"] == "ok"
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise
                time.sleep(0.1)
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_a_corpus_run_reaches_the_inbox_through_envelope_to(tmp_path, fake, secrets, capsys):
    """Corpus addresses are rewritten to corpus.example.test, which is no
    inbox; envelope_to is what files every parseable file under the sweep
    inbox."""
    out = tmp_path / "blast.json"
    code = cli_main([
        "blast", "fire", "--send", "--target", "chatwoot", "--config", _config(tmp_path, fake),
        "--corpus", str(REPO / "tests" / "fixtures" / "corpus"), "--rate", "200",
        "--out", str(out),
    ])
    assert code in (EXIT_OK, EXIT_FINDING), capsys.readouterr().err
    records = json.loads(out.read_text(encoding="utf-8"))["records"]
    sent = [r for r in records if (r.get("response") or {}).get("status") is not None]
    assert sent and all(r["response"]["status"] == 204 for r in sent)
    assert all(i.routed for i in fake.inbound)
    assert len(fake.conversations) == len(sent)
