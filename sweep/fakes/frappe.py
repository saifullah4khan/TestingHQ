"""A stand-in for Frappe Helpdesk: mail in over SMTP, records out over its REST API.

Standard library only. It plays the part of the whole pull-mail chain in
sweep/compose/frappe.yml (Mailpit, the Email Account that polls it, and the
helpdesk) at the two edges TestingHQ touches.

SMTP. A message accepted after DATA is handled at once the way Helpdesk's
Email Account (`append_to = HD Ticket`) handles a pulled one:

  - machine mail is parked, as Helpdesk's override parks it, and makes no
    record: an X-Auto-Generated header, a multipart/report delivery-status
    report, `Return-Path: <>`, or Auto-Submitted other than `no` and
    `auto-replied`;
  - a Message-ID already held is the same message again, and is not stored
    twice (Frappe's `is_exist_in_system`);
  - otherwise it is stored as a Communication (`sent_or_received` Received,
    `subject` the first 140 characters of the Subject, `sender` the Reply-To
    address when there is one and the From address otherwise, lowercased,
    `content` the HTML part when there is one and the plain text otherwise,
    `text_content` the plain text, `message_id` without angle brackets,
    `email_account`), linked to an HD Ticket through `reference_name`: the
    ticket of the message its In-Reply-To or References names when that
    message is held, a new ticket otherwise (with the same subject,
    `raised_by` the sender, `description` the content).

Frappe also joins a message to a recent ticket with the same subject from the
same sender; the fake leaves that rule out, so distinct messages that share a
subject open distinct tickets here.

HTTP. The REST calls the readback and the setup script make, in Frappe's
shapes:

    GET /api/method/ping                          {"message": "pong"}, no auth
    GET /api/method/frappe.auth.get_logged_user   {"message": "Administrator"}
    GET /api/resource/<doctype>                   {"data": [...]}
    GET /api/resource/<doctype>/<name>            {"data": {...}}

for the doctypes HD Ticket and Communication. A list honours `fields` (JSON;
`name` alone when absent, `*` for every field), `filters` and `or_filters`
(JSON lists of [field, operator, value] or [doctype, field, operator, value],
or a dict), `limit_page_length` (or `limit`; 20 when absent, 0 for no limit),
`limit_start` and `order_by`. Operators: =, !=, like, not like, in, not in,
is (set / not set). Comparisons ignore case, as MariaDB's utf8mb4_unicode_ci
collation does, and `like` takes the SQL wildcards % and _. A field the
doctype does not have is refused with 417.

Auth is Frappe's API key pair: `Authorization: token <key>:<secret>`, or
`Basic` with the base64 of `<key>:<secret>`. A request with no Authorization
header runs as Guest and is refused with 403 (PermissionError); a key pair
the server does not know is refused with 401 (AuthenticationError).

    with FakeFrappeHelpdesk() as helpdesk:
        helpdesk.smtp_port      # [targets.frappe] port
        helpdesk.url            # readback url, scheme to port
        helpdesk.authorization  # FRAPPE_API_AUTH

    python sweep/fakes/frappe.py --port 3500 --smtp-port 3525
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import socketserver
import sys
import threading
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

TICKET = "HD Ticket"
COMMUNICATION = "Communication"
DEFAULT_EMAIL_ACCOUNT = "Sweep Support"
DEFAULT_API_KEY = "sweepkey0000001"
DEFAULT_API_SECRET = "sweepsecret0001"
DEFAULT_PAGE_LENGTH = 20
SUBJECT_LIMIT = 140

#: The fields this fake fills in, per doctype. A list request may ask for any
#: of them; anything else is refused, as an unknown column is.
FIELDS: Dict[str, Tuple[str, ...]] = {
    TICKET: (
        "name", "subject", "raised_by", "description", "status", "priority",
        "ticket_type", "agent_group", "email_account", "via_customer_portal",
        "opening_date", "opening_time", "creation", "modified", "owner",
    ),
    COMMUNICATION: (
        "name", "subject", "communication_medium", "communication_type",
        "sender", "sender_full_name", "recipients", "content", "text_content",
        "sent_or_received", "status", "reference_doctype", "reference_name",
        "email_account", "in_reply_to", "message_id", "has_attachment",
        "communication_date", "creation", "modified", "owner",
    ),
}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _part_text(part: Any) -> str:
    try:
        return part.get_content()
    except (LookupError, KeyError, ValueError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


_MESSAGE_ID = re.compile(r"<([^<>]+)>")


def message_ids(value: Any) -> List[str]:
    """The bare Message-IDs in a header value, in order."""
    text = str(value or "")
    found = _MESSAGE_ID.findall(text) or text.split()
    return [item.strip() for item in found if item.strip()]


def auto_generated_reason(message: EmailMessage) -> Optional[str]:
    """Why Helpdesk parks this message instead of opening a ticket, or None.
    The same tests, in the same order, as helpdesk.overrides.email_account."""
    if message.get("X-Auto-Generated"):
        return "X-Auto-Generated"
    if (message.get_content_type() == "multipart/report"
            and message.get_param("report-type") == "delivery-status"):
        return "delivery status notification"
    if str(message.get("Return-Path") or "").strip() == "<>":
        return "null return-path"
    submitted = str(message.get("Auto-Submitted") or "no").split(";")[0].strip().lower()
    if submitted not in ("no", "auto-replied"):
        return f"Auto-Submitted: {submitted}"
    return None


def parse_message(data: bytes) -> Dict[str, Any]:
    """What the Email Account pull reads out of one raw message."""
    message = message_from_bytes(data, policy=default_policy)
    # Frappe's set_from: X-Original-From over From, and Reply-To over both
    # (unless Reply-To is one of its own incoming accounts), lowercased.
    from_header = str(message["X-Original-From"] or message["From"] or "")
    real_name, address = parseaddr(from_header)
    reply_to = parseaddr(str(message["Reply-To"] or ""))[1]
    if reply_to:
        address = reply_to
    subject = str(message["Subject"] or "").strip()
    html_part = message.get_body(preferencelist=("html",))
    text_part = message.get_body(preferencelist=("plain",))
    text = _part_text(text_part) if text_part is not None else ""
    if html_part is not None:
        content = _part_text(html_part)
    elif text_part is not None:
        content = text
    else:
        content = "[Blank Email]"
    own = message_ids(message["Message-ID"])
    return {
        "subject": subject[:SUBJECT_LIMIT] if subject else "No Subject",
        "sender": address.lower(),
        "sender_full_name": real_name or None,
        "recipients": str(message["To"] or "") or None,
        "content": content,
        "text_content": text,
        "message_id": own[0] if own else None,
        "parents": message_ids(message["In-Reply-To"])
        + list(reversed(message_ids(message["References"]))),
        "parked": auto_generated_reason(message),
    }


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


class QueryError(ValueError):
    """A list request Frappe would refuse."""


def _fold(value: Any) -> Optional[str]:
    """A value as the database compares it: text, ignoring case. A number
    compares as its digits, so `name = "12"` finds ticket 12."""
    return None if value is None else str(value).casefold()


def _like(pattern: Any, value: Any) -> bool:
    if value is None:
        return False
    regex = "".join(
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in str(pattern)
    )
    return re.fullmatch(regex, str(value), re.IGNORECASE | re.DOTALL) is not None


def _matches(row: Dict[str, Any], condition: Tuple[str, str, Any]) -> bool:
    field, operator, wanted = condition
    value = row.get(field)
    operator = operator.strip().lower()
    if operator in ("=", "=="):
        return _fold(value) == _fold(wanted)
    if operator == "!=":
        return _fold(value) != _fold(wanted)
    if operator == "like":
        return _like(wanted, value)
    if operator == "not like":
        return not _like(wanted, value)
    if operator in ("in", "not in"):
        options = wanted if isinstance(wanted, list) else str(wanted).split(",")
        found = _fold(value) in {_fold(str(o).strip()) for o in options}
        return found if operator == "in" else not found
    if operator == "is" and str(wanted).lower() in ("set", "not set"):
        is_set = value not in (None, "")
        return is_set if str(wanted).lower() == "set" else not is_set
    raise QueryError(f"operator {operator!r} is not supported")


def _known(doctype: str, field: str) -> str:
    if field not in FIELDS[doctype]:
        raise QueryError(f"Field not permitted in query: {field}")
    return field


def parse_conditions(doctype: str, raw: Optional[str], what: str) -> List[Tuple[str, str, Any]]:
    """`filters` or `or_filters` as (field, operator, value) triples."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QueryError(f"{what} is not valid JSON: {exc}") from exc
    conditions: List[Tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for field, wanted in value.items():
            if isinstance(wanted, list) and len(wanted) == 2:
                conditions.append((field, str(wanted[0]), wanted[1]))
            else:
                conditions.append((field, "=", wanted))
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, list) or len(item) not in (3, 4):
                raise QueryError(f"{what} entry {item!r} is not [field, operator, value]")
            if len(item) == 4:
                if item[0] != doctype:
                    raise QueryError(f"{what} entry {item!r} names another doctype")
                item = item[1:]
            conditions.append((str(item[0]), str(item[1]), item[2]))
    else:
        raise QueryError(f"{what} must be a list or an object")
    for field, _operator, _wanted in conditions:
        _known(doctype, field)
    return conditions


