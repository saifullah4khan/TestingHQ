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

# The outbound sink loop reads to check whether the pipeline auto-replied.
# It points at the file the demo's pipeline appends to, so the tool is checking
# the same thing the pipeline did rather than a file nothing writes to.
[loop.outbound]
kind = "mailbox"
path = "{sink}"

[loop]
reply_address = "no-reply@example.com"
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
    up in the right conversation. Machine-generated mail is recognised and
    nothing further happens: no ticket, no reply. Every field the readback can
    see is kept.

    `auto_reply` and `ticket_machine_mail` turn the machine-mail behaviour into
    the two defects `loop` is for. Both default off, so the handler is a correct
    pipeline until a run says otherwise.
    """

    protocol_version = "HTTP/1.1"
    records: list = []
    by_message_id: dict = {}
    counter = [0]
    auto_reply = [False]
    ticket_machine_mail = [False]
    outbound_path = [None]
    outbound: list = []
    #: Tags whose route this pipeline gets wrong, for `steady`. Keyed on the
    #: tag rather than on a header the payload carries, because a tool that told
    #: the system under test which transform it had applied would be measuring
    #: something other than what a customer sends.
    unstable_tags: set = set()

    def do_POST(self):  # noqa: N802 - the name the stdlib requires
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        content_type = self.headers.get("Content-Type", "")
        fields = _parse_multipart(raw, content_type)
        headers = _headers_blob(fields)

        if self._handle_machine_mail(fields, headers, raw):
            return self._json({"ok": True, "suppressed": True})

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
            # the routing check expects by default, unless this payload is one
            # the classifier handles differently from its own baseline.
            "route": self._route_for(recipients, fields),
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

    def _handle_machine_mail(self, fields, headers, raw) -> bool:
        """Recognise machine mail and, unless a defect is switched on, drop it.

        Returns True when the message was recognised and did not become a ticket.
        The auto-reply and the ticket are two independent defects, decided
        separately, so a pipeline that does both reports both.
        """
        from testinghq.pipeline.loop import machine_mail_marker

        marker = machine_mail_marker(
            headers, _text_field(fields, "subject"), _text_field(fields, "from")
        )
        if marker is None:
            return False
        if self.auto_reply[0]:
            self._emit(fields, marker)
        return not self.ticket_machine_mail[0]

    def _route_for(self, recipients, fields) -> str:
        destination = recipients[0] if recipients else "nowhere"
        if _tag_of(_text_field(fields, "headers")) in self.unstable_tags:
            return f"{destination}-unstable"
        return destination

    def _emit(self, fields, marker) -> None:
        """One outbound auto-reply, in memory and appended to the sink file."""
        line = {
            "id": f"OUT{len(self.outbound) + 1:04d}",
            "tag": _tag_of(_text_field(fields, "headers")),
            "to": _text_field(fields, "to"),
            "from": _text_field(fields, "from"),
            "subject": f"Re: {_text_field(fields, 'subject')}",
            "body": "This is an automatic reply.",
            "attachments": [],
            "message_id": f"outbound-{len(self.outbound) + 1}@example.test",
            "triggered_by": marker,
        }
        self.outbound.append(line)
        if self.outbound_path[0]:
            with open(self.outbound_path[0], "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")

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


def _steady_signed_tags(count=3) -> list:
    """The tags `steady` will give the signed variants.

    Computed with the tool's own builders, because the pipeline can only be
    told which payloads to relabel and the readback is keyed by tag. A demo
    that guessed the numbering would go stale silently.
    """
    from testinghq.pipeline import steady

    families = steady.build_families(0, intents=steady.load_intents()[:count])
    items = steady.build_items(families)
    tags, index = [], 0
    for family in families:
        for variant in family.variants:
            tag = items[index][1]
            index += 1
            if variant.transform == "add-signature":
                tags.append(tag)
    return tags


def reset() -> None:
    """Forget every ticket, as a fresh deployment would be.

    The machine-mail switches go back to correct too, so one run's defect does
    not leak into the next."""
    IntakeHandler.records = []
    IntakeHandler.by_message_id = {}
    IntakeHandler.counter[0] = 0
    IntakeHandler.auto_reply[0] = False
    IntakeHandler.ticket_machine_mail[0] = False
    IntakeHandler.outbound = []
    IntakeHandler.unstable_tags = set()


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
    sink = work / "outbound-sink.jsonl"
    config.write_text(
        CONFIG.format(port=_PORT[0], sink=sink.as_posix()), encoding="utf-8"
    )
    IntakeHandler.outbound_path[0] = str(sink)
    failures = []

    def run(argv, expected, label):
        code = cli_main(argv)
        print(f"\nexit {code}\n")
        if code != expected:
            failures.append(f"{label}: exit {code}, expected {expected}")
        return code

    # Poll settings for the runs that are meant to find losses. A run where a
    # record never appears can never satisfy "every probe found", so the readback
    # polls until it gives up, and waiting out the real 60s default twice over
    # would make this demo take two and a half minutes to teach a lesson that is
    # about the losses rather than the waiting. They report "GAVE UP before
    # settling" rather than "settled", which is the honest description of what
    # happened and is itself worth seeing.
    lossy_poll = ("--max-wait", "3", "--poll-interval", "0.2")
    # A one-second quiet window for the runs that are meant to settle. The
    # default is 5s because a real pipeline deserves the patience; a demo
    # running seven times in CI does not, and the loop it exercises is the same
    # one either way.
    quick_poll = ("--quiet-window", "1", "--poll-interval", "0.2")

    _rule("verify: a correct pipeline, and every one of the six checks running")
    reset()
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--out", str(work / "verify.json"), *quick_poll],
        0,
        "verify against a correct pipeline",
    )

    _rule("verify: the same run, against a system that lost every third record")
    _rule("         every status code in that run was a 200")
    reset()
    _losy_seen[0] = 0
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--readback", "examples.pipeline_demo:lossy_adapter",
         *lossy_poll],
        3,
        "verify against a lossy readback",
    )

    _rule("ledger: 12 uniquely tagged messages, reconciled against the system")
    reset()
    run(
        ["ledger", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--out", str(work / "ledger.json"), *quick_poll],
        0,
        "ledger against a correct pipeline",
    )

    _rule("ledger: the same run, against a system that lost every third record")
    reset()
    _losy_seen[0] = 0
    run(
        ["ledger", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "12", "--readback", "examples.pipeline_demo:lossy_adapter",
         *lossy_poll],
        3,
        "ledger against a lossy readback",
    )

    _rule("redeliver: a provider that retries, duplicates, and reorders")
    reset()
    run(
        ["redeliver", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "2", "--retry-after", "1", "--out", str(work / "redeliver.json"),
         *quick_poll],
        0,
        "redeliver against a correct pipeline",
    )

    _rule("verify check: reading a finished run back, sending nothing")
    reset()
    run(
        ["verify", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "4", "--out", str(work / "run.json"), *quick_poll],
        0,
        "verify fire, to produce an artifact",
    )
    run(
        ["verify", "check", str(work / "run.json"), "--config", str(config),
         "--out", str(work / "check.json"), *quick_poll],
        0,
        "verify check on that artifact",
    )

    _rule("loop: machine-generated mail against a pipeline that ignores it")
    reset()
    run(
        ["loop", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "10", "--out", str(work / "loop.json"), *quick_poll],
        0,
        "loop against a pipeline that ignores machine mail",
    )

    _rule("loop: the same run, against a pipeline that auto-replies to all of it")
    _rule("         and opens a ticket for each one, which is how a loop starts")
    reset()
    IntakeHandler.auto_reply[0] = True
    IntakeHandler.ticket_machine_mail[0] = True
    run(
        ["loop", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "10", "--out", str(work / "loop-bad.json"), *quick_poll],
        3,
        "loop against a pipeline that auto-replies to machine mail",
    )

    _rule("loop: the same pipeline, with --ticket-policy allowed")
    _rule("         the auto-reply check still fails; only the tickets go quiet")
    reset()
    IntakeHandler.auto_reply[0] = True
    run(
        ["loop", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "10", "--ticket-policy", "allowed",
         "--out", str(work / "loop-allowed.json"), *quick_poll],
        3,
        "loop with tickets allowed against a pipeline that auto-replies",
    )

    _rule("steady: a classifier that files a signed message differently")
    _rule("         the message is parsed perfectly. Only comparing it to the")
    _rule("         variants of itself can see that.")
    reset()
    IntakeHandler.unstable_tags = set(_steady_signed_tags(3))
    run(
        ["steady", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "3", "--max-flip-rate", "0",
         "--out", str(work / "steady.json"), *quick_poll],
        3,
        "steady against a classifier that loses track of a signature",
    )

    _rule("steady: the same run, a gate loose enough to accept the flip rate")
    reset()
    IntakeHandler.unstable_tags = set(_steady_signed_tags(3))
    run(
        ["steady", "fire", "--config", str(config), "--target", "local", "--send",
         "--count", "3", "--max-flip-rate", "0.9",
         "--out", str(work / "steady-lax.json"), *quick_poll],
        0,
        "steady with a loose gate against the same classifier",
    )

    _rule("result")
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        return 1
    print("every run behaved as its readback said it should, including the two")
    print("that found losses no status code mentioned")
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
