"""The demo pipeline from examples/pipeline_demo.py, forwarding to Mailpit.

examples/readback/mailpit.toml reads back what a pipeline sent. This serves
the pipeline from examples/pipeline_demo.py on the webhook that recipe's target
names, and adds the one thing the recipe needs: every ticket the pipeline opens
is forwarded over SMTP to the team address its classifier picked,
`<category>@team.example.com`, with the sender, subject, body, attachments and
Message-ID it arrived with. That forwarded copy is what the recipe finds by tag, and its To
address is the recipe's `route`. Acknowledgements still go to the JSON Lines
sink the demo writes, which `loop` reads as [loop.outbound].

    python sweep/mailpit_pipeline.py --port 8000 --smtp-port 1025 --outbound out.jsonl

POST /intake is the webhook, GET /tickets?tag= the demo's own readback, and
DELETE /tickets clears the pipeline's state and the outbound sink between
runs. Binds to 127.0.0.1 only. Standard library only.
"""
from __future__ import annotations

import argparse
import email
import importlib.util
import smtplib
import sys
import threading
from email.message import EmailMessage
from email.utils import formatdate
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Sequence, Tuple

#: (filename, content type, bytes) of one attachment the webhook carried.
Part = Tuple[str, str, bytes]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_PATH = REPO_ROOT / "examples" / "pipeline_demo.py"

#: Where forwarded tickets go: one mailbox per classifier category.
TEAM_DOMAIN = "team.example.com"
#: The envelope sender of every forward.
FORWARDER = f"forwarder@{TEAM_DOMAIN}"


def load_demo(path: Path = DEMO_PATH) -> ModuleType:
    """examples/pipeline_demo.py as a module, without running its demo."""
    spec = importlib.util.spec_from_file_location("pipeline_demo", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attachment_parts(content_type: str, raw: bytes) -> List[Part]:
    """The file parts of a multipart/form-data webhook body, with their bytes.
    The demo pipeline keeps only the names; a forward needs the files."""
    container = email.message_from_bytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
    )
    if not container.is_multipart():
        return []
    return [
        (part.get_filename(), part.get_content_type(), part.get_payload(decode=True) or b"")
        for part in container.get_payload()
        if part.get_filename()
    ]


def forward_message(record: Dict[str, object], parts: Sequence[Part] = ()) -> EmailMessage:
    """The forwarded copy of one ticket record."""
    message = EmailMessage()
    message["From"] = str(record.get("from") or FORWARDER)
    message["To"] = f"{record.get('category') or 'general'}@{TEAM_DOMAIN}"
    message["Subject"] = str(record.get("subject") or "")
    message["Date"] = formatdate(usegmt=True)
    if record.get("message_id"):
        message["Message-ID"] = f"<{record['message_id']}>"
    if record.get("in_reply_to"):
        message["In-Reply-To"] = f"<{record['in_reply_to']}>"
    references = record.get("references") or []
    if references:
        message["References"] = " ".join(f"<{ref}>" for ref in references)
    message["X-Ticket"] = str(record.get("id") or "")
    message.set_content(str(record.get("body") or ""))
    for filename, content_type, data in parts:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=filename)
    return message


def build_server(port: int, smtp_host: str, smtp_port: int, outbound: Path,
                 demo: ModuleType = None) -> ThreadingHTTPServer:
    demo = demo or load_demo()

    class ForwardingPipeline(demo.Pipeline):
        """The demo pipeline, unchanged, plus a forward of each new record."""

        def __init__(self, outbound_path: Path) -> None:
            super().__init__(outbound_path)
            # Re-entrant, so receive() can hold it across the demo's own
            # receive() and see exactly the records that call appended.
            self.lock = threading.RLock()

        def receive(self, form, attachments, parts: Sequence[Part] = ()):
            with self.lock:
                before = len(self.records)
                result = super().receive(form, attachments)
                new: List[dict] = [dict(r) for r in self.records[before:]]
            for record in new:
                self.forward(record, parts)
            return result

        def forward(self, record: dict, parts: Sequence[Part]) -> None:
            try:
                message = forward_message(record, parts)
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.send_message(message, from_addr=FORWARDER,
                                      to_addrs=[message["To"]])
            except (OSError, smtplib.SMTPException, ValueError) as error:
                # The webhook already answered; a forward that fails is a lost
                # copy, which is what the readback is there to notice.
                print(f"forward of {record.get('id')} failed: {error}",
                      file=sys.stderr, flush=True)

    pipeline = ForwardingPipeline(outbound)

    class Handler(demo.Handler):
        def do_POST(self):  # noqa: N802 - the name the stdlib requires
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            content_type = self.headers.get("Content-Type", "")
            form, files = demo.parse_form(content_type, raw)
            parts = attachment_parts(content_type, raw)
            self._json(pipeline.receive(form, files, parts))

        def do_DELETE(self):  # noqa: N802 - the name the stdlib requires
            pipeline.reset()
            self._json({"reset": True})

    Handler.pipeline = pipeline
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--smtp-host", default="127.0.0.1")
    parser.add_argument("--smtp-port", type=int, default=1025)
    parser.add_argument("--outbound", type=Path, required=True,
                        help="the JSON Lines sink acknowledgements are written to")
    args = parser.parse_args(argv)
    server = build_server(args.port, args.smtp_host, args.smtp_port, args.outbound)
    print(f"pipeline on http://127.0.0.1:{server.server_address[1]}/intake, "
          f"forwarding to {args.smtp_host}:{args.smtp_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
