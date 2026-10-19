"""A stand-in for the FreeScout stack, for tests that cannot run Docker.

The real chain is: SMTP into the deliver sidecar, which writes a Maildir;
Dovecot serves the Maildir over IMAP; FreeScout fetches it every minute and
writes `conversations`, `threads` and `attachments` rows; the readback adapter
reads those rows with SQL. This fake keeps every link it can:

  * SMTP in is the deliver sidecar itself (sweep/compose/freescout/
    maildir_smtp.py), writing a real Maildir in a temporary directory.
  * The fetch reads `new/`, does what FreeScout's `freescout:fetch-emails`
    does with each message (app/Console/Commands/FetchEmails.php: skip a
    Message-ID it has stored before, take the sender from Reply-To before
    From, thread a reply into the conversation it answers, keep the HTML part
    or else the escaped text with line breaks as `<br />`, keep the raw header
    block), and moves the file to `cur/` as seen. It runs as soon as a message
    is delivered rather than once a minute.
  * The database is SQLite with FreeScout's table and column names, behind a
    connection that takes PyMySQL's `%s` placeholders, so the readback adapter
    runs its own SQL against it unchanged.

    with FakeFreescout() as fake:
        fake.smtp_port                       # send here
        fake.dsn                             # FREESCOUT_MYSQL_DSN for it
        FreescoutMysqlAdapter(table, connect=fake.connect)
        sys.modules["pymysql"] = fake.pymysql_module()   # or this, for the CLI

Standard library only. Run on its own, it takes mail and prints what it
stored:

    python sweep/fakes/freescout.py --port 3425 [--db freescout.sqlite]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

_SIDECAR = Path(__file__).resolve().parents[1] / "compose" / "freescout" / "maildir_smtp.py"


def _load_sidecar() -> types.ModuleType:
    name = "thq_freescout_maildir_smtp"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SIDECAR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


maildir_smtp = _load_sidecar()

MAILBOX_EMAIL = "support@freescout.test"
DB_USER = "thq_readback"
DB_NAME = "freescout"
DB_HOST = "127.0.0.1"
DB_PORT = 3406

# FreeScout's constants (app/Conversation.php, app/Thread.php).
CONVERSATION_TYPE_EMAIL = 1
CONVERSATION_STATE_PUBLISHED = 2
CONVERSATION_STATUS_ACTIVE = 1
THREAD_TYPE_CUSTOMER = 1
THREAD_STATE_PUBLISHED = 2

#: The columns of FreeScout's tables that the readback reads, and the ones
#: that make a row look like FreeScout's.
SCHEMA = """
CREATE TABLE mailboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number INTEGER NOT NULL,
    threads_count INTEGER NOT NULL DEFAULT 0,
    type INTEGER NOT NULL,
    status INTEGER NOT NULL DEFAULT 1,
    state INTEGER NOT NULL DEFAULT 1,
    subject TEXT,
    customer_email TEXT,
    preview TEXT NOT NULL DEFAULT '',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    mailbox_id INTEGER NOT NULL
);
CREATE TABLE threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    type INTEGER NOT NULL,
    state INTEGER NOT NULL DEFAULT 1,
    body TEXT,
    headers TEXT,
    `from` TEXT,
    `to` TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    message_id TEXT UNIQUE,
    first INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    type INTEGER NOT NULL DEFAULT 0,
    size INTEGER,
    embedded INTEGER NOT NULL DEFAULT 0
);
"""


class FakeDatabaseError(Exception):
    """What the fake's driver raises, as PyMySQL raises OperationalError."""


class _Cursor:
    def __init__(self, store: "FreescoutStore") -> None:
        self._store = store
        self._rows: List[Tuple[Any, ...]] = []

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        self._rows = self._store.query(sql.replace("%s", "?"), tuple(args))
        return len(self._rows)

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self._rows = []


class _Connection:
    def __init__(self, store: "FreescoutStore") -> None:
        self._store = store
        self.closed = False

    def cursor(self) -> _Cursor:
        if self.closed:
            raise FakeDatabaseError("connection is closed")
        return _Cursor(self._store)

    def close(self) -> None:
        self.closed = True


