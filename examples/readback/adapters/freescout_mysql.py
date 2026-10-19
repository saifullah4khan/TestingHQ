"""A readback adapter for FreeScout that reads its MySQL or MariaDB database.

FreeScout has no free API: its REST API is a paid module. What it does have
is a database, and every message it fetches from a mailbox becomes rows there:
a `conversations` row (the ticket, with its `number`, `subject` and
`customer_email`) and a `threads` row per message (the `body`, the sender in
`from`, the raw header block in `headers`, the `message_id`), plus an
`attachments` row per stored file. The table and column names are the ones
FreeScout's own migrations create (database/migrations in the FreeScout
repository).

A message is found by its tag. TestingHQ stamps the tag into an
`X-TestingHQ-Tag` header and into the body as `[testinghq:<tag>]`, and
FreeScout keeps both: the header block verbatim in `threads.headers`, the body
in `threads.body`. The lookup asks for threads whose headers or body contain
the tag, then keeps only the ones whose recovered tag is exactly it, so a tag
that happens to be a prefix of another never matches the wrong message.

Each thread that carries the tag is one record. The record's `ticket_id` is
the conversation `number` FreeScout shows as `#123`, and everything but the
subject is the thread's own. The subject is the conversation's, which is the
subject of the message that opened it, so it is read for that thread only: a
reply FreeScout threads into a conversation has no subject of its own there,
and its subject check says the field is not visible rather than comparing the
reply with its original's subject. A message delivered
twice that FreeScout stored twice is two records, which is the duplicate the
ledger and redelivery tools look for.

Configuration, in the `[readback]` table:

    kind = "examples.readback.adapters.freescout_mysql:FreescoutMysqlAdapter"
    dsn = "env:FREESCOUT_MYSQL_DSN"        # required, always an env: reference
    mailbox = "support@freescout.test"     # optional: one mailbox only
    table_prefix = ""                      # optional: FreeScout's DB_TABLE_PREFIX
    timeout = 10.0                         # connect and read timeout, seconds

The DSN is `mysql://user:password@host:port/database`, with the password
percent-encoded where it holds `@`, `:` or `/`. It carries a password, so a
literal DSN in the file is refused: only an `env:NAME` reference is accepted,
and the variable is read when the adapter first connects, which `preflight`
does before anything is sent. A read-only database user is enough; the
adapter only ever runs SELECT.

PyMySQL is this example's own dependency, not TestingHQ's:

    pip install PyMySQL

It is imported when the adapter first connects, so importing this module, or
building the adapter for `config validate`, needs nothing installed. Tests
pass `connect=` instead, a callable taking PyMySQL's keyword arguments and
returning a DB-API connection.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from testinghq.core.config import ENV_PREFIX, resolve_env_references
from testinghq.pipeline.adapters import AdapterError
from testinghq.pipeline.messages import harvest_tag
from testinghq.pipeline.readback import FetchResult, Readback

#: DSN schemes accepted. All mean the same MySQL wire protocol.
DSN_SCHEMES: Tuple[str, ...] = ("mysql", "mariadb", "mysql+pymysql")
DEFAULT_PORT = 3306
DEFAULT_CHARSET = "utf8mb4"
DEFAULT_TIMEOUT = 10.0

#: FreeScout's `threads.type` values for a message, as app/Thread.php names
#: them: TYPE_CUSTOMER (1) is mail from a customer, TYPE_MESSAGE (2) is mail
#: from a user. Notes (3) and line items (4) are FreeScout's own annotations,
#: never a message that arrived, so they are not records.
MESSAGE_THREAD_TYPES: Tuple[int, ...] = (1, 2)

#: `conversations.state` for a deleted conversation (Conversation::STATE_DELETED).
STATE_DELETED = 3

#: The fields this adapter can see. `ticket_id` is always present.
VISIBLE_FIELDS: Tuple[str, ...] = (
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "message_id",
    "tag",
)

_PREFIX_SHAPE = re.compile(r"^[A-Za-z0-9_]*$")

#: A callable taking PyMySQL's connect keywords and returning a DB-API
#: connection.
ConnectFactory = Callable[..., Any]


def pymysql_connect(**params: Any) -> Any:
    """Open a connection with PyMySQL, imported here and nowhere else."""
    try:
        import pymysql
    except ImportError as exc:
        raise AdapterError(
            "the FreeScout readback adapter needs PyMySQL, which TestingHQ does "
            "not install: pip install PyMySQL"
        ) from exc
    return pymysql.connect(**params)


def parse_dsn(dsn: str, *, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """`mysql://user:password@host:port/database` as PyMySQL connect keywords.

    No message raised here quotes the DSN, because it holds a password.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise AdapterError("[readback].dsn resolved to an empty value")
    parts = urlsplit(dsn.strip())
    if parts.scheme.lower() not in DSN_SCHEMES:
        raise AdapterError(
            f"[readback].dsn must start with one of "
            f"{', '.join(s + '://' for s in DSN_SCHEMES)}; got a "
            f"{parts.scheme or 'scheme-less'} DSN"
        )
    if not parts.hostname:
        raise AdapterError("[readback].dsn names no host")
    if not parts.username:
        raise AdapterError("[readback].dsn names no user")
    database = unquote(parts.path.lstrip("/"))
    if not database or "/" in database:
        raise AdapterError("[readback].dsn must name one database after the host")
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as exc:
        raise AdapterError("[readback].dsn has a port that is not a number") from exc
    query = dict(parse_qsl(parts.query))
    return {
        "host": parts.hostname,
        "port": port,
        "user": unquote(parts.username),
        "password": unquote(parts.password or ""),
        "database": database,
        "charset": query.get("charset", DEFAULT_CHARSET),
        "connect_timeout": timeout,
        "read_timeout": timeout,
        "write_timeout": timeout,
        # Every SELECT its own transaction. Without autocommit, InnoDB's
        # repeatable read would keep answering every poll from the snapshot
        # the first one took, and a ticket created after it would never show.
        "autocommit": True,
    }


