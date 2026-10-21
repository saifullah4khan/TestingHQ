"""A stand-in for osTicket's email API and the MySQL tables behind it.

Stdlib only. It answers what the osTicket recipe sends and reads:

  POST /api/tickets.email   the raw message as the body, `X-API-Key` header.
                            A missing key, an unknown key, or a key bound to a
                            different address than the client's is 401, as
                            osTicket's ApiController::requireApiKey does. A new
                            message is 201 with the ticket number as the body
                            (text/html, as osTicket's Http::response sends it).
                            A Message-ID seen before is 201 with the existing
                            ticket's number and no new entry; a reply (its
                            In-Reply-To or References naming a stored
                            Message-ID) is appended to that ticket's thread.

osTicket has no read API, so the readback reads its database. This fake keeps
its tickets in SQLite under the same table and column names as osTicket's
install-mysql.sql (a subset of the columns), and `connect` hands the readback
adapter a DB-API connection to it, the way PyMySQL would hand it MySQL. The
body is stored the way osTicket stores it with rich text on, its default: the
HTML part when there is one, with format 'html', else the text part.

    python sweep/fakes/osticket.py --port 3299 --api-key KEY

As a module, `FakeOsTicket` is a context manager with `.port`, `.url`,
`.api_key`, `.requests` and `.connect`; `OsTicketStore` is the database alone,
for tests that need no socket.
"""
from __future__ import annotations

import argparse
import contextlib
import email
import email.policy
import os
import random
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass
from email.utils import getaddresses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, List, Mapping, Optional, Tuple

PREFIX = "ost_"
API_PATH = "/api/tickets.email"
DEFAULT_API_KEY = "0123456789ABCDEF0123456789ABCDEF"

SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}api_key (
  id INTEGER PRIMARY KEY AUTOINCREMENT, isactive INTEGER NOT NULL DEFAULT 1,
  ipaddr TEXT NOT NULL, apikey TEXT NOT NULL UNIQUE,
  can_create_tickets INTEGER NOT NULL DEFAULT 1,
  can_exec_cron INTEGER NOT NULL DEFAULT 1, notes TEXT);