def parse_fields(doctype: str, raw: Optional[str]) -> List[str]:
    if not raw:
        return ["name"]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = [part.strip() for part in raw.split(",")]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(f, str) for f in value):
        raise QueryError("fields must be a JSON list of field names")
    if "*" in value:
        return list(FIELDS[doctype])
    return [_known(doctype, field) for field in value]


def _int(raw: Optional[str], default: int, what: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise QueryError(f"{what} must be an integer, got {raw!r}") from exc


def list_rows(doctype: str, rows: List[Dict[str, Any]], query: Dict[str, str]) -> List[Dict[str, Any]]:
    """What GET /api/resource/<doctype> answers under `data` for `query`.
    `rows` are in insertion order."""
    fields = parse_fields(doctype, query.get("fields"))
    conditions = parse_conditions(doctype, query.get("filters"), "filters")
    alternatives = parse_conditions(doctype, query.get("or_filters"), "or_filters")
    length = _int(query.get("limit_page_length", query.get("limit")),
                  DEFAULT_PAGE_LENGTH, "limit_page_length")
    start = _int(query.get("limit_start"), 0, "limit_start")

    order = (query.get("order_by") or "modified desc").strip().split()
    order_field = _known(doctype, order[0].rsplit(".", 1)[-1].strip("`"))
    descending = len(order) < 2 or order[1].lower() != "asc"

    chosen = [
        (position, row) for position, row in enumerate(rows)
        if all(_matches(row, c) for c in conditions)
        and (not alternatives or any(_matches(row, c) for c in alternatives))
    ]
    def key(pair: Tuple[int, Dict[str, Any]]) -> Tuple[Any, ...]:
        value = pair[1].get(order_field)
        if isinstance(value, (int, float)):
            return (1, value, "", pair[0])
        return (0 if value is None else 2, 0, str(value or ""), pair[0])

    chosen.sort(key=key, reverse=descending)
    picked = [row for _position, row in chosen]
    picked = picked[start:start + length] if length > 0 else picked[start:]
    return [{field: row.get(field) for field in fields} for row in picked]


# ---------------------------------------------------------------------------
# The servers
# ---------------------------------------------------------------------------


class _SmtpHandler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self) -> None:
        helpdesk: FakeFrappeHelpdesk = self.server.helpdesk  # type: ignore[attr-defined]
        self.reply("220 frappe-fake.localhost ESMTP")
        mail_from = False
        recipients = 0
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            verb = raw.decode("utf-8", "replace").strip().split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250 frappe-fake.localhost")
            elif verb == "MAIL":
                mail_from, recipients = True, 0
                self.reply("250 2.1.0 Ok")
            elif verb == "RCPT":
                recipients += 1
                self.reply("250 2.1.5 Ok")
            elif verb == "DATA":
                if not mail_from or not recipients:
                    self.reply("503 5.5.1 need MAIL and RCPT first")
                    continue
                self.reply("354 end with <CRLF>.<CRLF>")
                lines = []
                while True:
                    line = self.rfile.readline()
                    if not line or line == b".\r\n":
                        break
                    lines.append(line[1:] if line.startswith(b"..") else line)
                outcome = helpdesk.receive(b"".join(lines))
                self.reply(f"250 2.0.0 Ok: {outcome}")
                mail_from, recipients = False, 0
            elif verb == "RSET":
                mail_from, recipients = False, 0
                self.reply("250 2.0.0 Ok")
            elif verb == "NOOP":
                self.reply("250 2.0.0 Ok")
            elif verb == "QUIT":
                self.reply("221 2.0.0 Bye")
                return
            else:
                self.reply("502 5.5.2 command not implemented")


