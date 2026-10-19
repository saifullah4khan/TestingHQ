"""A stand-in for Zammad: mail in over SMTP, tickets out over Zammad's REST API.

Zammad pulls mail: an email channel fetches a mailbox (POP3 or IMAP) on a
schedule and opens a ticket per new message. The sweep runs it behind a Mailpit
that takes the SMTP and serves the POP3. This fake folds the three into one
process, standard library only, so a recipe can be tested end to end without a
container:

  SMTP      every accepted message lands in a mailbox. With `fetch_interval`
            set, a background fetch turns the mailbox into tickets on that
            period, the way the channel does; with 0 a message is a ticket as
            soon as the server has taken it.
  HTTP      the readback calls Zammad answers, in Zammad's JSON shape:

    GET /api/v1/tickets/search?query=<text>[&expand=true][&page=&per_page=]
    GET /api/v1/tickets[?expand=true][&page=&per_page=]
    GET /api/v1/tickets/<id>[?expand=true]
    GET /api/v1/users/me

Every API call needs `Authorization: Token token=<token>` (or the `Bearer`
spelling Rails accepts for the same token), and is answered 401 otherwise.

The search matches the way Zammad's database search does when no
Elasticsearch is configured (app/models/ticket/search.rb): `*` is dropped, and
the rest is a case-insensitive substring of the ticket's title or number, or of
any article's body, from, to or subject. Results are newest first, 50 to a page
by default and at most 200.

`expand=true` renders each ticket the way Zammad's
`attributes_with_association_names` does: every `*_id` column stays, and each
association gains its name next to it. A user association is the user's login,
which for a customer Zammad created from an email is the sender's address, in
lower case. The articles are not part of it, so the body needs a second request
(`GET /api/v1/ticket_articles/by_ticket/<id>`), which this fake does not serve.

    with ZammadFake(token="t0ken") as fake:
        ...  # SMTP to 127.0.0.1:fake.smtp_port, API at fake.url

    python sweep/fakes/zammad.py --port 3380 --smtp-port 3325 --token t0ken
"""
from __future__ import annotations

import argparse
import json
import socketserver
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

#: The group an email channel files its tickets under in a fresh Zammad.
DEFAULT_GROUP = "Users"
#: Zammad's first ticket number on a system whose counter starts fresh.
FIRST_TICKET_NUMBER = 31001
#: `paginate_with(max: 200, default: 50)` on the search, `max: 100` on the index.
SEARCH_PER_PAGE = 50
SEARCH_PER_PAGE_MAX = 200
INDEX_PER_PAGE_MAX = 100

ADMIN_LOGIN = "admin@zammad.test"
_ADMIN_ID = 3
#: The login of user 1, the system user Zammad owns unassigned tickets with.
_SYSTEM_LOGIN = "-"


def _now() -> str:
    stamp = datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


@dataclass
class Article:
    id: int
    from_addr: str
    to: str
    subject: str
    body: str
    content_type: str
    message_id: Optional[str]


@dataclass
class Ticket:
    id: int
    number: str
    title: str
    customer_id: int
    customer_login: str
    group: str
    created_at: str
    articles: List[Article] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        needle = query.replace("*", "").casefold()
        if not needle:
            return True
        haystacks = [self.title, self.number]
        for article in self.articles:
            haystacks += [article.body, article.from_addr, article.to, article.subject]
        return any(needle in (text or "").casefold() for text in haystacks)

    def attributes(self) -> Dict[str, Any]:
        """`attributes_with_association_ids`: the ticket row plus `*_ids` lists."""
        return {
            "id": self.id,
            "group_id": 1,
            "priority_id": 2,
            "state_id": 1,
            "organization_id": None,
            "number": self.number,
            "title": self.title,
            "owner_id": 1,
            "customer_id": self.customer_id,
            "note": None,
            "first_response_at": None,
            "first_response_escalation_at": None,
            "first_response_in_min": None,
            "first_response_diff_in_min": None,
            "close_at": None,
            "close_escalation_at": None,
            "close_in_min": None,
            "close_diff_in_min": None,
            "update_escalation_at": None,
            "update_in_min": None,
            "update_diff_in_min": None,
            "last_close_at": None,
            "last_contact_at": self.created_at,
            "last_contact_agent_at": None,
            "last_contact_customer_at": self.created_at,
            "last_owner_update_at": None,
            "create_article_type_id": 1,
            "create_article_sender_id": 2,
            "article_count": len(self.articles),
            "escalation_at": None,
            "pending_time": None,
            "type": None,
            "time_unit": None,
            "preferences": {"channel_id": 3},
            "updated_by_id": self.customer_id,
            "created_by_id": self.customer_id,
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "article_ids": [article.id for article in self.articles],
            "ticket_time_accounting_ids": [],
        }

    def expanded(self) -> Dict[str, Any]:
        """`attributes_with_association_names`: the same, plus the names."""
        data = self.attributes()
        data.update({
            "group": self.group,
            "state": "new",
            "priority": "2 normal",
            "owner": _SYSTEM_LOGIN,
            "customer": self.customer_login,
            "created_by": self.customer_login,
            "updated_by": self.customer_login,
            "create_article_type": "email",
            "create_article_sender": "Customer",
        })
        return data