class FreescoutMysqlAdapter:
    """Reads FreeScout's conversations and threads for a tag."""

    can_enumerate = True

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        connect: Optional[ConnectFactory] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        table = dict(config or {})
        dsn = table.get("dsn")
        if dsn is None:
            raise AdapterError(
                'the FreeScout readback adapter needs [readback].dsn, for example '
                'dsn = "env:FREESCOUT_MYSQL_DSN"'
            )
        if not isinstance(dsn, str) or not dsn.startswith(ENV_PREFIX):
            raise AdapterError(
                "[readback].dsn is a literal value. A DSN carries the database "
                f'password, so it must come from the environment as "{ENV_PREFIX}'
                '<VARIABLE>"; a config file is a file that gets shared, and a '
                "secret in it is a secret that leaks"
            )
        self._dsn_reference = dsn
        self._env = env

        prefix = table.get("table_prefix", "")
        if not isinstance(prefix, str) or not _PREFIX_SHAPE.match(prefix):
            raise AdapterError(
                "[readback].table_prefix may hold only letters, digits and "
                "underscores, as FreeScout's DB_TABLE_PREFIX does"
            )
        self._prefix = prefix

        mailbox = table.get("mailbox")
        if mailbox is not None and (not isinstance(mailbox, str) or "@" not in mailbox):
            raise AdapterError("[readback].mailbox must be the mailbox's email address")
        self.mailbox = mailbox.strip() if isinstance(mailbox, str) else None

        timeout = table.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise AdapterError("[readback].timeout must be a positive number of seconds")
        self._timeout = float(timeout)

        unmapped = set(table.get("unmapped_fields") or ())
        self._fields = tuple(name for name in VISIBLE_FIELDS if name not in unmapped)

        self._connect = connect or pymysql_connect
        self._connection: Any = None

    # ------------------------------------------------------------------
    # The connection
    # ------------------------------------------------------------------

    def _params(self) -> Dict[str, Any]:
        resolved = resolve_env_references(
            "[readback]", {"dsn": self._dsn_reference}, self._env, error=AdapterError
        )
        return parse_dsn(resolved["dsn"], timeout=self._timeout)

    def _open(self) -> Any:
        params = self._params()
        try:
            return self._connect(**params)
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - any driver failure is a refusal
            raise AdapterError(
                f"could not connect to FreeScout's database at "
                f"{params['host']}:{params['port']}/{params['database']} as "
                f"{params['user']}: {type(exc).__name__}: {exc}"
            ) from exc

    def _rows(self, sql: str, args: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        """Run one SELECT, reconnecting once if the connection has gone away."""
        for attempt in (1, 2):
            if self._connection is None:
                self._connection = self._open()
            try:
                cursor = self._connection.cursor()
                try:
                    cursor.execute(sql, tuple(args))
                    return [tuple(row) for row in cursor.fetchall()]
                finally:
                    cursor.close()
            except Exception as exc:  # noqa: BLE001 - reported below
                self._drop_connection()
                if attempt == 2:
                    raise AdapterError(
                        f"FreeScout readback query failed: {type(exc).__name__}: {exc}"
                    ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def _drop_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------
    #
    # Kept to what MySQL, MariaDB and SQLite all accept: INSTR rather than
    # LIKE (no wildcard escaping to get wrong), backticks around `from`,
    # which is a reserved word, and %s placeholders as PyMySQL takes them.

    def _t(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def _select(self, where: str) -> str:
        types = ", ".join(str(t) for t in MESSAGE_THREAD_TYPES)
        mailbox = " AND m.email = %s" if self.mailbox else ""
        return (
            "SELECT c.number, c.subject, c.state, m.email, "
            "t.id, t.`from`, t.body, t.headers, t.message_id, t.`first` "
            f"FROM {self._t('threads')} t "
            f"JOIN {self._t('conversations')} c ON c.id = t.conversation_id "
            f"LEFT JOIN {self._t('mailboxes')} m ON m.id = c.mailbox_id "
            f"WHERE t.type IN ({types}){where}{mailbox} "
            "ORDER BY t.id"
        )

    def _mailbox_args(self) -> Tuple[Any, ...]:
        return (self.mailbox,) if self.mailbox else ()

    def _attachments(self, thread_ids: Sequence[int]) -> Dict[int, List[str]]:
        names: Dict[int, List[str]] = {thread_id: [] for thread_id in thread_ids}
        if not thread_ids:
            return names
        marks = ", ".join("%s" for _ in thread_ids)
        rows = self._rows(
            f"SELECT thread_id, file_name FROM {self._t('attachments')} "
            f"WHERE thread_id IN ({marks}) ORDER BY id",
            thread_ids,
        )
        for thread_id, file_name in rows:
            if file_name:
                names.setdefault(thread_id, []).append(str(file_name))
        return names

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def _readbacks(self, rows: List[Tuple[Any, ...]], want: Optional[str]) -> FetchResult:
        tagged = []
        for row in rows:
            headers, body = _text(row[7]), _text(row[6])
            tag = harvest_tag(headers, body)
            if want is not None and tag != want:
                continue
            tagged.append((row, tag))
        attachments = (
            self._attachments([row[4] for row, _tag in tagged])
            if "attachment_names" in self._fields
            else {}
        )
        found = FetchResult()
        for row, tag in tagged:
            (number, subject, state, mailbox_email, thread_id, sender, body, _headers,
             message_id, first) = row
            values: Dict[str, Any] = {
                "from_addr": _text(sender),
                # The conversation's subject is the subject of the message that
                # opened it. A reply threaded in later has no subject of its
                # own in FreeScout, so for it the field is not visible.
                "subject": _text(subject) if first else None,
                "body": _text(body),
                "attachment_names": tuple(attachments.get(thread_id, ())),
                "route": _text(mailbox_email),
                "message_id": _text(message_id),
                "tag": tag,
            }
            kept = {name: values[name] for name in self._fields}
            present = tuple(
                name for name, value in kept.items()
                if value is not None or name == "attachment_names"
            )
            found.append(
                Readback(
                    exists=state != STATE_DELETED,
                    ticket_id=str(number),
                    fields=present,
                    **{name: value for name, value in kept.items() if name in present},
                )
            )
        return found

    def preflight(self) -> None:
        """Connect, and check the tables and columns this adapter reads exist,
        and the configured mailbox with them, before anything is sent."""
        try:
            self._rows(self._select(" AND 1 = 0"), self._mailbox_args())
            self._rows(f"SELECT thread_id, file_name FROM {self._t('attachments')} WHERE 1 = 0")
        except AdapterError as exc:
            raise AdapterError(
                f"FreeScout's tables are not readable as this adapter expects "
                f"({exc}). Check the DSN names FreeScout's database and, if "
                "FreeScout sets DB_TABLE_PREFIX, that table_prefix matches it."
            ) from exc
        if self.mailbox and not self._rows(
            f"SELECT id FROM {self._t('mailboxes')} WHERE email = %s", (self.mailbox,)
        ):
            raise AdapterError(
                f"FreeScout has no mailbox with the address {self.mailbox!r}; "
                "messages fetched into it could never be found"
            )

    def fetch(self, probe) -> FetchResult:
        tag = probe.tag
        rows = self._rows(
            self._select(" AND (INSTR(t.headers, %s) > 0 OR INSTR(t.body, %s) > 0)"),
            (tag, tag, *self._mailbox_args()),
        )
        return self._readbacks(rows, tag)

    def list_all(self) -> FetchResult:
        return self._readbacks(self._rows(self._select(""), self._mailbox_args()), None)

    def close(self) -> None:
        self._drop_connection()


def _text(value: Any) -> Optional[str]:
    """A column value as text. MySQL hands back str for text columns and
    bytes for a binary collation; None stays None, so an empty column reads
    as not recorded rather than as an empty string that matched."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)
