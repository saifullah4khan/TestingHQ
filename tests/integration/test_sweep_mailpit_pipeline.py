"""sweep/mailpit_pipeline.py, the system under test of `sweep/run.sh mailpit`.

The demo pipeline from examples/pipeline_demo.py, plus a forward of every
ticket over SMTP. Here the forward goes to the loopback server in
smtp_sink.py instead of Mailpit, and `verify` reads it back through the
mailbox kind: what the pipeline forwarded has to be what was sent, filed
under the team address its classifier picked.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from smtp_sink import SmtpSink
from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_OK

pytestmark = pytest.mark.allow_network

REPO_ROOT = Path(__file__).resolve().parents[2]
FAST = ["--rate", "200", "--quiet-window", "0.5", "--poll-interval", "0.1",
        "--readback-rate", "200"]


def _module():
    spec = importlib.util.spec_from_file_location(
        "mailpit_pipeline", REPO_ROOT / "sweep" / "mailpit_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pipeline(tmp_path):
    """(pipeline port, forwarded-mail sink path, smtp sink)."""
    forwarded = tmp_path / "forwarded.jsonl"
    with SmtpSink(sink_path=forwarded) as smtp:
        server = _module().build_server(0, "127.0.0.1", smtp.port, tmp_path / "outbound.jsonl")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], forwarded, smtp
        finally:
            server.shutdown()
            server.server_close()


def _config(tmp_path, port):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.pipeline]\nurl = "http://127.0.0.1:{port}/intake"\n\n'
        '[readback]\nkind = "mailbox"\npath = "forwarded.jsonl"\n',
        encoding="utf-8",
    )
    return str(path)


def test_each_ticket_is_forwarded_as_sent_to_its_teams_mailbox(tmp_path, pipeline, capsys):
    port, forwarded, smtp = pipeline
    out = tmp_path / "verify.json"
    code = cli_main(["verify", "fire", "--send", "--target", "pipeline",
                     "--config", _config(tmp_path, port), "--count", "6",
                     "--out", str(out), *FAST])
    report = capsys.readouterr().out
    assert code == EXIT_OK, report
    assert "VERIFIED" in report
    assert smtp.peers() <= {"127.0.0.1"}

    lines = [json.loads(line) for line in forwarded.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 6
    for line in lines:
        assert line["route"].endswith("@team.example.com")
        assert line["from"] != "forwarder@team.example.com"
        assert line["headers"]["X-Ticket"].startswith("T")


def test_delete_resets_the_pipeline_between_runs(tmp_path, pipeline):
    port, _forwarded, _smtp = pipeline
    code = cli_main(["ledger", "fire", "--send", "--target", "pipeline",
                     "--config", _config(tmp_path, port), "--count", "3",
                     "--out", str(tmp_path / "ledger.json"), *FAST])
    assert code == EXIT_OK
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    tickets = f"http://127.0.0.1:{port}/tickets"
    assert len(json.load(opener.open(tickets, timeout=5))) == 3
    request = urllib.request.Request(tickets, method="DELETE")
    assert json.load(opener.open(request, timeout=5)) == {"reset": True}
    assert json.load(opener.open(tickets, timeout=5)) == []
    assert (tmp_path / "outbound.jsonl").read_text(encoding="utf-8") == ""
