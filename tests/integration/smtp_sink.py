"""A loopback SMTP server for the socket tests, on the standard library alone.

`smtpd` left the standard library in Python 3.12, so this is a small
`socketserver` one: enough of RFC 5321 to take a message the way a real mail
server does (greeting, EHLO, MAIL, RCPT, DATA with dot-unstuffing, RSET,
QUIT), and nothing else. No AUTH and no STARTTLS.

Every accepted message is kept in `messages`, with its envelope and the peer
it came from, and, when `sink_path` is given, appended as one JSON line in the
shape the `mailbox` readback kind reads, so a pipeline tool can read back what
this server received.

    with SmtpSink(sink_path=tmp_path / "sink.jsonl") as server:
        ...  # [targets.mail] transport = "smtp", host = "127.0.0.1", port = server.port
"""
from __future__ import annotations

import json
import socketserver
import threading
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Received:
    envelope_from: str
    envelope_to: Tuple[str, ...]
    data: bytes
    peer: str


def _address(argument: str) -> str:
    """`FROM:<a@b.test> SIZE=10` -> `a@b.test`."""
    _, _, rest = argument.partition(":")
    return rest.strip().split(" ", 1)[0].strip("<>")


def _sink_line(index: int, message: Received) -> dict:
    """One `mailbox` sink line for a received message."""
    parsed = message_from_bytes(message.data, policy=default_policy)
    plain = parsed.get_body(preferencelist=("plain",))
    html = parsed.get_body(preferencelist=("html",))
    return {
        "id": f"smtp-{index}",
        "from": str(parsed["From"] or ""),
        "to": str(parsed["To"] or ""),
        "subject": str(parsed["Subject"] or ""),
        "body": plain.get_content() if plain is not None else "",
        "html": html.get_content() if html is not None else None,
        "attachments": [part.get_filename() for part in parsed.iter_attachments()],
        "message_id": str(parsed["Message-ID"] or "").strip("<>") or None,
        "in_reply_to": str(parsed["In-Reply-To"] or "") or None,
        "references": str(parsed["References"] or "").split(),
        "route": message.envelope_to[0] if message.envelope_to else None,
        "headers": {name: str(value) for name, value in parsed.items()},
    }


class _Handler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self) -> None:
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        self.reply("220 smtp-sink.localhost ESMTP")
        sender: Optional[str] = None
        recipients: List[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            verb, _, argument = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250 smtp-sink.localhost")
            elif verb == "MAIL":
                sender, recipients = _address(argument), []
                self.reply("250 2.1.0 Ok")
            elif verb == "RCPT":
                address = _address(argument)
                if address in sink.reject_recipients:
                    self.reply("550 5.1.1 no such mailbox")
                else:
                    recipients.append(address)
                    self.reply("250 2.1.5 Ok")
            elif verb == "DATA":
                if sender is None or not recipients:
                    self.reply("503 5.5.1 need MAIL and RCPT first")
                    continue
                self.reply("354 end with <CRLF>.<CRLF>")
                lines = []
                while True:
                    line = self.rfile.readline()
                    if not line or line == b".\r\n":
                        break
                    lines.append(line[1:] if line.startswith(b"..") else line)
                index = sink.accept(Received(
                    sender, tuple(recipients), b"".join(lines), self.client_address[0],
                ))
                self.reply(f"250 2.0.0 Ok: queued as {index}")
                sender, recipients = None, []
            elif verb in ("RSET", "NOOP"):
                sender, recipients = (None, []) if verb == "RSET" else (sender, recipients)
                self.reply("250 2.0.0 Ok")
            elif verb == "QUIT":
                self.reply("221 2.0.0 Bye")
                return
            else:
                self.reply("502 5.5.2 command not implemented")


class SmtpSink:
    """The server, on 127.0.0.1 and an ephemeral port, in a background thread."""

    def __init__(self, sink_path: Optional[Path] = None,
                 reject_recipients: Sequence[str] = ()) -> None:
        self.sink_path = Path(sink_path) if sink_path is not None else None
        self.reject_recipients = frozenset(reject_recipients)
        self.messages: List[Received] = []
        self.sessions = 0
        self._lock = threading.Lock()
        self._server: Optional[socketserver.ThreadingTCPServer] = None

    def accept(self, message: Received) -> int:
        with self._lock:
            self.messages.append(message)
            index = len(self.messages)
            if self.sink_path is not None:
                with self.sink_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_sink_line(index, message)) + "\n")
        return index

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def peers(self) -> set:
        return {message.peer for message in self.messages}

    def __enter__(self) -> "SmtpSink":
        sink = self

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

            def process_request(self, request, client_address):
                with sink._lock:
                    sink.sessions += 1
                super().process_request(request, client_address)

        self._server = Server(("127.0.0.1", 0), _Handler)
        self._server.sink = self  # type: ignore[attr-defined]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
