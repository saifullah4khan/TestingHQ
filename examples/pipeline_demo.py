"""The pipeline tools, run over real sockets against a small inbound-email pipeline.

The pipeline in the first half of this file is the system under test. It
imports nothing from testinghq. It parses the SendGrid-shaped webhook, decides
what is machine mail by its own reading of RFC 3834, finds the run's tag with
its own regex, deduplicates on Message-ID, threads replies onto their parent's
ticket (even when the reply arrives first), acknowledges new tickets by mail,
and labels each message with a toy keyword classifier that reads the content.

The second half runs verify, ledger, redeliver, loop and steady against it,
once against the correct pipeline and once with a defect switched on, and
checks each run's exit code. The defects are on the write side: the pipeline
answers 200 to every POST and then loses, duplicates or mishandles the message.

    python examples/pipeline_demo.py

It binds to 127.0.0.1 only, and every address in every payload is on a
reserved example domain.
"""
from __future__ import annotations

import email
import email.utils
import json
import re
import sys
import tempfile
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------------------
# The pipeline under test
# ---------------------------------------------------------------------------

#: The marker a message carries its test tag in. A helpdesk search over the
#: subject and body would find it the same way.
TAG_PATTERN = re.compile(r"\[testinghq:([A-Za-z0-9._-]+)\]")

#: RFC 3834 section 5: any Auto-Submitted value other than "no" is automatic.
#: Section 2 lists the other signals a responder should respect.
BULK_PRECEDENCE = ("bulk", "list", "junk")
LIST_HEADERS = ("list-id", "list-unsubscribe", "list-post", "list-help")
ROBOT_LOCALS = ("mailer-daemon", "postmaster", "noreply", "no-reply",
                "donotreply", "do-not-reply")
AUTO_REPLY_SUBJECTS = ("automatic reply", "auto-reply", "autoreply", "out of office")

#: The toy classifier's vocabulary: words (or word starts) per category, in the
#: three languages the steady fixture is written in.
CATEGORY_WORDS = {
    "billing": ("charge", "refund", "invoice", "inv-", "paise", "رقم", "چارج", "انوائس"),
    "account": ("account", "password", "log in", "login", "subscription", "cancel",
                "gdpr", "mansookh", "اکاؤنٹ", "پاس ورڈ", "سبسکرپشن", "منسوخ"),
    "shipping": ("order", "deliver", "arrived", "parcel", "size",
                 "آرڈر", "پارسل", "سائز"),
    "product": ("kettle", "crack", "damage", "daraar", "app", "feature",
                "کیتل", "دراڑ", "ایپ"),
    "sales": ("pricing", "price", "cost", "qeemat", "قیمت", "لاگت"),
}
_CATEGORY_PATTERNS = {
    category: [re.compile(r"(?<!\w)" + re.escape(word), re.IGNORECASE) for word in words]
    for category, words in CATEGORY_WORDS.items()
}

REPLY_ADDRESS = "support@example.com"