def _error(exc_type: str, message: str) -> Dict[str, Any]:
    return {
        "exc_type": exc_type,
        "exception": f"frappe.exceptions.{exc_type}: {message}",
        "_server_messages": json.dumps([json.dumps({"message": message})]),
    }


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = "frappe-fake"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.helpdesk.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        helpdesk: FakeFrappeHelpdesk = self.server.helpdesk  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        authorization = self.headers.get("Authorization")
        helpdesk.record_request("GET", path, query, authorization)

        if path == "/api/method/ping":
            return self._send(200, {"message": "pong"})

        user = helpdesk.authenticate(authorization)
        if user is None:
            return self._send(401, _error("AuthenticationError", "Authentication failed"))

        if path == "/api/method/frappe.auth.get_logged_user":
            if user == "Guest":
                return self._send(403, _error("PermissionError", "Not permitted"))
            return self._send(200, {"message": user})

        if not path.startswith("/api/resource/"):
            return self._send(404, _error("PageDoesNotExistError", "Page not found"))
        doctype, _, name = path[len("/api/resource/"):].partition("/")
        if doctype not in FIELDS:
            return self._send(404, _error("DoesNotExistError", f"DocType {doctype} not found"))
        if user == "Guest":
            return self._send(403, _error("PermissionError", f"No permission for {doctype}"))
        rows = helpdesk.snapshot(doctype)
        if not name:
            try:
                return self._send(200, {"data": list_rows(doctype, rows, query)})
            except QueryError as exc:
                return self._send(417, _error("DataError", str(exc)))
        for row in rows:
            if str(row["name"]) == name:
                return self._send(200, {"data": dict(row, doctype=doctype)})
        return self._send(404, _error("DoesNotExistError", f"{doctype} {name} not found"))


