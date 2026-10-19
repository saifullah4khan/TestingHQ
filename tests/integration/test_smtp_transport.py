"""An SMTP target, over a real socket, into the loopback server in smtp_sink.py.

The unit tests drive the SMTP clients through a fake `smtplib.SMTP`. These run
the real `smtplib` against a real listening socket, through the real CLI, and
then hold the server's receipts against the run artifact: the bytes that
arrived must be the bytes the `mime` format builds, every reply must be
recorded as the status, and the readback must see what the server took in.

Every connection is asserted to have come from loopback, as in
tests/e2e/test_real_sockets.py, so the network exemption stays bounded.
"""
from __future__ import annotations

import json
import socket

import pytest

from smtp_sink import SmtpSink
from testinghq.blast.corpus import CorpusPlan
from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK
from testinghq.core.formats import get_format
from testinghq.core.transport import DEFAULT_BOUNDARY

pytestmark = pytest.mark.allow_network

LOOPBACK = {"127.0.0.1"}


def _config(tmp_path, port, extra=""):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.mail]\ntransport = "smtp"\nhost = "127.0.0.1"\nport = {port}\n{extra}',
        encoding="utf-8",
    )
    return str(path)


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _blast(config, out, *extra):
    return cli_main([
        "blast", "fire", "--send", "--target", "mail", "--config", config,
        "--rate", "1000", "--out", str(out), *extra,
    ])


def test_blast_over_smtp_delivers_exactly_the_mime_bytes(tmp_path, capsys):
    with SmtpSink() as server:
        out = tmp_path / "blast.json"
        code = _blast(_config(tmp_path, server.port), out, "--count", "6")
        assert code in (EXIT_OK, EXIT_FINDING), capsys.readouterr().err
    assert server.peers() <= LOOPBACK

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["config"]["transport"] == "smtp"
    assert artifact["config"]["format"] == "mime"
    accepted = [r for r in artifact["records"] if r["response"]["status"] == 250]
    assert len(accepted) == len(server.messages) > 0

    mime = get_format("mime", DEFAULT_BOUNDARY)
    pairs = CorpusPlan(seed=0, count=6).build()
    wanted = {mime.encode(email).body for email, _label in pairs}
    for message in server.messages:
        assert message.data in wanted, "the server received bytes the mime format did not build"
    assert all(r["response"]["body_snippet"].startswith("2.0.0 Ok: queued")
               for r in accepted)


def test_a_refused_recipient_is_recorded_as_the_reply(tmp_path, capsys):
    with SmtpSink(reject_recipients=["desk@helpdesk.test"]) as server:
        out = tmp_path / "blast.json"
        config = _config(tmp_path, server.port, 'envelope_to = "desk@helpdesk.test"\n')
        assert _blast(config, out, "--count", "1") == EXIT_FINDING
    assert server.messages == []
    record = json.loads(out.read_text(encoding="utf-8"))["records"][0]
    assert record["response"]["status"] == 550
    assert record["response"]["error"] == "5.1.1 no such mailbox"
    assert record["response"]["error_kind"] is None


def test_nothing_listening_is_a_refused_connection(tmp_path, capsys):
    out = tmp_path / "blast.json"
    assert _blast(_config(tmp_path, _free_port()), out, "--count", "2") == EXIT_FINDING
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
    assert summary["transport_errors"]["refused"] == 2


def test_verify_over_smtp_round_trips_through_the_mailbox_readback(tmp_path, capsys):
    """The SMTP verification bar: verify fire over SMTP into the loopback
    server, read back from the mailbox sink the server writes, every check
    that has something to check PASS, routing included."""
    sink = tmp_path / "sink.jsonl"
    with SmtpSink(sink_path=sink) as server:
        config = _config(
            tmp_path, server.port,
            'envelope_to = "desk@helpdesk.test"\n\n[readback]\nkind = "mailbox"\n'
            'path = "sink.jsonl"\n',
        )
        out = tmp_path / "verify.json"
        code = cli_main([
            "verify", "fire", "--send", "--target", "mail", "--config", config,
            "--count", "8", "--rate", "200", "--expect-route", "desk@helpdesk.test",
            "--quiet-window", "0.3", "--poll-interval", "0.05", "--out", str(out),
        ])
        printed = capsys.readouterr()
    assert code == EXIT_OK, printed.out + printed.err
    assert server.peers() <= LOOPBACK
    assert len(server.messages) == 8
    assert all(m.envelope_to == ("desk@helpdesk.test",) for m in server.messages)

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    assert artifact["config"]["transport"] == "smtp"
    statuses = {}
    for record in artifact["records"]:
        assert record["response"]["status"] == 250
        for check in record["verification"]["checks"]:
            statuses.setdefault(check["check"], set()).add(
                (check["status"], check["reason"])
            )
    for name in ("ticket_created", "sender", "subject", "body", "routing"):
        assert statuses[name] == {("passed", None)}, (name, statuses[name])
    # A payload without attachments has nothing to check there; every one
    # that has them must pass.
    assert ("passed", None) in statuses["attachments"]
    assert statuses["attachments"] <= {("passed", None), ("skipped", "nothing to check")}


def test_barrage_over_smtp_keeps_one_connection_per_worker(tmp_path, capsys):
    with SmtpSink() as server:
        out = tmp_path / "barrage.json"
        code = cli_main([
            "barrage", "fire", "--send", "--target", "mail",
            "--config", _config(tmp_path, server.port), "--mode", "closed",
            "--rate", "40", "--duration", "0.5", "--warmup", "0", "--concurrency", "2",
            "--out", str(out),
        ])
        assert code == EXIT_OK, capsys.readouterr().err
        opened = server.connections
    assert server.peers() <= LOOPBACK

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["config"]["transport"] == "smtp"
    connections = artifact["connections"]
    assert connections["keepalive"] is True
    assert connections["connections_opened"] == opened == connections["workers_connected"]
    assert connections["setup_ms"]["p50"] is not None
    sent = artifact["summary"]["requests"]["sent"]
    assert sent == len(server.messages) > opened
    assert artifact["summary"]["status_classes"]["2xx"] == sent