def classify(text: str) -> str:
    """The category whose words appear most often in `text`, or "general"."""
    scores = {
        category: sum(len(p.findall(text)) for p in patterns)
        for category, patterns in _CATEGORY_PATTERNS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "general"
    return next(c for c in CATEGORY_WORDS if scores[c] == best)


def machine_mail_reason(headers: Message, sender: str, subject: str) -> Optional[str]:
    """Why this message is automatic, or None for a person's mail."""
    auto = (headers.get("Auto-Submitted") or "").split(";")[0].strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    precedence = (headers.get("Precedence") or "").strip().lower()
    if precedence in BULK_PRECEDENCE:
        return f"Precedence: {precedence}"
    present = {name.lower() for name in headers.keys()}
    for name in LIST_HEADERS:
        if name in present:
            return f"{name} header"
    if (headers.get("Return-Path") or "").strip() == "<>":
        return "null Return-Path"
    if (headers.get("Content-Type") or "").lower().startswith("multipart/report"):
        return "delivery report"
    local = email.utils.parseaddr(sender)[1].lower().split("@", 1)[0]
    if local in ROBOT_LOCALS or local.startswith("owner-") or local.endswith("-request"):
        return f"sender {local}@"
    if subject.strip().lower().startswith(AUTO_REPLY_SUBJECTS):
        return "auto-reply subject"
    return None


def _ids(value: Optional[str]) -> List[str]:
    """Message-IDs in a header, read leniently: `<id>` as RFC 5322 writes
    them, or bare `id@host` tokens as some senders do."""
    bracketed = re.findall(r"<([^<>\s]+)>", value or "")
    return bracketed or [t for t in (value or "").split() if "@" in t]


class Pipeline:
    """Intake state, and the defects a run can switch on.

    lose_every           answer 200, then never write every Nth message
    duplicate_every      write every Nth message twice, as a worker that lost
                         its queue ack and processed the redelivery would
    answer_machine_mail  send the acknowledgement to machine mail too
    ticket_machine_mail  open a ticket for machine mail
    truncated_classifier label from the first 60 characters of the body, as a
                         prompt cut short to save tokens would
    """

    def __init__(self, outbound_path: Path) -> None:
        self.outbound_path = outbound_path
        self.lock = threading.Lock()
        self.reset()

    def reset(self, **defects) -> None:
        self.records: List[dict] = []
        self.ticket_of: Dict[str, str] = {}
        self.waiting: Dict[str, List[dict]] = {}
        self.accepted = self.tickets = self.sent = 0
        self.lose_every = defects.pop("lose_every", 0)
        self.duplicate_every = defects.pop("duplicate_every", 0)
        self.answer_machine_mail = defects.pop("answer_machine_mail", False)
        self.ticket_machine_mail = defects.pop("ticket_machine_mail", False)
        self.truncated_classifier = defects.pop("truncated_classifier", False)
        if defects:
            raise TypeError(f"unknown defects: {sorted(defects)}")
        self.outbound_path.write_text("", encoding="utf-8")

    def receive(self, form: Dict[str, str], attachments: List[str]) -> dict:
        headers = email.message_from_string(form.get("headers", ""))
        sender, subject, body = form.get("from", ""), form.get("subject", ""), form.get("text", "")
        message_id = (_ids(headers.get("Message-ID")) or [""])[0]
        with self.lock:
            reason = machine_mail_reason(headers, sender, subject)
            if reason and self.answer_machine_mail:
                self._acknowledge(sender, subject, message_id, None)
            if reason and not self.ticket_machine_mail:
                return {"ignored": reason}
            if message_id and message_id in self.ticket_of:
                return {"duplicate_of": self.ticket_of[message_id]}
            self.accepted += 1
            if self.lose_every and self.accepted % self.lose_every == 0:
                return {"accepted": True}
            parents = _ids(headers.get("In-Reply-To")) + _ids(headers.get("References"))[::-1]
            parent_ticket = next((self.ticket_of[p] for p in parents if p in self.ticket_of), None)
            found = TAG_PATTERN.search(subject) or TAG_PATTERN.search(body)
            label_text = body[:60] if self.truncated_classifier else f"{subject}\n{body}"
            record = {
                "id": parent_ticket or self._new_ticket(),
                "tag": found.group(1) if found else None,
                "from": sender,
                "subject": subject,
                "body": body,
                "attachments": attachments,
                "route": (json.loads(form.get("envelope") or "{}").get("to") or [""])[0],
                "message_id": message_id,
                "in_reply_to": (_ids(headers.get("In-Reply-To")) or [None])[0],
                "references": _ids(headers.get("References")),
                "category": classify(label_text),
            }
            self.records.append(record)
            if self.duplicate_every and self.accepted % self.duplicate_every == 0:
                self.records.append(dict(record, id=self._new_ticket()))
            if parent_ticket is None and parents:
                self.waiting.setdefault(parents[0], []).append(record)
            if message_id:
                self.ticket_of[message_id] = record["id"]
                self._adopt_orphans(message_id, record["id"])
            if parent_ticket is None and not reason:
                self._acknowledge(sender, subject, message_id, record["id"])
            return {"ticket": record["id"]}

    def _new_ticket(self) -> str:
        self.tickets += 1
        return f"T{self.tickets:05d}"

    def _adopt_orphans(self, message_id: str, ticket: str) -> None:
        """Merge the tickets of replies that arrived before this message into
        its ticket, together with anything already threaded onto them."""
        for orphan in self.waiting.pop(message_id, []):
            old = orphan["id"]
            if old == ticket:
                continue
            for record in self.records:
                if record["id"] == old:
                    record["id"] = ticket
            for known, value in self.ticket_of.items():
                if value == old:
                    self.ticket_of[known] = ticket

    def _acknowledge(self, to: str, subject: str, message_id: str,
                     ticket: Optional[str]) -> None:
        """One outbound acknowledgement, appended to the outbound sink."""
        self.sent += 1
        own_id = f"ack-{self.sent}@helpdesk.example.com"
        if ticket:
            self.ticket_of[own_id] = ticket
        line = {
            "id": f"OUT{self.sent:04d}",
            "from": REPLY_ADDRESS,
            "to": to,
            "subject": f"Re: {subject}",
            "body": "We received your message and will reply soon.",
            "attachments": [],
            "message_id": own_id,
            "in_reply_to": message_id or None,
            "references": [message_id] if message_id else [],
        }
        with self.outbound_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")

    def find(self, tag: Optional[str]) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.records if tag is None or r["tag"] == tag]


def parse_form(content_type: str, raw: bytes):
    """A multipart/form-data body as ({field: text}, [attachment filenames])."""
    container = email.message_from_bytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
    )
    parts = {}
    for part in container.get_payload() if container.is_multipart() else []:
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[name] = part
    try:
        charsets = json.loads(parts["charsets"].get_payload(decode=True) or b"{}")
    except (KeyError, ValueError):
        charsets = {}
    form, files = {}, []
    for name, part in parts.items():
        if part.get_filename():
            files.append(part.get_filename())
            continue
        data = part.get_payload(decode=True) or b""
        try:
            form[name] = data.decode(charsets.get(name, "utf-8"))
        except (LookupError, UnicodeDecodeError):
            form[name] = data.decode("utf-8", "replace")
    return form, files