class FakeFrappeHelpdesk:
    """Both servers on 127.0.0.1, in background threads.

    `tickets` and `communications` are what the helpdesk holds, oldest first;
    `parked` the messages set aside as machine mail, as (reason, raw bytes);
    `messages` the raw bytes of every message accepted over SMTP; `requests`
    every HTTP request as (method, path, query, Authorization header or None).
    `welcome_ticket` adds the "Welcome to Helpdesk" ticket, and its
    Communication, that a fresh install creates outside any Email Account.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, smtp_port: int = 0,
                 api_key: str = DEFAULT_API_KEY, api_secret: str = DEFAULT_API_SECRET,
                 email_account: str = DEFAULT_EMAIL_ACCOUNT, welcome_ticket: bool = True,
                 verbose: bool = False) -> None:
        self.host = host
        self._requested_ports = (port, smtp_port)
        self.api_key = api_key
        self.api_secret = api_secret
        self.email_account = email_account
        self.verbose = verbose
        self.tickets: List[Dict[str, Any]] = []
        self.communications: List[Dict[str, Any]] = []
        self.parked: List[Tuple[str, bytes]] = []
        self.messages: List[bytes] = []
        self.requests: List[Tuple[str, str, Dict[str, str], Optional[str]]] = []
        self._lock = threading.Lock()
        self._http: Optional[ThreadingHTTPServer] = None
        self._smtp: Optional[socketserver.ThreadingTCPServer] = None
        if welcome_ticket:
            welcome = "<p>Hi there, welcome to Helpdesk.</p>"
            name = self._open_ticket(
                subject="Welcome to Helpdesk", raised_by="welcome@helpdesk.example.test",
                description=welcome, email_account=None, via_customer_portal=1,
            )
            self._store_communication(name, {
                "subject": "Welcome to Helpdesk", "sender": "welcome@helpdesk.example.test",
                "sender_full_name": None, "recipients": None, "content": welcome,
                "text_content": "Hi there, welcome to Helpdesk.", "message_id": None,
            }, email_account=None, in_reply_to=None)

    # -- auth -------------------------------------------------------------

    @property
    def authorization(self) -> str:
        """The Authorization value this fake accepts: FRAPPE_API_AUTH."""
        return f"token {self.api_key}:{self.api_secret}"

    def authenticate(self, header: Optional[str]) -> Optional[str]:
        """The user a request runs as: Guest without a header, Administrator
        for the right key pair, None (refused) for anything else."""
        if not header:
            return "Guest"
        scheme, _, credentials = header.strip().partition(" ")
        credentials = credentials.strip()
        if scheme.lower() == "basic":
            try:
                credentials = base64.b64decode(credentials, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
        elif scheme.lower() != "token":
            return None
        key, _, secret = credentials.partition(":")
        if key == self.api_key and secret == self.api_secret:
            return "Administrator"
        return None

    # -- mail -------------------------------------------------------------

    def _open_ticket(self, *, subject: str, raised_by: str, description: str,
                     email_account: Optional[str], via_customer_portal: int = 0) -> int:
        name = len(self.tickets) + 1
        stamp = _stamp()
        self.tickets.append({
            "name": name,
            "subject": subject,
            "raised_by": raised_by,
            "description": description,
            "status": "Open",
            "priority": "Medium",
            "ticket_type": "Unspecified",
            "agent_group": None,
            "email_account": email_account,
            "via_customer_portal": via_customer_portal,
            "opening_date": stamp[:10],
            "opening_time": stamp[11:],
            "creation": stamp,
            "modified": stamp,
            "owner": "Administrator",
        })
        return name

    def _store_communication(self, ticket: int, mail: Dict[str, Any], *,
                             email_account: Optional[str],
                             in_reply_to: Optional[str]) -> Dict[str, Any]:
        stamp = _stamp()
        record = {
            "name": f"{len(self.communications) + 1:010x}",
            "subject": mail["subject"],
            "communication_medium": "Email",
            "communication_type": "Communication",
            "sender": mail["sender"],
            "sender_full_name": mail["sender_full_name"],
            "recipients": mail["recipients"],
            "content": mail["content"],
            "text_content": mail["text_content"],
            "sent_or_received": "Received",
            "status": "Linked",
            "reference_doctype": TICKET,
            "reference_name": str(ticket),
            "email_account": email_account,
            "in_reply_to": in_reply_to,
            "message_id": mail["message_id"],
            "has_attachment": 0,
            "communication_date": stamp,
            "creation": stamp,
            "modified": stamp,
            "owner": "Administrator",
        }
        self.communications.append(record)
        return record

    def receive(self, data: bytes) -> str:
        """Take one raw message the way the Email Account pull does. Returns
        what became of it: the ticket it is on, or why it was set aside."""
        mail = parse_message(data)
        with self._lock:
            self.messages.append(data)
            if mail["parked"]:
                self.parked.append((mail["parked"], data))
                return f"parked ({mail['parked']})"
            held = {c["message_id"]: c for c in self.communications if c["message_id"]}
            if mail["message_id"] in held:
                return f"already on ticket {held[mail['message_id']]['reference_name']}"
            parent = next((held[p] for p in mail["parents"] if p in held), None)
            if parent is not None:
                ticket = int(parent["reference_name"])
            else:
                ticket = self._open_ticket(
                    subject=mail["subject"], raised_by=mail["sender"],
                    description=mail["content"], email_account=self.email_account,
                )
            self._store_communication(
                ticket, mail, email_account=self.email_account,
                in_reply_to=parent["name"] if parent is not None else None,
            )
        return f"ticket {ticket}"

    def record_request(self, method: str, path: str, query: Dict[str, str],
                       authorization: Optional[str]) -> None:
        with self._lock:
            self.requests.append((method, path, query, authorization))

    def snapshot(self, doctype: str = TICKET) -> List[Dict[str, Any]]:
        """A copy of every record of `doctype`, oldest first."""
        with self._lock:
            rows = self.tickets if doctype == TICKET else self.communications
            return [dict(row) for row in rows]

    # -- lifecycle --------------------------------------------------------

    @property
    def port(self) -> int:
        assert self._http is not None, "the fake is not running"
        return self._http.server_address[1]

    @property
    def smtp_port(self) -> int:
        assert self._smtp is not None, "the fake is not running"
        return self._smtp.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FakeFrappeHelpdesk":
        http_port, smtp_port = self._requested_ports

        class Http(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Smtp(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._http = Http((self.host, http_port), _HttpHandler)
        self._http.helpdesk = self  # type: ignore[attr-defined]
        self._smtp = Smtp((self.host, smtp_port), _SmtpHandler)
        self._smtp.helpdesk = self  # type: ignore[attr-defined]
        for server in (self._http, self._smtp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        for server in (self._http, self._smtp):
            if server is not None:
                server.shutdown()
                server.server_close()

    def __enter__(self) -> "FakeFrappeHelpdesk":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3500, help="HTTP port (REST API)")
    parser.add_argument("--smtp-port", type=int, default=3525, help="SMTP port (mail in)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--api-secret", default=DEFAULT_API_SECRET)
    parser.add_argument("--email-account", default=DEFAULT_EMAIL_ACCOUNT)
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    args = parser.parse_args(argv)
    helpdesk = FakeFrappeHelpdesk(
        host=args.host, port=args.port, smtp_port=args.smtp_port,
        api_key=args.api_key, api_secret=args.api_secret,
        email_account=args.email_account, verbose=args.verbose,
    )
    helpdesk.start()
    print(f"HTTP: {helpdesk.url}", flush=True)
    print(f"SMTP: {helpdesk.host}:{helpdesk.smtp_port}", flush=True)
    print(f"FRAPPE_API_AUTH={helpdesk.authorization}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        helpdesk.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