class FreescoutStore:
    """FreeScout's tables, and what its fetch writes into them."""

    def __init__(self, path: str = ":memory:", mailbox_email: str = MAILBOX_EMAIL) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute(
                "INSERT INTO mailboxes (name, email) VALUES (?, ?)", ("Support", mailbox_email)
            )
            self._db.commit()
        self.mailbox_id = 1
        self.password = secrets.token_hex(8)
        self.connections = 0

    # -- the driver --------------------------------------------------------

    def connect(self, **params: Any) -> _Connection:
        """PyMySQL's `connect`, checking the credentials a DSN for this fake
        carries, so a wrong DSN fails the way a real server refuses it."""
        expected = {
            "host": DB_HOST, "port": DB_PORT, "user": DB_USER,
            "password": self.password, "database": DB_NAME,
        }
        for key, value in expected.items():
            if params.get(key) != value:
                raise FakeDatabaseError(
                    f"(1045, \"Access denied for user '{params.get('user')}'\")"
                )
        if params.get("autocommit") is not True:
            raise FakeDatabaseError("the readback must connect with autocommit on")
        self.connections += 1
        return _Connection(self)

    @property
    def dsn(self) -> str:
        return f"mysql://{DB_USER}:{self.password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

    def pymysql_module(self) -> types.ModuleType:
        """A module that stands in for `pymysql`, for code that imports it."""
        module = types.ModuleType("pymysql")
        module.connect = self.connect  # type: ignore[attr-defined]
        return module

    def query(self, sql: str, args: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        with self._lock:
            try:
                return [tuple(row) for row in self._db.execute(sql, args).fetchall()]
            except sqlite3.Error as exc:
                raise FakeDatabaseError(str(exc)) from exc

    # -- the fetch -----------------------------------------------------------

    def ingest(self, raw: bytes) -> Optional[int]:
        """Store one message as FreeScout's fetch does. Returns the new
        thread id, or None for a Message-ID it has stored before."""
        message = message_from_bytes(raw, policy=default_policy)
        sender = _first_address(message.get_all("Reply-To", [])) or _first_address(
            message.get_all("From", [])
        )
        if not sender:
            return None  # FetchEmails: "From is empty", marked seen, skipped.
        message_id = str(message.get("Message-ID", "")).strip().strip("<>").strip()
        if not message_id:
            message_id = hashlib.sha1(raw).hexdigest() + "@freescout.fake"
        subject = str(message.get("Subject", "") or "")
        body = _stored_body(message)
        headers = _raw_headers(raw)
        attachments = [
            (part.get_filename() or "attachment", part.get_content_type(),
             len(part.get_payload(decode=True) or b""))
            for part in message.iter_attachments()
        ]
        references = [
            token.strip("<>")
            for value in (message.get("In-Reply-To", ""), message.get("References", ""))
            for token in str(value or "").split()
        ]
        with self._lock:
            db = self._db
            if db.execute("SELECT 1 FROM threads WHERE message_id = ?", (message_id,)).fetchone():
                return None
            previous = None
            for reference in references:
                previous = db.execute(
                    "SELECT conversation_id FROM threads WHERE message_id = ?", (reference,)
                ).fetchone()
                if previous:
                    break
            if previous:
                conversation_id = previous[0]
                first = 0
                db.execute(
                    "UPDATE conversations SET customer_email = ?, threads_count = threads_count + 1 "
                    "WHERE id = ?", (sender, conversation_id),
                )
            else:
                number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM conversations").fetchone()[0]
                cursor = db.execute(
                    "INSERT INTO conversations (number, threads_count, type, status, state, subject, "
                    "customer_email, preview, has_attachments, mailbox_id) "
                    "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (number, CONVERSATION_TYPE_EMAIL, CONVERSATION_STATUS_ACTIVE,
                     CONVERSATION_STATE_PUBLISHED, subject, sender, _preview(body),
                     1 if attachments else 0, self.mailbox_id),
                )
                conversation_id = cursor.lastrowid
                first = 1
            cursor = db.execute(
                "INSERT INTO threads (conversation_id, type, state, body, headers, `from`, `to`, "
                "has_attachments, message_id, first) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, THREAD_TYPE_CUSTOMER, THREAD_STATE_PUBLISHED, body, headers,
                 sender, str(message.get("To", "") or ""), 1 if attachments else 0,
                 message_id, first),
            )
            thread_id = cursor.lastrowid
            for name, mime_type, size in attachments:
                db.execute(
                    "INSERT INTO attachments (thread_id, file_name, mime_type, size) VALUES (?, ?, ?, ?)",
                    (thread_id, name, mime_type, size),
                )
            db.commit()
        return thread_id

    def fetch_maildir(self, maildir: Path) -> int:
        """Take every unseen message in `maildir/new/`, oldest first, and
        mark it seen in `cur/`, as an IMAP fetch through Dovecot leaves it."""
        taken = 0
        new = Path(maildir) / "new"
        for path in sorted(new.iterdir()):
            raw = path.read_bytes()
            self.ingest(raw)
            path.rename(Path(maildir) / "cur" / f"{path.name}:2,S")
            taken += 1
        return taken

    def delete_conversation(self, number: int) -> None:
        """What deleting a conversation in FreeScout's UI does to the row."""
        with self._lock:
            self._db.execute("UPDATE conversations SET state = 3 WHERE number = ?", (number,))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _first_address(values: Sequence[Any]) -> str:
    for _name, address in getaddresses([str(v) for v in values]):
        if address and "@" in address:
            return address.strip().lower()
    return ""


