"""Readback adapter for osTicket, reading its MySQL tables.

osTicket has no API to read tickets back (its API creates tickets and runs
cron, nothing else), so this adapter reads the database osTicket writes to.
It is an example of a custom `module:attribute` adapter that brings its own
dependency: PyMySQL, installed next to TestingHQ, never a dependency of
TestingHQ itself.

    pip install PyMySQL
    export OSTICKET_MYSQL_DSN="mysql://reader:password@127.0.0.1:3306/osticket"

and in the target config, with the repository root on PYTHONPATH:

    [readback]
    kind = "examples.readback.adapters.osticket_mysql:OsTicketMysqlAdapter"
    dsn = "env:OSTICKET_MYSQL_DSN"

THE TABLES, from osTicket's setup/inc/streams/core/install-mysql.sql (1.18),
with the default `ost_` prefix:

    ost_thread           one per ticket: object_type 'T', object_id = ticket
    ost_thread_entry     one per message: type 'M', title, body, format, user_id
    ost_thread_entry_email   the Message-ID (`mid`) and raw headers of an entry
    ost_ticket           ticket_id, number (what the API returns), user_id, dept_id
    ost_ticket__cdata    the ticket form's answers: subject. Built by the
                         installer from the ticket form, not by install-mysql.sql
    ost_user, ost_user_email   the sender: user.default_email_id -> address
    ost_department       the department the ticket was filed under (route)
    ost_attachment, ost_file   attachment names of an entry (type 'H')

HOW A MESSAGE IS FOUND. TestingHQ never puts the tag in the subject (see
testinghq/pipeline/messages.py); it stamps `[testinghq:<tag>]` into the text
and HTML bodies and derives the Message-ID from it. So a lookup is a message
entry whose body carries the marker, or whose stored Message-ID is the one
derived from the tag. One record per matching entry: a message osTicket filed
twice, as two tickets or as two entries on one ticket, comes back twice, which
is the duplicate the ledger and redeliver tools count.

WHAT EACH FIELD IS.

    ticket_id     ost_ticket.number
    subject       the entry's title (osTicket stores the message subject
                  there), else ost_ticket__cdata.subject
    from_addr     the address of the entry's user, else of the ticket's user
    body          the entry's body, as stored: HTML when osTicket's rich text
                  setting is on (the default), which the body check strips
    attachment_names  ost_attachment names on the entry
    route         ost_department.name
    message_id, in_reply_to, references   ost_thread_entry_email
    tag           harvested from the stored headers, then the body

THE SNAPSHOT TRAP. MySQL's default isolation level is REPEATABLE READ: a
connection that keeps a transaction open sees the database as it was at its
first read, forever, and a readback polling through it would never see a
ticket created after that. The connection is opened with autocommit on and
every read ends its transaction, so each poll sees what osTicket has written.

THE DSN is an `env:` reference, never a literal, by the same rule as
`[readback.headers]`: `mysql://user:password@host:port/database`, with the
user and password percent-encoded if they hold reserved characters. Use a
database user with SELECT only: this adapter never writes, and a reader that
cannot write cannot damage a ticket store.

PyMySQL is imported only when the adapter connects, so this module imports,
and a config naming it loads and validates, without PyMySQL installed. A
missing PyMySQL is a refusal at preflight, before anything is sent.
`connect=` takes any DB-API connection factory instead, which is how the tests
run this adapter against a SQLite copy of the same tables.
"""
from __future__ import annotations

import email.parser
import email.policy
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from testinghq.core.config import env_reference_problem, resolve_env_references
from testinghq.pipeline.messages import build_chain_message_id, harvest_tag, tag_marker
from testinghq.pipeline.readback import FetchResult, Readback, ReadbackError

DEFAULT_TABLE_PREFIX = "ost_"
DEFAULT_PORT = 3306
DEFAULT_LIST_LIMIT = 10_000
DEFAULT_TIMEOUT = 10.0

#: osTicket's thread object type for a ticket, and thread entry type for a
#: message from the user (as opposed to 'R' a response or 'N' a note).
OBJECT_TYPE_TICKET = "T"
ENTRY_TYPE_MESSAGE = "M"
#: ost_attachment.type for a file attached to a thread entry.
ATTACHMENT_TYPE_ENTRY = "H"