CREATE TABLE IF NOT EXISTS {p}department (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS {p}user (
  id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER NOT NULL DEFAULT 0,
  default_email_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS {p}user_email (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  flags INTEGER NOT NULL DEFAULT 0, address TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS {p}ticket (
  ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT,
  user_id INTEGER NOT NULL DEFAULT 0, user_email_id INTEGER NOT NULL DEFAULT 0,
  dept_id INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'Other',
  ip_address TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS {p}ticket__cdata (
  ticket_id INTEGER PRIMARY KEY, subject TEXT, priority TEXT);
CREATE TABLE IF NOT EXISTS {p}thread (
  id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER NOT NULL,
  object_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS {p}thread_entry (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER NOT NULL DEFAULT 0,
  thread_id INTEGER NOT NULL DEFAULT 0, staff_id INTEGER NOT NULL DEFAULT 0,
  user_id INTEGER NOT NULL DEFAULT 0, type TEXT NOT NULL DEFAULT '',
  poster TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
  title TEXT, body TEXT NOT NULL, format TEXT NOT NULL DEFAULT 'html');
CREATE TABLE IF NOT EXISTS {p}thread_entry_email (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_entry_id INTEGER NOT NULL,
  email_id INTEGER, mid TEXT NOT NULL, headers TEXT);
CREATE TABLE IF NOT EXISTS {p}file (
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS {p}attachment (
  id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER NOT NULL,
  type TEXT NOT NULL, file_id INTEGER NOT NULL, name TEXT,
  inline INTEGER NOT NULL DEFAULT 0);
"""


class ApiRefusal(Exception):
    """An osTicket API error: the status and the text osTicket answers with."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass(frozen=True)
class Received:
    """One request the fake answered. Header names are lowercased, since
    HTTP header names are case-insensitive and clients differ in how they
    spell them."""

    path: str
    headers: Mapping[str, str]
    status: int
    peer: str


class OsTicketStore:
    """osTicket's tables in a SQLite file, and the email intake that fills
    them. Safe to use from several threads."""

    def __init__(self, db_path: str, *, seed: int = 0) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._numbers = random.Random(seed)
        with self._db() as db:
            db.executescript(SCHEMA.format(p=PREFIX))
            if not db.execute(f"SELECT COUNT(*) FROM {PREFIX}department").fetchone()[0]:
                for name in ("Support", "Sales", "Maintenance"):
                    db.execute(f"INSERT INTO {PREFIX}department (name) VALUES (?)", (name,))
                # The installer opens one ticket of its own; a listing sees it
                # as a record no run sent.
                self._create_ticket(
                    db, "support@osticket.example", "osTicket Support",
                    "osTicket Installed!", "<p>Thank you for choosing osTicket.</p>",
                    "html", "", "<installed@osticket.example>", [],
                )

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation, committed and closed after it."""
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def connect(self, dsn: Any = None, timeout: Optional[float] = None) -> sqlite3.Connection:
        """A DB-API connection to the tables, for the readback adapter. The
        DSN is accepted and ignored: there is one database."""
        return sqlite3.connect(self.db_path, timeout=timeout or 10, check_same_thread=False)

    connect.paramstyle = "qmark"  # type: ignore[attr-defined]

    def add_api_key(self, key: str, ipaddr: str = "127.0.0.1", *, can_create: bool = True) -> None:
        with self._lock, self._db() as db:
            db.execute(
                f"INSERT OR REPLACE INTO {PREFIX}api_key "
                "(isactive, ipaddr, apikey, can_create_tickets, can_exec_cron, notes) "
                "VALUES (1, ?, ?, ?, 0, 'fake')",
                (ipaddr, key, 1 if can_create else 0),
            )

    def require_api_key(self, key: Optional[str], ip: str) -> None:
        """osTicket's check: the key must exist for this client address, be
        active, and be allowed to create tickets."""
        if not key:
            raise ApiRefusal(401, "Valid API key required")
        with self._db() as db:
            row = db.execute(
                f"SELECT isactive, can_create_tickets FROM {PREFIX}api_key "
                "WHERE apikey = ? AND ipaddr = ?",
                (key, ip),
            ).fetchone()
        if row is None or not row[0]:
            raise ApiRefusal(401, "API key not found/active or source IP not authorized")
        if not row[1]:
            raise ApiRefusal(401, "API key not authorized")

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    def ingest(self, raw: bytes) -> str:
        """File one raw message and return the ticket number osTicket would."""
        if not raw.strip():
            raise ApiRefusal(400, "Unable to read email request")
        message = email.message_from_bytes(raw, policy=email.policy.default)
        senders = [a for _n, a in getaddresses([str(message.get("From", ""))]) if a]
        if not senders:
            raise ApiRefusal(400, "Unable to create new ticket :email: Valid email address required")
        sender = senders[0].lower()
        name = next((n for n, a in getaddresses([str(message["From"])]) if n), sender)
        subject = str(message.get("Subject", "") or "")
        mid = str(message.get("Message-ID", "") or "").strip()
        body, body_format = _body_of(message)
        headers = _header_block(raw)
        attachments = _attachments_of(message)
        related = _message_ids(message.get("In-Reply-To")) + _message_ids(message.get("References"))

        with self._lock, self._db() as db:
            if mid:
                seen = self._ticket_of_mid(db, [mid])
                if seen is not None:
                    return seen[1]
            parent = self._ticket_of_mid(db, related) if related else None
            if parent is not None:
                ticket_id, number = parent
                user_id = self._user(db, sender, name)
                thread_id = db.execute(
                    f"SELECT id FROM {PREFIX}thread WHERE object_type = 'T' AND object_id = ?",
                    (ticket_id,),
                ).fetchone()[0]
                # osTicket 1.18.4 stores no title on a reply it threads onto
                # a ticket: the reply's subject is only in its stored headers.
                self._entry(db, thread_id, user_id, name, None, body, body_format,
                            mid, headers, attachments)
                return number
            return self._create_ticket(db, sender, name, subject, body, body_format,
                                       headers, mid, attachments)

    def _ticket_of_mid(self, db: sqlite3.Connection, mids: List[str]) -> Optional[Tuple[int, str]]:
        for mid in mids:
            row = db.execute(
                f"SELECT t.ticket_id, t.number FROM {PREFIX}thread_entry_email m "
                f"JOIN {PREFIX}thread_entry e ON e.id = m.thread_entry_id "
                f"JOIN {PREFIX}thread th ON th.id = e.thread_id AND th.object_type = 'T' "
                f"JOIN {PREFIX}ticket t ON t.ticket_id = th.object_id "
                "WHERE m.mid = ? ORDER BY m.id LIMIT 1",
                (mid,),
            ).fetchone()
            if row is not None:
                return row[0], row[1]
        return None

    def _user(self, db: sqlite3.Connection, address: str, name: str) -> int:
        row = db.execute(
            f"SELECT user_id FROM {PREFIX}user_email WHERE address = ?", (address,)
        ).fetchone()
        if row is not None:
            return row[0]
        user_id = db.execute(
            f"INSERT INTO {PREFIX}user (name) VALUES (?)", (name,)
        ).lastrowid
        email_id = db.execute(
            f"INSERT INTO {PREFIX}user_email (user_id, address) VALUES (?, ?)",
            (user_id, address),
        ).lastrowid
        db.execute(
            f"UPDATE {PREFIX}user SET default_email_id = ? WHERE id = ?", (email_id, user_id)
        )
        return user_id

    def _number(self, db: sqlite3.Connection) -> str:
        while True:
            number = f"{self._numbers.randrange(100000, 1000000)}"
            if db.execute(
                f"SELECT 1 FROM {PREFIX}ticket WHERE number = ?", (number,)
            ).fetchone() is None:
                return number

    def _create_ticket(self, db, sender, name, subject, body, body_format, headers,
                       mid, attachments) -> str:
        user_id = self._user(db, sender, name)
        number = self._number(db)
        ticket_id = db.execute(
            f"INSERT INTO {PREFIX}ticket (number, user_id, dept_id, source) "
            "VALUES (?, ?, 1, 'Email')",
            (number, user_id),
        ).lastrowid
        db.execute(
            f"INSERT INTO {PREFIX}ticket__cdata (ticket_id, subject) VALUES (?, ?)",
            (ticket_id, subject),
        )
        thread_id = db.execute(
            f"INSERT INTO {PREFIX}thread (object_id, object_type) VALUES (?, 'T')",
            (ticket_id,),
        ).lastrowid
        self._entry(db, thread_id, user_id, name, subject, body, body_format, mid,
                    headers, attachments)
        return number

    def _entry(self, db, thread_id, user_id, name, subject, body, body_format, mid,
               headers, attachments) -> None:
        entry_id = db.execute(
            f"INSERT INTO {PREFIX}thread_entry "
            "(thread_id, user_id, type, poster, source, title, body, format) "
            "VALUES (?, ?, 'M', ?, 'Email', ?, ?, ?)",
            (thread_id, user_id, name, subject, body, body_format),
        ).lastrowid
        if mid:
            db.execute(
                f"INSERT INTO {PREFIX}thread_entry_email (thread_entry_id, mid, headers) "
                "VALUES (?, ?, ?)",
                (entry_id, mid, headers),
            )
        for filename, content_type, size, inline in attachments:
            file_id = db.execute(
                f"INSERT INTO {PREFIX}file (type, size, name) VALUES (?, ?, ?)",
                (content_type, size, filename),
            ).lastrowid
            db.execute(
                f"INSERT INTO {PREFIX}attachment (object_id, type, file_id, name, inline) "
                "VALUES (?, 'H', ?, ?, ?)",
                (entry_id, file_id, filename, 1 if inline else 0),
            )

    # ------------------------------------------------------------------
    # Reading, for tests
    # ------------------------------------------------------------------

    def ticket_numbers(self) -> List[str]:
        with self._db() as db:
            return [row[0] for row in db.execute(
                f"SELECT number FROM {PREFIX}ticket ORDER BY ticket_id"
            )]


def _body_of(message: email.message.EmailMessage) -> Tuple[str, str]:
    """osTicket's MailParser::getBody with rich text on: the HTML part if
    there is one, else the text part."""
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        return html.get_content(), "html"
    text = message.get_body(preferencelist=("plain",))
    if text is not None:
        return text.get_content(), "text"
    return "", "text"


def _attachments_of(message: email.message.EmailMessage) -> List[Tuple[str, str, int, bool]]:
    found = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        inline = (part.get_content_disposition() or "") == "inline"
        found.append((filename, part.get_content_type(), len(payload), inline))
    return found


def _header_block(raw: bytes) -> str:
    """The raw header block, as osTicket keeps it in thread_entry_email."""
    for separator in (b"\r\n\r\n", b"\n\n"):
        head, found, _rest = raw.partition(separator)
        if found:
            return head.decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def _message_ids(value: Any) -> List[str]:
    if not value:
        return []
    return [f"<{token.strip('<>')}>" for token in str(value).split() if token.strip("<>")]


class FakeOsTicket:
    """The HTTP intake on 127.0.0.1 plus its store, as a context manager."""

    def __init__(
        self,
        port: int = 0,
        *,
        api_key: str = DEFAULT_API_KEY,
        key_ip: str = "127.0.0.1",
        db_path: Optional[str] = None,
    ) -> None:
        self._owned_dir = None
        if db_path is None:
            self._owned_dir = tempfile.mkdtemp(prefix="thq-osticket-")
            db_path = os.path.join(self._owned_dir, "osticket.sqlite3")
        self.store = OsTicketStore(db_path)
        self.api_key = api_key
        self.store.add_api_key(api_key, key_ip)
        self.requests: List[Received] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(self))
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{API_PATH}"

    @property
    def connect(self):
        return self.store.connect

    def start(self) -> "FakeOsTicket":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._owned_dir is not None:
            shutil.rmtree(self._owned_dir, ignore_errors=True)

    def __enter__(self) -> "FakeOsTicket":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _handler_for(fake: FakeOsTicket):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # quiet
            return None

        def _answer(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            fake.requests.append(Received(
                path=self.path, status=status,
                headers={k.lower(): v for k, v in self.headers.items()},
                peer=self.client_address[0],
            ))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.path.split("?", 1)[0] != API_PATH:
                self._answer(400, "Unable to find handler")
                return
            try:
                fake.store.require_api_key(self.headers.get("X-API-Key"), self.client_address[0])
                number = fake.store.ingest(raw)
            except ApiRefusal as refusal:
                self._answer(refusal.status, refusal.text)
                return
            self._answer(201, number)

        def do_GET(self) -> None:
            self._answer(400, "Unable to find handler")

    return Handler


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A fake osTicket email API and store.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--api-key", default=os.environ.get("OSTICKET_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--key-ip", default="127.0.0.1",
                        help="the client address the key is bound to")
    parser.add_argument("--db", default=None, help="SQLite file to keep the tables in")
    args = parser.parse_args(argv)
    fake = FakeOsTicket(args.port, api_key=args.api_key, key_ip=args.key_ip, db_path=args.db)
    print(f"osTicket fake: {fake.url}", flush=True)
    print(f"tables: {fake.store.db_path}", flush=True)
    # A plain kill stops it as cleanly as Ctrl-C, removing its temporary tables.
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    try:
        fake._server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

