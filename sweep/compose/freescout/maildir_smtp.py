"""SMTP in, Maildir out: the mail delivery sidecar of the FreeScout stack.

FreeScout fetches its mail over IMAP, so a message has to land in a mailbox an
IMAP server serves before FreeScout can see it. This server does the delivery
in between. It speaks enough of RFC 5321 to take a message the way a
real mail server does (greeting, EHLO, MAIL, RCPT, DATA with dot-unstuffing,
RSET, NOOP, QUIT), and it delivers each one by recipient:

  * mail for the mailbox address (`--address`, repeatable) is written into the
    Maildir that Dovecot serves to FreeScout, the way a mail server's local
    delivery writes it: into `tmp/` first, then renamed into `new/` under a unique
    name, line endings as LF, with `Return-Path` and `Delivered-To` added at
    the top, owned by the mail user (`--uid`, `--gid`) and readable by it only.
  * mail for anyone else is what FreeScout sent out (an auto-reply, a support
    user's reply to a customer). It goes nowhere: it is appended to a JSON Lines sink
    (`--sink`) in the shape TestingHQ's `mailbox` readback kind reads, so
    `loop` can see what the helpdesk sent.

Standard library only, so it runs in a stock Python image with no install:

    python maildir_smtp.py --port 25 --maildir /srv/mail/support/Maildir \\
        --address support@freescout.test --uid 1000 --gid 1000 \\
        --sink /sink/outbound.jsonl

Nothing here does AUTH or STARTTLS. It listens inside the compose network,
and on a published port bound to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import socket
import socketserver
import sys
import threading
import time
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

#: The largest message accepted, advertised in EHLO.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
#: Seconds a connection may sit idle before it is dropped.
IDLE_TIMEOUT = 300.0

_COUNTER = itertools.count(1)
_COUNTER_LOCK = threading.Lock()


def _address(argument: str) -> str:
    """`FROM:<a@b.test> SIZE=10` -> `a@b.test`."""
    _, _, rest = argument.partition(":")
    rest = rest.strip()
    if rest.startswith("<"):
        return rest[1:].split(">", 1)[0].strip()
    return rest.split(" ", 1)[0].strip()


def _safe_hostname(name: str) -> str:
    """A host name as a Maildir file name may carry it: `/` and `:` are
    written as octal escapes, the way the Maildir specification asks."""
    return name.replace("/", "\\057").replace(":", "\\072") or "localhost"


def unique_name(
    *, clock: Callable[[], float] = time.time, hostname: Optional[str] = None
) -> str:
    """`<seconds>.M<microseconds>P<pid>Q<counter>.<host>`, the Maildir name
    Dovecot itself writes. Unique per delivery within one process, and across
    processes and hosts by the pid and the host name."""
    now = clock()
    seconds = int(now)
    micros = int((now - seconds) * 1_000_000)
    with _COUNTER_LOCK:
        count = next(_COUNTER)
    host = _safe_hostname(hostname if hostname is not None else socket.gethostname())
    return f"{seconds}.M{micros}P{os.getpid()}Q{count}.{host}"


def _chown(path: Path, uid: Optional[int], gid: Optional[int]) -> None:
    if uid is not None or gid is not None:
        os.chown(path, -1 if uid is None else uid, -1 if gid is None else gid)


def ensure_maildir(maildir: Path, *, uid: Optional[int] = None, gid: Optional[int] = None) -> None:
    """Create `cur/`, `new/` and `tmp/` (and the parents), owned by the mail
    user, so the IMAP server running as that user can move what lands here."""
    created: List[Path] = []
    for path in [*reversed(maildir.parents), maildir]:
        if path == Path(path.anchor) or path.exists():
            continue
        path.mkdir(mode=0o700)
        created.append(path)
    for sub in ("cur", "new", "tmp"):
        path = maildir / sub
        if not path.exists():
            path.mkdir(mode=0o700)
            created.append(path)
    # The Maildir itself and anything made here belong to the mail user, and
    # so does the directory holding it (the user's home), which Dovecot writes
    # its index files into.
    for path in {*created, maildir, maildir.parent} | {maildir / s for s in ("cur", "new", "tmp")}:
        _chown(path, uid, gid)


def as_delivered(data: bytes, envelope_from: str, recipient: str) -> bytes:
    """The message as a mail server's local delivery stores it: `Return-Path` and
    `Delivered-To` on top, and every line ending LF, since a Maildir file is a
    Unix text file and the IMAP server converts back to CRLF on the wire."""
    body = data.replace(b"\r\n", b"\n")
    if not body.endswith(b"\n"):
        body += b"\n"
    top = f"Return-Path: <{envelope_from}>\nDelivered-To: {recipient}\n".encode("utf-8")
    return top + body


def maildir_deliver(
    maildir: Path,
    data: bytes,
    *,
    envelope_from: str,
    recipient: str,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    clock: Callable[[], float] = time.time,
    hostname: Optional[str] = None,
) -> Path:
    """Write one message into `maildir/new/` and return its path.

    Written in `tmp/` under a name nothing else uses, flushed to disk, then
    renamed into `new/`. A reader of `new/` therefore sees either nothing or
    the whole message, never part of one.
    """
    maildir = Path(maildir)
    ensure_maildir(maildir, uid=uid, gid=gid)
    content = as_delivered(data, envelope_from, recipient)
    while True:
        name = unique_name(clock=clock, hostname=hostname)
        staging = maildir / "tmp" / name
        try:
            descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        break
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _chown(staging, uid, gid)
        final = maildir / "new" / name
        os.rename(staging, final)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return final


def sink_line(data: bytes, envelope_from: str, recipients: Sequence[str], index: str) -> dict:
    """One `mailbox` readback line for a message the helpdesk sent out."""
    parsed = message_from_bytes(data, policy=default_policy)
    plain = parsed.get_body(preferencelist=("plain",))
    html = parsed.get_body(preferencelist=("html",))
    return {
        "id": index,
        "from": str(parsed["From"] or envelope_from),
        "to": str(parsed["To"] or ", ".join(recipients)),
        "subject": str(parsed["Subject"] or ""),
        "body": plain.get_content() if plain is not None else "",
        "html": html.get_content() if html is not None else None,
        "attachments": [part.get_filename() for part in parsed.iter_attachments()],
        "message_id": str(parsed["Message-ID"] or "").strip().strip("<>") or None,
        "in_reply_to": str(parsed["In-Reply-To"] or "").strip() or None,
        "references": str(parsed["References"] or "").split(),
        "route": recipients[0] if recipients else None,
        "headers": {name: str(value) for name, value in parsed.items()},
    }


class Delivery:
    """Decides where each message goes and puts it there."""

    def __init__(
        self,
        maildir: Path,
        addresses: Iterable[str],
        *,
        sink: Optional[Path] = None,
        uid: Optional[int] = None,
        gid: Optional[int] = None,
        on_delivered: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.maildir = Path(maildir)
        self.addresses = frozenset(a.strip().lower() for a in addresses if a.strip())
        if not self.addresses:
            raise ValueError("at least one mailbox address is needed")
        self.sink = Path(sink) if sink is not None else None
        self.uid = uid
        self.gid = gid
        self.on_delivered = on_delivered
        self._lock = threading.Lock()
        self.delivered = 0
        self.sunk = 0

    def is_local(self, recipient: str) -> bool:
        return recipient.strip().lower() in self.addresses

    def __call__(self, envelope_from: str, recipients: Sequence[str], data: bytes) -> Tuple[int, str]:
        local = [r for r in recipients if self.is_local(r)]
        outbound = [r for r in recipients if not self.is_local(r)]
        with self._lock:
            receipt = []
            if local:
                path = maildir_deliver(
                    self.maildir, data, envelope_from=envelope_from, recipient=local[0],
                    uid=self.uid, gid=self.gid,
                )
                self.delivered += 1
                receipt.append(path.name)
                _log(f"delivered to {local[0]} as new/{path.name}")
            if outbound:
                self.sunk += 1
                index = f"out-{self.sunk}"
                if self.sink is not None:
                    self.sink.parent.mkdir(parents=True, exist_ok=True)
                    with self.sink.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(sink_line(data, envelope_from, outbound, index)) + "\n")
                receipt.append(index)
                _log(f"outbound for {', '.join(outbound)} recorded as {index}")
        if local and self.on_delivered is not None:
            self.on_delivered(self.maildir)
        return 250, f"2.0.0 Ok: queued as {' '.join(receipt)}"


def _log(line: str) -> None:
    print(line, flush=True)


class _Handler(socketserver.StreamRequestHandler):
    timeout = IDLE_TIMEOUT

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8", "replace") + b"\r\n")

    def _read_data(self) -> Optional[bytes]:
        """The DATA section, dot-unstuffed, or None when it ran past the size
        limit (the rest is still read, so the SMTP exchange stays in step)."""
        lines: List[bytes] = []
        size = 0
        too_big = False
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b"."):
                line = line[1:]
            size += len(line)
            if size > MAX_MESSAGE_BYTES:
                too_big = True
                continue
            lines.append(line)
        return None if too_big else b"".join(lines)

    def handle(self) -> None:
        deliver: Delivery = self.server.deliver  # type: ignore[attr-defined]
        name = self.server.banner_name  # type: ignore[attr-defined]
        self.reply(f"220 {name} ESMTP")
        sender: Optional[str] = None
        recipients: List[str] = []
        while True:
            try:
                raw = self.rfile.readline()
            except (socket.timeout, OSError):
                return
            if not raw:
                return
            verb, _, argument = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            verb = verb.upper()
            if verb == "EHLO":
                self.reply(f"250-{name}")
                self.reply("250-8BITMIME")
                self.reply("250-SMTPUTF8")
                self.reply(f"250 SIZE {MAX_MESSAGE_BYTES}")
            elif verb == "HELO":
                self.reply(f"250 {name}")
            elif verb == "MAIL":
                sender, recipients = _address(argument), []
                self.reply("250 2.1.0 Ok")
            elif verb == "RCPT":
                if sender is None:
                    self.reply("503 5.5.1 need MAIL first")
                    continue
                address = _address(argument)
                if "@" not in address:
                    self.reply("501 5.1.3 bad recipient address")
                    continue
                recipients.append(address)
                self.reply("250 2.1.5 Ok")
            elif verb == "DATA":
                if sender is None or not recipients:
                    self.reply("503 5.5.1 need MAIL and RCPT first")
                    continue
                self.reply("354 end with <CRLF>.<CRLF>")
                data = self._read_data()
                if data is None:
                    self.reply("552 5.3.4 message too big")
                else:
                    try:
                        code, text = deliver(sender, tuple(recipients), data)
                    except Exception as exc:  # noqa: BLE001 - reported to the client
                        _log(f"delivery failed: {type(exc).__name__}: {exc}")
                        code, text = 451, "4.3.0 local delivery failed"
                    self.reply(f"{code} {text}")
                sender, recipients = None, []
            elif verb == "RSET":
                sender, recipients = None, []
                self.reply("250 2.0.0 Ok")
            elif verb == "NOOP":
                self.reply("250 2.0.0 Ok")
            elif verb == "QUIT":
                self.reply("221 2.0.0 Bye")
                return
            else:
                self.reply("502 5.5.2 command not implemented")


class MaildirSmtpServer(socketserver.ThreadingTCPServer):
    """The server. `serve_forever` in a thread, or `main` in a container."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], deliver: Delivery, *, banner_name: str = "deliver.localhost") -> None:
        self.deliver = deliver
        self.banner_name = banner_name
        super().__init__(address, _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--listen", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=25)
    parser.add_argument("--maildir", required=True, type=Path, help="the Maildir mailbox mail is delivered into")
    parser.add_argument("--address", action="append", required=True, help="a recipient delivered into the Maildir; repeatable")
    parser.add_argument("--sink", type=Path, help="JSON Lines file for every other recipient's mail")
    parser.add_argument("--uid", type=int, help="owner of what is written into the Maildir")
    parser.add_argument("--gid", type=int, help="group of what is written into the Maildir")
    args = parser.parse_args(argv)

    deliver = Delivery(args.maildir, args.address, sink=args.sink, uid=args.uid, gid=args.gid)
    ensure_maildir(args.maildir, uid=args.uid, gid=args.gid)
    server = MaildirSmtpServer((args.listen, args.port), deliver)
    _log(
        f"listening on {args.listen}:{server.port}, delivering "
        f"{', '.join(sorted(deliver.addresses))} into {args.maildir}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