#: The LIKE escape character. Not a backslash: MySQL reads '\\' in a literal
#: as one backslash and SQLite as two, so the same SQL would mean different
#: things. '!' means the same in both.
_LIKE_ESCAPE = "!"

_PREFIX_SHAPE = re.compile(r"^[A-Za-z0-9_]*$")
_DSN_SCHEMES = ("mysql", "mysql+pymysql")

#: The tables every lookup reads, checked to exist at preflight.
_TABLES = (
    "ticket", "ticket__cdata", "thread", "thread_entry", "thread_entry_email",
    "user", "user_email", "department", "attachment", "file",
)


class OsTicketReadbackError(ReadbackError):
    """A refusal from this adapter: bad config, no PyMySQL, or a database it
    cannot read."""


@dataclass(frozen=True)
class Dsn:
    """A parsed `mysql://` DSN. `password` is kept out of repr."""

    host: str
    port: int
    user: str
    database: str
    password: str = ""
    options: Tuple[Tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        return (
            f"Dsn(host={self.host!r}, port={self.port}, user={self.user!r}, "
            f"database={self.database!r})"
        )


def parse_dsn(value: str) -> Dsn:
    """Parse `mysql://user:password@host:port/database[?option=value]`."""
    parts = urlsplit(value)
    if parts.scheme not in _DSN_SCHEMES:
        raise OsTicketReadbackError(
            f"the osTicket DSN must start with mysql://, got scheme {parts.scheme!r}"
        )
    if not parts.hostname:
        raise OsTicketReadbackError("the osTicket DSN names no host")
    database = unquote(parts.path.lstrip("/"))
    if not database or "/" in database:
        raise OsTicketReadbackError(
            "the osTicket DSN must name one database, as mysql://user@host/osticket"
        )
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as exc:
        raise OsTicketReadbackError(f"the osTicket DSN has a bad port: {exc}") from None
    return Dsn(
        host=parts.hostname,
        port=port,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=database,
        options=tuple(parse_qsl(parts.query)),
    )


def pymysql_connect(dsn: Dsn, timeout: float) -> Any:
    """Open a PyMySQL connection, importing PyMySQL here and only here."""
    try:
        import pymysql
    except ImportError:
        raise OsTicketReadbackError(
            "the osTicket readback adapter needs PyMySQL: pip install PyMySQL. "
            "It is a dependency of this example adapter, not of TestingHQ."
        ) from None
    options = dict(dsn.options)
    return pymysql.connect(
        host=dsn.host,
        port=dsn.port,
        user=dsn.user,
        password=dsn.password,
        database=dsn.database,
        charset=options.get("charset", "utf8mb4"),
        connect_timeout=timeout,
        read_timeout=timeout,
        autocommit=True,
    )


#: The DB-API paramstyle PyMySQL accepts (it formats `%s`).
pymysql_connect.paramstyle = "format"

#: The factory used when none is passed. A module attribute so a test running
#: the real CLI can point it at a stand-in database.
default_connect: Callable[[Dsn, float], Any] = pymysql_connect


def _table(raw: Mapping[str, Any], key: str, default: Any, kind: type) -> Any:
    value = raw.get(key, default)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise OsTicketReadbackError(
            f"[readback].{key} must be a {kind.__name__}, got {type(value).__name__}"
        )
    return value


class OsTicketMysqlAdapter:
    """Reads tickets back from osTicket's MySQL tables. See the module
    docstring for the tables, the fields and the DSN."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        connect: Optional[Callable[[Dsn, float], Any]] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        if "dsn" not in config:
            raise OsTicketReadbackError(
                '[readback].dsn is required, as dsn = "env:OSTICKET_MYSQL_DSN"'
            )
        # The shape is checked now, so a literal DSN is refused when the
        # config loads; the variable is read at preflight, before sending.
        self._dsn_ref = {"dsn": config["dsn"]}
        problem = env_reference_problem("[readback].dsn", config["dsn"])
        if problem is not None:
            raise OsTicketReadbackError(problem)

        prefix = _table(config, "table_prefix", DEFAULT_TABLE_PREFIX, str)
        if not _PREFIX_SHAPE.match(prefix):
            raise OsTicketReadbackError(
                f"[readback].table_prefix = {prefix!r} may hold only letters, "
                "digits and underscores"
            )
        self.prefix = prefix
        self.list_limit = _table(config, "list_limit", DEFAULT_LIST_LIMIT, int)
        if self.list_limit < 1:
            raise OsTicketReadbackError("[readback].list_limit must be at least 1")
        timeout = config.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise OsTicketReadbackError("[readback].timeout must be a positive number")
        self.timeout = float(timeout)
        self.can_enumerate = _table(config, "enumerate", True, bool)

        self._connect = connect
        self._env = env
        self._dsn: Optional[Dsn] = None
        self._conn: Any = None
        self._placeholder = "%s"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _resolve_dsn(self) -> Dsn:
        if self._dsn is None:
            resolved = resolve_env_references(
                "[readback]", self._dsn_ref, self._env, error=OsTicketReadbackError
            )
            self._dsn = parse_dsn(resolved["dsn"])
        return self._dsn

    def _connection(self) -> Any:
        if self._conn is None:
            dsn = self._resolve_dsn()
            factory = self._connect or default_connect
            paramstyle = getattr(factory, "paramstyle", "format")
            self._placeholder = "?" if paramstyle == "qmark" else "%s"
            try:
                self._conn = factory(dsn, self.timeout)
            except ReadbackError:
                raise
            except Exception as exc:  # noqa: BLE001 - any driver error refuses
                raise OsTicketReadbackError(
                    f"could not connect to osTicket's database at "
                    f"{dsn.host}:{dsn.port}/{dsn.database} as {dsn.user!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self._conn

    def _execute(self, conn: Any, sql: str, params: Sequence[Any]) -> List[Tuple[Any, ...]]:
        cursor = conn.cursor()
        try:
            cursor.execute(sql.replace("{p}", self._placeholder), tuple(params))
            rows = [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
        # End the read transaction, so the next poll sees new rows.
        conn.rollback()
        return rows

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        """Run one SELECT and end its transaction, reconnecting once if the
        connection dropped between polls."""
        for attempt in (1, 2):
            conn = self._connection()
            try:
                return self._execute(conn, sql, params)
            except Exception as exc:  # noqa: BLE001 - driver errors vary
                self._drop()
                if attempt == 2:
                    raise OsTicketReadbackError(
                        f"reading osTicket's database failed: {type(exc).__name__}: {exc}"
                    ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already broken
                pass

    def _t(self, name: str) -> str:
        return f"{self.prefix}{name}"

    # ------------------------------------------------------------------
    # The adapter contract
    # ------------------------------------------------------------------

    def preflight(self) -> None:
        """Refuse before sending: the DSN variable unset, PyMySQL missing,
        the database unreachable, or a table missing (a wrong prefix)."""
        conn = self._connection()
        missing = []
        for name in _TABLES:
            try:
                self._execute(conn, f"SELECT 1 FROM {self._t(name)} WHERE 1 = 0", ())
            except Exception as exc:  # noqa: BLE001 - driver errors vary
                missing.append(f"{self._t(name)} ({type(exc).__name__}: {exc})")
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 - reported below
                    pass
        if missing:
            raise OsTicketReadbackError(
                f"osTicket's database cannot be read: {'; '.join(missing)}. Is "
                f"table_prefix ({self.prefix!r}) the prefix the install used, has "
                "osTicket finished installing, and may this user SELECT from it?"
            )

    def fetch(self, probe: Any) -> FetchResult:
        tag = probe.tag
        marker = _like_contains(tag_marker(tag))
        mid = _like_contains(build_chain_message_id(tag))
        probe_mid = getattr(probe, "message_id", "") or ""
        mids = [mid]
        if probe_mid and probe_mid != build_chain_message_id(tag):
            mids.append(_like_contains(probe_mid))
        mid_clause = " OR ".join(
            f"m.mid LIKE {{p}} ESCAPE '{_LIKE_ESCAPE}'" for _ in mids
        )
        where = (
            f"(e.body LIKE {{p}} ESCAPE '{_LIKE_ESCAPE}' OR e.id IN "
            f"(SELECT m.thread_entry_id FROM {self._t('thread_entry_email')} m "
            f"WHERE {mid_clause}))"
        )
        rows = self._query(self._entry_sql(where), [marker, *mids])
        return FetchResult([self._readback(row) for row in rows])

    def list_all(self) -> FetchResult:
        rows = self._query(
            self._entry_sql("1 = 1") + f" LIMIT {int(self.list_limit) + 1}"
        )
        partial = len(rows) > self.list_limit
        return FetchResult(
            [self._readback(row) for row in rows[: self.list_limit]], partial=partial
        )

    def close(self) -> None:
        self._drop()

    # ------------------------------------------------------------------
    # SQL and rows
    # ------------------------------------------------------------------

    def _entry_sql(self, where: str) -> str:
        t = self._t
        return (
            "SELECT e.id, e.title, e.body, e.format, t.number, c.subject, "
            "ue.address, d.name "
            f"FROM {t('thread_entry')} e "
            f"JOIN {t('thread')} th ON th.id = e.thread_id "
            f"AND th.object_type = '{OBJECT_TYPE_TICKET}' "
            f"JOIN {t('ticket')} t ON t.ticket_id = th.object_id "
            f"LEFT JOIN {t('ticket__cdata')} c ON c.ticket_id = t.ticket_id "
            f"LEFT JOIN {t('user')} u ON u.id = "
            "COALESCE(NULLIF(e.user_id, 0), t.user_id) "
            f"LEFT JOIN {t('user_email')} ue ON ue.id = u.default_email_id "
            f"LEFT JOIN {t('department')} d ON d.id = t.dept_id "
            f"WHERE e.type = '{ENTRY_TYPE_MESSAGE}' AND {where} "
            "ORDER BY e.id"
        )

    def _readback(self, row: Tuple[Any, ...]) -> Readback:
        entry_id, title, body, _format, number, ticket_subject, address, dept = row
        fields = ["ticket_id", "body"]
        values: Dict[str, Any] = {"ticket_id": _text(number), "body": _text(body)}

        subject = _text(title) or _text(ticket_subject)
        if subject is not None:
            fields.append("subject")
            values["subject"] = subject
        if _text(address):
            fields.append("from_addr")
            values["from_addr"] = _text(address)
        if _text(dept):
            fields.append("route")
            values["route"] = _text(dept)

        names = [
            _text(name)
            for (name,) in self._query(
                f"SELECT COALESCE(a.name, f.name) FROM {self._t('attachment')} a "
                f"LEFT JOIN {self._t('file')} f ON f.id = a.file_id "
                f"WHERE a.object_id = {{p}} AND a.type = '{ATTACHMENT_TYPE_ENTRY}' "
                "ORDER BY a.id",
                [entry_id],
            )
            if _text(name)
        ]
        fields.append("attachment_names")
        values["attachment_names"] = tuple(names)

        headers_text = None
        mail = self._query(
            f"SELECT mid, headers FROM {self._t('thread_entry_email')} "
            "WHERE thread_entry_id = {p} ORDER BY id",
            [entry_id],
        )
        if mail:
            mid, headers_text = _text(mail[0][0]), _text(mail[0][1])
            if mid:
                fields.append("message_id")
                values["message_id"] = mid.strip().strip("<>").strip()
            if headers_text:
                parsed = email.parser.HeaderParser(policy=email.policy.compat32).parsestr(
                    headers_text
                )
                fields += ["in_reply_to", "references"]
                in_reply_to = parsed.get("In-Reply-To")
                values["in_reply_to"] = (
                    str(in_reply_to).strip().strip("<>").strip() if in_reply_to else None
                )
                values["references"] = tuple(
                    token.strip("<>") for token in str(parsed.get("References") or "").split()
                )

        tag = harvest_tag(headers_text, _text(body))
        if tag:
            fields.append("tag")
            values["tag"] = tag
        return Readback(exists=True, fields=tuple(fields), **values)


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _like_contains(value: str) -> str:
    """A LIKE pattern matching `value` anywhere, with LIKE's own wildcards in
    it escaped, so a tag holding '_' matches only itself."""
    escaped = (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def build(config: Mapping[str, Any]) -> OsTicketMysqlAdapter:
    """Factory form, for `kind = "...osticket_mysql:build"`."""
    return OsTicketMysqlAdapter(config)