class Handler(BaseHTTPRequestHandler):
    """POST /intake is the webhook. GET /tickets?tag= is the readback, and
    GET /tickets with no tag lists every record."""

    protocol_version = "HTTP/1.1"
    pipeline: Pipeline

    def do_POST(self):  # noqa: N802 - the name the stdlib requires
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        form, files = parse_form(self.headers.get("Content-Type", ""), raw)
        self._json(self.pipeline.receive(form, files))

    def do_GET(self):  # noqa: N802
        query = parse_qs(urlsplit(self.path).query)
        self._json(self.pipeline.find((query.get("tag") or [None])[0]))

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


# ---------------------------------------------------------------------------
# The demonstration
# ---------------------------------------------------------------------------

CONFIG = """[targets.local]
url = "http://127.0.0.1:{port}/intake"

[readback]
kind = "http"
url = "http://127.0.0.1:{port}/tickets"
tag_param = "tag"

[loop.outbound]
kind = "mailbox"
path = "{sink}"

[loop]
reply_address = "{reply}"
"""

#: Loopback answers in milliseconds, so the readback need not wait as long as
#: it would for a real deployment, nor pace itself for a vendor API.
FAST = ("--quiet-window", "1", "--poll-interval", "0.2", "--readback-rate", "200")
#: For runs where records go missing: the readback waits for them until it
#: gives up, so give up quickly.
SHORT = ("--max-wait", "3")
STEADY = ("steady", "fire", "--count", "3", "--label-field", "category")
MACHINE_DEFECTS = {"answer_machine_mail": True, "ticket_machine_mail": True}

#: (title, argv, pipeline defects, expected exit code)
RUNS = (
    ("verify: the correct pipeline", ["verify", "fire", "--count", "12"], {}, 0),
    ("ledger: the correct pipeline", ["ledger", "fire", "--count", "12"], {}, 0),
    ("ledger: a worker that writes every fifth message twice",
     ["ledger", "fire", "--count", "12"], {"duplicate_every": 5}, 3),
    ("ledger: a pipeline that drops every third message after answering 200",
     ["ledger", "fire", "--count", "12", *SHORT], {"lose_every": 3}, 3),
    ("verify: the same lossy pipeline",
     ["verify", "fire", "--count", "12", *SHORT], {"lose_every": 3}, 3),
    ("redeliver: duplicates, a slow retry, a reply first, a reference chain",
     ["redeliver", "fire", "--count", "2", "--retry-after", "1"], {}, 0),
    ("loop: machine mail against the correct pipeline", ["loop", "fire"], {}, 0),
    ("loop: a pipeline that tickets and acknowledges machine mail",
     ["loop", "fire"], MACHINE_DEFECTS, 3),
    ("loop: the same pipeline, with tickets for machine mail allowed",
     ["loop", "fire", "--ticket-policy", "allowed"], MACHINE_DEFECTS, 3),
    ("steady: a classifier that reads the whole message", [*STEADY], {}, 0),
    ("steady: a classifier that reads the first 60 characters",
     [*STEADY], {"truncated_classifier": True}, 3),
)


def main() -> int:
    from testinghq.cli import main as cli

    work = Path(tempfile.mkdtemp(prefix="testinghq-demo-"))
    sink = work / "outbound.jsonl"
    pipeline = Pipeline(sink)
    Handler.pipeline = pipeline
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = work / "target.toml"
    config.write_text(
        CONFIG.format(port=server.server_address[1], sink=sink.as_posix(),
                      reply=REPLY_ADDRESS),
        encoding="utf-8",
    )
    failures = []

    def run(label: str, argv: List[str], expected: int) -> None:
        code = cli(argv)
        print(f"\nexit {code}, expected {expected}")
        if code != expected:
            failures.append(f"{label}: exit {code}, expected {expected}")

    try:
        for number, (title, argv, defects, expected) in enumerate(RUNS, 1):
            print(f"\n{'=' * 74}\n{number}. {title}\n{'=' * 74}")
            pipeline.reset(**defects)
            out = work / f"run{number}.json"
            run(title, [*argv, "--config", str(config), "--target", "local", "--send",
                        "--rate", "50", "--out", str(out), *FAST], expected)
            if number == 1:
                print("\nverify check: the same artifact read back again, sending nothing")
                run("verify check", ["verify", "check", str(out), "--config", str(config),
                                     *FAST], 0)
    finally:
        server.shutdown()
    print(f"\n{'=' * 74}")
    for failure in failures:
        print(f"FAILED {failure}")
    if failures:
        return 1
    print(f"every run exited as expected; artifacts in {work}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