def _stored_body(message) -> str:
    """The HTML part when there is one, otherwise the text part escaped with
    `htmlspecialchars` and given `<br />` line breaks by `nl2br`."""
    html_part = message.get_body(preferencelist=("html",))
    if html_part is not None:
        return html_part.get_content()
    text_part = message.get_body(preferencelist=("plain",))
    text = text_part.get_content() if text_part is not None else ""
    return html.escape(text, quote=True).replace("\n", "<br />\n")


def _raw_headers(raw: bytes) -> str:
    """The header block as the message carried it."""
    normalized = raw.replace(b"\r\n", b"\n")
    head, _sep, _body = normalized.partition(b"\n\n")
    return head.decode("utf-8", errors="replace") + "\n"


def _preview(body: str) -> str:
    text = html.unescape(body.replace("<br />", " "))
    return " ".join(text.split())[:255]


class FakeFreescout:
    """The deliver sidecar's SMTP server and a store fetching from its
    Maildir, on 127.0.0.1. A context manager; `.smtp_port` (and `.port`, the
    same) is where to send."""

    def __init__(self, *, port: int = 0, db_path: str = ":memory:",
                 mailbox_email: str = MAILBOX_EMAIL) -> None:
        self._port = port
        self.mailbox_email = mailbox_email
        self.store = FreescoutStore(db_path, mailbox_email)
        self._root: Optional[Path] = None
        self._server: Any = None
        self._fetch_lock = threading.Lock()
        self.outbound: Optional[Path] = None

    def _fetch(self, maildir: Path) -> None:
        with self._fetch_lock:
            self.store.fetch_maildir(maildir)

    def __enter__(self) -> "FakeFreescout":
        self._root = Path(tempfile.mkdtemp(prefix="thq-freescout-"))
        self.maildir = self._root / "mail" / "support" / "Maildir"
        self.outbound = self._root / "outbound.jsonl"
        delivery = maildir_smtp.Delivery(
            self.maildir, [self.mailbox_email], sink=self.outbound, on_delivered=self._fetch,
        )
        maildir_smtp.ensure_maildir(self.maildir)
        self._server = maildir_smtp.MaildirSmtpServer(("127.0.0.1", self._port), delivery)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self.store.close()
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)

    @property
    def smtp_port(self) -> int:
        return self._server.port

    port = smtp_port

    @property
    def dsn(self) -> str:
        return self.store.dsn

    def connect(self, **params: Any) -> _Connection:
        return self.store.connect(**params)

    def pymysql_module(self) -> types.ModuleType:
        return self.store.pymysql_module()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A stand-in for the FreeScout stack: SMTP in, rows out.")
    parser.add_argument("--port", type=int, default=3425, help="SMTP port on 127.0.0.1")
    parser.add_argument("--db", default=":memory:", help="SQLite file to keep the rows in")
    args = parser.parse_args(argv)
    with FakeFreescout(port=args.port, db_path=args.db) as fake:
        print(f"FreeScout fake: SMTP on 127.0.0.1:{fake.smtp_port}, rows in {args.db}", flush=True)
        seen = 0
        try:
            while True:
                time.sleep(1)
                rows = fake.store.query(
                    "SELECT t.id, c.number, c.subject, t.`from` FROM threads t "
                    "JOIN conversations c ON c.id = t.conversation_id WHERE t.id > ? ORDER BY t.id",
                    (seen,),
                )
                for thread_id, number, subject, sender in rows:
                    print(f"#{number} {sender}: {subject}", flush=True)
                    seen = thread_id
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