@dataclass(frozen=True)
class Received:
    envelope_from: str
    envelope_to: Tuple[str, ...]
    data: bytes
    peer: str


def _smtp_address(argument: str) -> str:
    """`FROM:<a@b.test> SIZE=10` -> `a@b.test`."""
    _, _, rest = argument.partition(":")
    return rest.strip().split(" ", 1)[0].strip("<>")


class _SmtpHandler(socketserver.StreamRequestHandler):
    """Enough of RFC 5321 to take a message the way Mailpit does."""

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self) -> None:
        fake: ZammadFake = self.server.fake  # type: ignore[attr-defined]
        self.reply("220 zammad-fake.localhost ESMTP")
        sender: Optional[str] = None
        recipients: List[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            verb, _, argument = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250 zammad-fake.localhost")
            elif verb == "MAIL":
                sender, recipients = _smtp_address(argument), []
                self.reply("250 2.1.0 Ok")
            elif verb == "RCPT":
                recipients.append(_smtp_address(argument))
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
                index = fake.accept(Received(
                    sender, tuple(recipients), b"".join(lines), self.client_address[0],
                ))
                self.reply(f"250 2.0.0 Ok: queued as {index}")
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


def _bearer(header: Optional[str]) -> Optional[str]:
    """The token in `Token token=abc`, `Token token="abc"`, `Token abc` or
    `Bearer abc`, the spellings Rails' `authenticate_with_http_token` reads."""
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() not in ("token", "bearer"):
        return None
    rest = rest.strip()
    first = rest.split(",", 1)[0].strip()
    if first.startswith("token="):
        first = first[len("token="):]
    return first.strip('"') or None


def _page(params: Dict[str, List[str]], default: int, maximum: int) -> Tuple[int, int]:
    def number(name: str, fallback: int) -> int:
        try:
            return max(1, int(params.get(name, [str(fallback)])[0]))
        except ValueError:
            return fallback

    return number("page", 1), min(number("per_page", default), maximum)


def _flag(params: Dict[str, List[str]], name: str) -> bool:
    return params.get(name, ["false"])[0].lower() in ("true", "1", "yes")


class _ApiHandler(BaseHTTPRequestHandler):
    server_version = "zammad-fake"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        fake: ZammadFake = self.server.fake  # type: ignore[attr-defined]
        authorization = self.headers.get("Authorization")
        split = urlsplit(self.path)
        fake.record_request("GET", self.path, authorization)
        if _bearer(authorization) != fake.token:
            self._send(401, {
                "error": "Authentication required",
                "error_human": "Authentication required",
            })
            return
        params = parse_qs(split.query)
        expand = _flag(params, "expand")
        path = split.path.rstrip("/")
        if path == "/api/v1/tickets/search":
            query = (params.get("query") or params.get("term") or [""])[0]
            page, per_page = _page(params, SEARCH_PER_PAGE, SEARCH_PER_PAGE_MAX)
            found = [t for t in reversed(fake.ticket_list()) if t.matches(query)]
            chunk = found[(page - 1) * per_page: page * per_page]
            records = [t.expanded() if expand else t.attributes() for t in chunk]
            if _flag(params, "with_total_count"):
                self._send(200, {"records": records, "total_count": len(found)})
            else:
                self._send(200, records)
        elif path == "/api/v1/tickets":
            page, per_page = _page(params, INDEX_PER_PAGE_MAX, INDEX_PER_PAGE_MAX)
            chunk = fake.ticket_list()[(page - 1) * per_page: page * per_page]
            self._send(200, [t.expanded() if expand else t.attributes() for t in chunk])
        elif path.startswith("/api/v1/tickets/") and path.rsplit("/", 1)[1].isdigit():
            wanted = int(path.rsplit("/", 1)[1])
            ticket = next((t for t in fake.ticket_list() if t.id == wanted), None)
            if ticket is None:
                self._send(404, {"error": "Couldn't find Ticket with 'id'=%d" % wanted})
            else:
                self._send(200, ticket.expanded() if expand else ticket.attributes())
        elif path == "/api/v1/users/me":
            self._send(200, {
                "id": _ADMIN_ID, "login": ADMIN_LOGIN, "email": ADMIN_LOGIN,
                "firstname": "Sweep", "lastname": "Admin", "active": True,
                "role_ids": [1, 2], "group_ids": {"1": ["full"]},
            })
        else:
            self._send(404, {"error": "No route matches [GET] %s" % split.path})


class ZammadFake:
    """SMTP and the REST API on 127.0.0.1, each on its own port, in threads."""

    def __init__(
        self,
        token: str = "zammad-fake-token",
        *,
        port: int = 0,
        smtp_port: int = 0,
        fetch_interval: float = 0.0,
        group: str = DEFAULT_GROUP,
    ) -> None:
        self.token = token
        self.group = group
        self.fetch_interval = fetch_interval
        self.received: List[Received] = []
        self.requests: List[Tuple[str, str, Optional[str]]] = []
        self.fetches = 0
        self._wanted_ports = (port, smtp_port)
        self._mailbox: List[Received] = []
        self._tickets: List[Ticket] = []
        self._users: Dict[str, int] = {}
        self._article_ids = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._smtp: Optional[socketserver.ThreadingTCPServer] = None
        self._http: Optional[ThreadingHTTPServer] = None
        self._threads: List[threading.Thread] = []

    # -- what the servers call -------------------------------------------

    def accept(self, message: Received) -> int:
        with self._lock:
            self.received.append(message)
            self._mailbox.append(message)
            index = len(self.received)
        if self.fetch_interval <= 0:
            self.fetch()
        return index

    def record_request(self, method: str, path: str, authorization: Optional[str]) -> None:
        with self._lock:
            self.requests.append((method, path, authorization))

    def ticket_list(self) -> List[Ticket]:
        with self._lock:
            return list(self._tickets)

    @property
    def tickets(self) -> List[Ticket]:
        return self.ticket_list()

    # -- the channel fetch -----------------------------------------------

    def fetch(self) -> int:
        """Turn every message waiting in the mailbox into a ticket, as one run
        of the email channel does, and empty the mailbox. Returns how many."""
        with self._lock:
            waiting, self._mailbox = self._mailbox, []
            for message in waiting:
                self._tickets.append(self._ticket_from(message))
            self.fetches += 1
            return len(waiting)

    def _customer(self, address: str) -> Tuple[int, str]:
        login = address.strip().lower()
        if login not in self._users:
            self._users[login] = _ADMIN_ID + 1 + len(self._users)
        return self._users[login], login

    def _ticket_from(self, message: Received) -> Ticket:
        parsed = message_from_bytes(message.data, policy=default_policy)
        _, sender = parseaddr(str(parsed["From"] or ""))
        sender = sender or message.envelope_from
        customer_id, login = self._customer(sender)
        subject = str(parsed["Subject"] or "").strip()
        html = parsed.get_body(preferencelist=("html",))
        plain = parsed.get_body(preferencelist=("plain",))
        # Zammad keeps the HTML part when there is one, and the text otherwise.
        part = html if html is not None else plain
        body = part.get_content() if part is not None else ""
        to = ", ".join(
            address for _, address in getaddresses([str(parsed["To"] or "")]) if address
        )
        self._article_ids += 1
        ticket_id = len(self._tickets) + 1
        stamp = _now()
        return Ticket(
            id=ticket_id,
            number=str(FIRST_TICKET_NUMBER + ticket_id - 1),
            title=subject or "-",
            customer_id=customer_id,
            customer_login=login,
            group=self.group,
            created_at=stamp,
            articles=[Article(
                id=self._article_ids,
                from_addr=str(parsed["From"] or ""),
                to=to,
                subject=subject,
                body=body,
                content_type="text/html" if html is not None else "text/plain",
                message_id=str(parsed["Message-ID"] or "") or None,
            )],
        )

    def _fetch_loop(self) -> None:
        while not self._stop.wait(self.fetch_interval):
            self.fetch()

    # -- lifecycle -------------------------------------------------------

    @property
    def port(self) -> int:
        assert self._http is not None
        return self._http.server_address[1]

    @property
    def smtp_port(self) -> int:
        assert self._smtp is not None
        return self._smtp.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "ZammadFake":
        http_port, smtp_port = self._wanted_ports

        class SmtpServer(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class HttpServer(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._smtp = SmtpServer(("127.0.0.1", smtp_port), _SmtpHandler)
        self._smtp.fake = self  # type: ignore[attr-defined]
        self._http = HttpServer(("127.0.0.1", http_port), _ApiHandler)
        self._http.fake = self  # type: ignore[attr-defined]
        targets = [self._smtp.serve_forever, self._http.serve_forever]
        if self.fetch_interval > 0:
            targets.append(self._fetch_loop)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        for server in (self._smtp, self._http):
            if server is not None:
                server.shutdown()
                server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="A stand-in for Zammad: SMTP in, the ticket search API out.",
    )
    parser.add_argument("--port", type=int, default=3380, help="HTTP API port (default 3380)")
    parser.add_argument("--smtp-port", type=int, default=3325, help="SMTP port (default 3325)")
    parser.add_argument("--token", default="zammad-fake-token",
                        help="the API token the fake accepts")
    parser.add_argument("--fetch-interval", type=float, default=0.0,
                        help="seconds between channel fetches; 0 opens a ticket at once")
    args = parser.parse_args(argv)
    with ZammadFake(args.token, port=args.port, smtp_port=args.smtp_port,
                    fetch_interval=args.fetch_interval) as fake:
        print(f"zammad fake: API {fake.url}, SMTP 127.0.0.1:{fake.smtp_port}", flush=True)
        print(f"Authorization: Token token={fake.token}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
