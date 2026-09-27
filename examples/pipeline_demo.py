"""An end-to-end demonstration of verify, ledger and redeliver.

Everything else in this repository's suite is hermetic. This script is not, and
deliberately so: it starts a real HTTP server on 127.0.0.1 that parses multipart
inbound payloads into "tickets" the way a real intake pipeline would, points the
CLI at it over a real socket, and prints what the three tools say.

It exists because the injectable-client seam, which the whole suite relies on,
also means no test in the suite proves that the real transport, the real
serializer, the real readback HTTP client and a real server can talk to each
other. A seam that has only ever met a stub is a seam nobody has checked.

Two things to notice when you read the output:

  The pipeline here is correct. It deduplicates on Message-ID, it threads a
  reply onto its parent's ticket, it keeps the attachments. That is the
  baseline; a tool that reported a problem here would be wrong, and the
  script fails if it does.

  Then the same verification runs against a readback that reports every third
  record as absent, while the endpoint keeps answering 200. That is the whole
  argument for this suite of tools, made by running it.

Run it:

    .venv\\Scripts\\python.exe examples\\pipeline_demo.py

It binds to loopback only. Every address in every payload is synthetic and
reserved-example, and nothing is sent anywhere else.
"""
from __future__ import annotations

import email
import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_BOUNDARY = re.compile(r"boundary=([^\s;]+)")
TAG_MARKER = re.compile(r"\[testinghq:([^\]]+)\]")
TAG_HEADER = re.compile(r"^X-TestingHQ-Tag:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

CONFIG = """[targets.local]
name = "local"
url = "http://127.0.0.1:{port}/intake"

[readback]
kind = "http"
url = "http://127.0.0.1:{port}/tickets"
tag_param = "tag"
"""


def _tag_of(raw: str) -> str:
    match = TAG_HEADER.search(raw)
    if match:
        return match.group(1).strip()
    found = TAG_MARKER.search(raw)
    return found.group(1) if found else ""


def _bare(value: str) -> str:
    return (value or "").strip().strip("<>").strip()


def _parse_multipart(raw: str, content_type: str) -> dict:
    """Split the SendGrid-shaped multipart into `{part name: Message}`.

    This is the part a real pipeline has to get right, and getting it wrong is
    exactly what the tools exist to catch: the payload's real headers arrive as
    the body of a form field named `headers`, not as MIME headers on the
    request. A handler that read `From` off the outer message would see nothing,
    which is the shape of the bug the sender check reports as an empty address.
    """
    boundary = _BOUNDARY.search(content_type)
    if boundary is None:
        return {}
    container = email.message_from_string(
        f"Content-Type: {content_type}\r\n\r\n{raw}"
    )
    fields = {}
    for part in container.get_payload():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = part
    return fields


def _decoded(part) -> str:
    if part is None:
        return ""
    return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _headers_blob(fields: dict) -> dict:
    """The payload's own headers, re-parsed as a message so `Message-ID` and
    the threading headers can be read normally."""
    blob = _decoded(fields.get("headers"))
    return email.message_from_string(blob) if blob else {}


def _text_field(fields: dict, name: str) -> str:
    return _decoded(fields.get(name))


def _json_field(fields: dict, name: str):
    raw = _decoded(fields.get(name))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _attachment_names(fields: dict):
    names = []
    index = 1
    while f"attachment{index}" in fields:
        part = fields[f"attachment{index}"]
        names.append(part.get_filename() or f"attachment{index}")
        index += 1
    return names


class IntakeHandler(BaseHTTPRequestHandler):
    """A small, correct intake pipeline.

    One ticket per Message-ID, so a provider's retry produces no second ticket.
    A reply is filed on its parent's ticket, so an out-of-order reply still ends
    up in the right conversation. Every field the readback can see is kept.
    """

    protocol_version = "HTTP/1.1"
    records: list = []
    by_message_id: dict = {}
    counter = [0]

    def do_POST(self):  # noqa: N802 - the name the stdlib requires
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        content_type = self.headers.get("Content-Type", "")
        fields = _parse_multipart(raw, content_type)
        headers = _headers_blob(fields)

        message_id = _bare(headers.get("Message-ID", ""))
        if message_id and message_id in self.by_message_id:
            self.by_message_id[message_id]["deliveries"] = (
                self.by_message_id[message_id].get("deliveries", 1) + 1
            )
            return self._json({"ok": True, "deduplicated": True})

        parent_id = _bare(headers.get("In-Reply-To", ""))
        parent = self.by_message_id.get(parent_id)

        IntakeHandler.counter[0] += 1
        ticket_id = parent["id"] if parent else f"T{IntakeHandler.counter[0]:05d}"

        envelope = _json_field(fields, "envelope") or {}
        recipients = envelope.get("to") or []

        record = {
            "id": ticket_id,
            "tag": _tag_of(raw),
            "from": _text_field(fields, "from"),
            "subject": _text_field(fields, "subject"),
            "body": _text_field(fields, "text"),
            "attachments": _attachment_names(fields),
            # Filed in the mailbox the message was addressed to, which is what
            # the routing check expects by default.
            "route": recipients[0] if recipients else "nowhere",
            "message_id": message_id,
            "in_reply_to": parent_id or None,
            "references": (headers.get("References") or "").split(),
            "deliveries": 1,
        }
        self.records.append(record)
        if message_id:
            self.by_message_id[message_id] = record
        _reconcile_orphans(self.records)
        return self._json({"ok": True, "id": record["id"]})

    def do_GET(self):  # noqa: N802
        """The readback endpoint. `?tag=` looks one payload up; no tag
        enumerates everything, which is what the ledger's stray check needs."""
        if "?" not in self.path:
            return self._json(self.records)
        query = self.path.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        tag = params.get("tag", "")
        if not tag:
            return self._json(self.records)
        return self._json([r for r in self.records if r["tag"] == tag])

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return  # keep the demo's output readable


def reset() -> None:
    """Forget every ticket, as a fresh deployment would be."""
    IntakeHandler.records = []
    IntakeHandler.by_message_id = {}
    IntakeHandler.counter[0] = 0


def _reconcile_orphans(records: list) -> None:
    """Move any message that was waiting for a parent onto that parent's ticket.

    This is the whole reply-first case, and it is the part a correct intake
    pipeline has to get right: a reply often lands before the message it
    answers, so it is filed provisionally and moved when the parent finally
    shows up. Opening a new conversation instead is the common bug, and it is
    what the `thread_together` check catches. Re-runs to a fixed point because a
    third message can be waiting on the second, which was waiting on the first.
    """
    by_message = {r["message_id"]: r for r in records if r["message_id"]}
    moved = True
    while moved:
        moved = False
        for record in records:
            parent = by_message.get(record.get("in_reply_to") or "")
            if parent is None or parent["id"] == record["id"]:
                continue
            record["id"] = parent["id"]
            moved = True


#: A readback that reports every third record as absent, while the endpoint
#: keeps answering 200. The loss is invented on the read side, so every status
#: code in the run is a success and the only thing that can notice is a tool
#: that asks what the system produced.
#:
#: Built from the config it is handed rather than from a port stashed in this
#: module. The first version read a module global, which is zero when the CLI
#: imports this file as `examples.pipeline_demo` while the server was started
#: from the `__main__` copy, and the run failed with "the requested address is
#: not valid in its context" rather than with anything about pipelines.
LOSSY_EVERY = 3
_losy_seen = [0]
#: The live server's port, so the config written by `main` can point at it.
#: The lossy adapter deliberately does not read this: it is handed the config
#: instead, because the CLI imports this file as `examples.pipeline_demo` while
#: the server is started from the `__main__` copy, and the two module objects
#: have separate globals.
_PORT = [0]


def lossy_adapter(config):
    from testinghq.pipeline.adapters import HttpJsonAdapter

    inner = HttpJsonAdapter(
        config["url"],
        tag_param=config["tag_param"],
        items_key=config["items_key"],
        field_map=config["field_map"],
        list_path=config.get("list_path"),
    )
    inner_fetch = inner.fetch

    class _Lossy:
        def fetch(self, probe):
            _losy_seen[0] += 1
            if _losy_seen[0] % LOSSY_EVERY == 0:
                return []
            return inner_fetch(probe)

        def list_all(self):
            return inner.list_all()

        def close(self) -> None:
            return None

    return _Lossy()


def _rule(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:
    from testinghq.cli import main as cli_main

    work = Path(tempfile.mkdtemp(prefix="testinghq-demo-"))
    config = work / "target.toml"
    config.write_text(CONFIG.format(port=_PORT[0]), encoding="utf-8")
    failures = []

    def run(argv, expected, label):
        code = cli_main(argv)
        print(f"\nexit {code}\n")
        if code != expected:
            failures.append(f"{label}: exit {code}, expected {expected}")
        return code

    _rule("verify: a correct pipeline, and every one of the six checks running")
    reset()
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--out", str(work / "verify.json")],
        0,
        "verify against a correct pipeline",
    )

    _rule("verify: the same run, against a system that lost every third record")
    _rule("         every status code in that run was a 200")
    reset()
    _losy_seen[0] = 0
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--readback", "examples.pipeline_demo:lossy_adapter"],
        3,
        "verify against a lossy readback",
    )

    _rule("ledger: 12 uniquely tagged messages, reconciled against the system")
    reset()
    run(
        ["ledger", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--out", str(work / "ledger.json")],
        0,
        "ledger against a correct pipeline",
    )

    _rule("ledger: the same run, against a system that lost every third record")
    reset()
    _losy_seen[0] = 0
    run(
        ["ledger", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--readback", "examples.pipeline_demo:lossy_adapter"],
        3,
        "ledger against a lossy readback",
    )

    _rule("redeliver: a provider that retries, duplicates, and reorders")
    reset()
    run(
        ["redeliver", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "2", "--retry-after", "1", "--out", str(work / "redeliver.json")],
        0,
        "redeliver against a correct pipeline",
    )

    _rule("verify check: reading a finished run back, sending nothing")
    reset()
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "4", "--out", str(work / "run.json")],
        0,
        "verify fire, to produce an artifact",
    )
    run(
        ["verify", "check", str(work / "run.json"), "--config", str(config),
         "--out", str(work / "check.json")],
        0,
        "verify check on that artifact",
    )

    _rule("result")
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        return 1
    print("all four runs behaved as their readback said they should, including")
    print("the two that found losses the status codes never mentioned")
    return 0


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), IntakeHandler)
    _PORT[0] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        code = main()
    finally:
        server.shutdown()
    sys.exit(code)
