"""`blast fire --send --corpus` over real loopback sockets, HTTP and SMTP.

The HTTP side is a stdlib server on 127.0.0.1 that hands every body it
receives to `fake_sink.FakeSink`, which decodes the SendGrid multipart back
into fields as a real endpoint would. The SMTP side is `smtp_sink.SmtpSink`.
Both runs go through the real CLI and must produce one record per corpus file
with its source name, and nothing that arrived may carry an address from the
corpus.
"""
from __future__ import annotations

import json
import threading
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fake_sink import FakeSink
from smtp_sink import SmtpSink
from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_OK
from testinghq.core.guardrails import is_synthetic_address

pytestmark = pytest.mark.allow_network

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
LOOPBACK = {"127.0.0.1"}

SOURCES = [
    "005-raw-utf8-headers.box",
    "007-forwarded.box",
    "inbox/001-plain-ascii.eml",
    "inbox/002-alternative-cc-reply-to.eml",
    "inbox/003-attachments.eml",
    "inbox/004-encoded-headers.box",
    "inbox/006-reply-thread.eml",
    "nested/008-html-only.eml",
    "nested/009-degenerate.box",
]
PARSED = [source for source in SOURCES if source != "nested/009-degenerate.box"]

REAL_DOMAINS = (
    "lindenbrook-freight.com", "orchardlane-supplies.com", "kestrelpoint.co.uk",
    "bluewhistle-studio.io", "mapleridge-insurance.com", "vandermeer-hydraulik.de",
    "petitpont-atelier.fr", "harbourview-clinic.ca", "tidewater-logistics.org",
    "sunvale-telecom.net",
)


def _no_real_domain(text: str) -> bool:
    lowered = text.lower()
    return not any("@" + domain in lowered for domain in REAL_DOMAINS)


class DecodingHttpSink:
    """A loopback HTTP server whose handler feeds each POST body to FakeSink."""

    def __init__(self):
        self.sink = FakeSink()
        self.peers = []
        self._lock = threading.Lock()

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with outer._lock:
                    outer.sink.post(body, self.headers.get("Content-Type", ""))
                    outer.peers.append(self.client_address[0])
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/inbound"


def _fire(config_text, tmp_path):
    config = tmp_path / "target.toml"
    config.write_text(config_text, encoding="utf-8")
    out = tmp_path / "blast.json"
    code = cli_main([
        "blast", "fire", "--send", "--config", str(config), "--target", "desk",
        "--corpus", str(CORPUS), "--count", "100", "--rate", "1000", "--out", str(out),
    ])
    return code, json.loads(out.read_text(encoding="utf-8"))


def _assert_one_record_per_file(artifact):
    records = artifact["records"]
    assert [r["source"] for r in records] == SOURCES
    assert artifact["config"]["corpus_dir"] == str(CORPUS)
    assert artifact["summary"]["unparseable"][0]["source"] == "nested/009-degenerate.box"
    sent = [r for r in records if r.get("outcome") != "unparseable"]
    assert [r["source"] for r in sent] == PARSED
    return sent


def test_blast_corpus_over_http_reaches_the_sink_one_record_per_file(tmp_path, capsys):
    with DecodingHttpSink() as server:
        code, artifact = _fire(f'[targets.desk]\nurl = "{server.url}"\n', tmp_path)
    assert code == EXIT_OK, capsys.readouterr().err
    assert set(server.peers) <= LOOPBACK

    sent = _assert_one_record_per_file(artifact)
    assert all(r["response"]["status"] == 200 for r in sent)
    received = server.sink.received
    assert len(received) == len(PARSED)

    for response in received:
        decoded = response.decoded
        assert _no_real_domain(response.body.decode("latin-1"))
        for attachment in decoded.attachments:
            assert _no_real_domain(attachment.content.decode("utf-8", "replace"))
        for address in decoded.envelope["to"]:
            assert is_synthetic_address(address)
        assert "X-TestingHQ-Tag: " in decoded.headers_text
        assert "[testinghq:" in decoded.subject and "[testinghq:" in decoded.text

    subjects = [r.decoded.subject for r in received]
    assert subjects[0].startswith("Commande 3390 : colis abîmé à la livraison")
    by_subject = {r.decoded.subject.split(" [")[0]: r.decoded for r in received}
    claim = by_subject["Claim 4471: signed form and contact list"]
    assert [a.filename for a in claim.attachments] == ["claim-4471.pdf", "contacts.csv"]


def test_blast_corpus_over_smtp_reaches_the_server_one_record_per_file(tmp_path, capsys):
    with SmtpSink() as server:
        code, artifact = _fire(
            f'[targets.desk]\ntransport = "smtp"\nhost = "127.0.0.1"\nport = {server.port}\n',
            tmp_path,
        )
    assert code == EXIT_OK, capsys.readouterr().err
    assert server.peers() <= LOOPBACK
    assert artifact["config"]["transport"] == "smtp"

    sent = _assert_one_record_per_file(artifact)
    assert all(r["response"]["status"] == 250 for r in sent)
    assert len(server.messages) == len(PARSED)

    for message in server.messages:
        assert is_synthetic_address(message.envelope_from) or message.envelope_from == ""
        assert all(is_synthetic_address(a) for a in message.envelope_to)
        parsed = message_from_bytes(message.data, policy=policy.default)
        texts = [message.data.decode("latin-1")]
        for part in parsed.walk():
            texts.extend(str(v) for _k, v in part.items())
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                texts.append(payload.decode("utf-8", "replace"))
        assert _no_real_domain("\n".join(texts))
        assert parsed["X-TestingHQ-Tag"]
