"""Transport for firing (or attempting to fire) a serialized InboundEmail at
a target: an HTTP POST, or an SMTP transaction.

`send(payload, resolved)` is the one entry point every send path uses. It
dispatches on `resolved.transport`: "http" goes through `post`, "smtp" through
`send_smtp`. Both take an injectable client so tests never touch the real
network. The default HTTP client, UrllibHttpClient, is a thin wrapper around
urllib.request, and the default SMTP client, SmtplibClient, one around
smtplib; they and the two persistent clients at the end of this module are the
only places that open a socket. Tests inject a fake client that implements the
same `send(PreparedRequest) -> ClientResponse` (or
`send(SmtpRequest) -> SmtpReply`) shape and records what it received.

A timeout is always enforced: `post` takes a `timeout` argument (seconds,
default 10.0) and threads it through to the client on every call. The
default client passes it straight to urlopen.
"""
from __future__ import annotations

import io
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Protocol

from ..blast.payload import InboundEmail
from ..blast.serialize import FormField, FormFile, FormPart, part_value_bytes
from .formats import WireFormat, get_format

# Fixed multipart boundary. Blast fires at endpoints the operator controls
# for testing, not at adversarial third parties, so a fixed boundary is
# sufficient and keeps the wire body byte-identical for the same payload
# (determinism), which matters for replay and for hermetic tests that assert
# on exact request bytes.
DEFAULT_BOUNDARY = "----testinghq-boundary-2f6a9c"

#: The format used when a target does not name one.
DEFAULT_FORMAT = "sendgrid"

#: A hook that sees the finished body and returns headers to add. Signatures and
#: checksums are the reason this exists: they cover the exact bytes, so they
#: cannot be computed until the bytes exist.
HeaderHook = Callable[[bytes], Dict[str, str]]


@dataclass(frozen=True)
class PreparedRequest:
    """A fully-built HTTP request, ready to hand to an HttpClient."""

    url: str
    method: str
    headers: Dict[str, str]
    body: bytes
    timeout: float


@dataclass(frozen=True)
class ClientResponse:
    """What an HttpClient hands back after a request completes. `headers` is
    optional for a client to fill; the readback adapter reads `Retry-After`
    from it."""

    status: int
    body: bytes
    headers: Dict[str, str] = field(default_factory=dict)


class HttpClient(Protocol):
    """The shape any injectable HTTP client must satisfy. Raise on transport
    failure (connection refused, DNS failure, timeout); return a
    ClientResponse (any status code, including 4xx/5xx) for anything that
    got a response from the server."""

    def send(self, request: PreparedRequest) -> ClientResponse: ...


@dataclass(frozen=True)
class TransportResult:
    """The outcome of one `post` call.

    `status` and `body_snippet` are None/"" when the request never got a
    response (the client raised); `error` carries the failure message in
    that case. `sent` is False when this result describes a dry-run that
    never touched a client at all (see `describe`)."""

    status: Optional[int]
    latency_ms: float
    body_snippet: str
    error: Optional[str] = None
    sent: bool = True


def encode_multipart(parts: List[FormPart], boundary: str = DEFAULT_BOUNDARY) -> bytes:
    """Encode a list of FormField/FormFile parts as a multipart/form-data
    body, in the given order, using CRLF line endings per RFC 7578."""
    buf = io.BytesIO()
    for part in parts:
        buf.write(f"--{boundary}\r\n".encode("utf-8"))
        if isinstance(part, FormFile):
            content_type = part.content_type or "application/octet-stream"
            buf.write(
                (
                    f'Content-Disposition: form-data; name="{part.name}"; '
                    f'filename="{part.filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8")
            )
            buf.write(part.content)
            buf.write(b"\r\n")
        elif isinstance(part, FormField):
            disposition = f'; name="{part.name}"' if part.name else ""
            buf.write(
                f"Content-Disposition: form-data{disposition}\r\n\r\n".encode("utf-8")
            )
            buf.write(part_value_bytes(part))
            buf.write(b"\r\n")
        else:  # pragma: no cover - defensive, FormPart is a closed union
            raise TypeError(f"unknown form part type: {type(part).__name__}")
    buf.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buf.getvalue()


def _content_snippet(body: bytes, limit: int = 200) -> str:
    return body[:limit].decode("utf-8", errors="replace")


class UrllibHttpClient:
    """Default HttpClient: a thin wrapper around urllib.request. This is the
    only code path in testinghq that opens a real socket."""

    def send(self, request: PreparedRequest) -> ClientResponse:
        req = urllib.request.Request(
            request.url,
            # `None` rather than `b""` for an empty body. urllib treats an empty
            # bytes body as a body to send, and puts a Content-Length: 0 on a
            # GET, which is legal and which some HTTP servers reject. The
            # readback adapter issues GETs, so this is on a live path rather
            # than a hypothetical one.
            data=request.body if request.body else None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout) as resp:
                return ClientResponse(
                    status=resp.status, body=resp.read(), headers=dict(resp.headers.items())
                )
        except urllib.error.HTTPError as exc:
            # A non-2xx status is still a response, not a transport failure.
            body = exc.read() if hasattr(exc, "read") else b""
            headers = dict(exc.headers.items()) if exc.headers is not None else {}
            return ClientResponse(status=exc.code, body=body, headers=headers)


def build_request(
    payload: InboundEmail,
    target_url: str,
    *,
    timeout: float = 10.0,
    wire_format: Optional[WireFormat] = None,
    extra_headers: Optional[HeaderHook] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> PreparedRequest:
    """Serialize `payload` and build the PreparedRequest that would be sent
    for it, without sending anything. Exposed separately so callers (e.g. a
    dry-run reporter) can inspect exactly what would go over the wire.

    `wire_format` defaults to SendGrid Inbound Parse (parsed mode). Send paths
    pass the resolved target's format (`core.targets.ResolvedTarget`).

    `extra_headers` is a callable taking the finished body bytes and returning a
    header dict. It runs after the body is built and never before, which is the
    only order that works: an HMAC signature or a checksum covers the exact
    bytes that go on the wire, so a hook that ran earlier would sign something
    the server never receives. A hook that changes nothing about the body cannot
    invalidate the Content-Length already computed from it.

    `headers` are the target's own headers (`[targets.<name>.headers]`, already
    resolved from the environment), added to every request. They may not name
    Content-Type or Content-Length, which describe the body and are computed
    here. A hook header with the same name as a target header replaces it:
    sigcheck's hook carries the credential under test, and that is the one that
    has to reach the endpoint.
    """
    if wire_format is None:
        wire_format = get_format(DEFAULT_FORMAT, DEFAULT_BOUNDARY)

    encoded = wire_format.encode(payload)
    body = encoded.body
    computed = {
        "Content-Type": encoded.content_type,
        "Content-Length": str(len(body)),
    }
    computed_names = {name.casefold() for name in computed}
    headers_out = dict(computed)
    for name, value in (headers or {}).items():
        if name.casefold() in computed_names:
            raise ValueError(
                f"a target header may not set {name!r}: the transport computes "
                "it from the body, and a replaced value would not describe the "
                "body actually being sent"
            )
        headers_out[name] = value
    headers = headers_out

    if extra_headers is not None:
        supplied = extra_headers(body)
        if not isinstance(supplied, dict):
            raise TypeError(
                "the extra_headers hook must return a dict of header names to "
                f"values, got {type(supplied).__name__}"
            )
        for name, value in supplied.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError(
                    "the extra_headers hook must return string header names and "
                    f"string values, got {name!r}: {value!r}"
                )
            # Refuse rather than overwrite. A hook silently replacing
            # Content-Type would produce a request whose announced type does not
            # describe its own body, which is the failure the format layer
            # removes, reintroduced through the back door.
            if name.casefold() in computed_names:
                raise ValueError(
                    f"the extra_headers hook tried to set {name!r}, which the "
                    "transport already computed. A header hook may add headers, "
                    "not replace them: a replaced Content-Type or Content-Length "
                    "would not describe the body actually being sent."
                )
            for existing in [k for k in headers if k.casefold() == name.casefold()]:
                del headers[existing]
            headers[name] = value

    return PreparedRequest(
        url=target_url, method="POST", headers=headers, body=body, timeout=timeout
    )


def post(
    payload: InboundEmail,
    target_url: str,
    client: Optional[HttpClient] = None,
    *,
    timeout: float = 10.0,
    clock=time.monotonic,
    wire_format: Optional[WireFormat] = None,
    extra_headers: Optional[HeaderHook] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> TransportResult:
    """POST a serialized InboundEmail to `target_url` via `client`.

    `client` defaults to UrllibHttpClient (a real network call); tests should
    always inject a fake. `clock` is injectable for hermetic latency
    assertions and defaults to time.monotonic; it measures wall time around
    the client call only, it never contributes to generated payload content,
    so it does not affect Blast's determinism guarantee.

    A timeout is always enforced: it defaults to 10 seconds and is passed to
    the client on every call via PreparedRequest.timeout.

    `wire_format`, `extra_headers` and `headers` are forwarded to
    `build_request` unchanged; see that function for why the hook runs after
    the body is built.
    """
    if client is None:
        client = UrllibHttpClient()

    request = build_request(
        payload,
        target_url,
        timeout=timeout,
        wire_format=wire_format,
        extra_headers=extra_headers,
        headers=headers,
    )

    start = clock()
    try:
        response = client.send(request)
    except Exception as exc:  # noqa: BLE001 - any transport failure is reportable
        elapsed_ms = (clock() - start) * 1000
        return TransportResult(
            status=None,
            latency_ms=elapsed_ms,
            body_snippet="",
            error=str(exc),
        )
    elapsed_ms = (clock() - start) * 1000
    return TransportResult(
        status=response.status,
        latency_ms=elapsed_ms,
        body_snippet=_content_snippet(response.body),
        error=None,
    )


# ---------------------------------------------------------------------------
# Keep-alive client. A self-contained block at the end of the module, with its
# own imports, so it can be maintained without touching the code above.
# ---------------------------------------------------------------------------

import http.client as _http_client  # noqa: E402
from urllib.parse import urlsplit as _urlsplit  # noqa: E402

#: What a server that closed an idle kept-alive connection looks like from the
#: client side. Retried once, and only on a connection that already carried a
#: request: on a fresh connection the same error is a real failure, and a
#: retry there could deliver a request twice.
_STALE_CONNECTION_ERRORS = (
    _http_client.RemoteDisconnected,
    _http_client.BadStatusLine,
    ConnectionResetError,
    BrokenPipeError,
)


def _connection_for(scheme: str, host: str, port: Optional[int], timeout: float):
    if scheme == "https":
        return _http_client.HTTPSConnection(host, port, timeout=timeout)
    return _http_client.HTTPConnection(host, port, timeout=timeout)


class PersistentHttpClient(HttpClient):
    """An HttpClient that keeps one HTTP/1.1 connection open across requests.

    NOT thread-safe: give each worker thread its own instance. Returns a
    ClientResponse for any status; raises on transport failure, like every
    HttpClient. `PreparedRequest.timeout` applies to every request, including
    one sent over a connection opened with a different timeout.

    `connect_seconds` records the setup time of every connection this instance
    opened, so a caller can report handshakes separately from request latency.
    `connection_factory` exists for tests; it takes (scheme, host, port,
    timeout) and returns an `http.client.HTTPConnection`-shaped object.
    """

    def __init__(self, connection_factory=None, clock=time.perf_counter) -> None:
        self._factory = connection_factory or _connection_for
        self._clock = clock
        self._conn = None
        self._origin = None
        self._used = False
        self.connect_seconds: List[float] = []
        self.reconnects = 0

    @staticmethod
    def _split(url: str):
        parts = _urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {url!r}")
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        return (parts.scheme, parts.hostname, parts.port), target

    def connect(self, url: str, timeout: float = 10.0) -> float:
        """Open the connection for `url` now and return its setup time in
        seconds. Optional: `send` connects on demand."""
        origin, _target = self._split(url)
        return self._open(origin, timeout)

    def _open(self, origin, timeout: float) -> float:
        self.close()
        scheme, host, port = origin
        conn = self._factory(scheme, host, port, timeout)
        started = self._clock()
        conn.connect()
        elapsed = self._clock() - started
        self._conn, self._origin, self._used = conn, origin, False
        self.connect_seconds.append(elapsed)
        return elapsed

    def close(self) -> None:
        conn, self._conn, self._origin = self._conn, None, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    def send(self, request: PreparedRequest) -> ClientResponse:
        origin, target = self._split(request.url)
        for attempt in (0, 1):
            if self._conn is None or self._origin != origin:
                self._open(origin, request.timeout)
            conn, reused = self._conn, self._used
            conn.timeout = request.timeout
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(request.timeout)
            try:
                conn.request(
                    request.method, target,
                    body=request.body if request.body else None,
                    headers=request.headers,
                )
                response = conn.getresponse()
                body = response.read()
            except _STALE_CONNECTION_ERRORS:
                self.close()
                if not reused or attempt:
                    raise
                self.reconnects += 1
                continue
            except BaseException:
                self.close()
                raise
            self._used = True
            if response.will_close:
                self.close()
            # Same shape UrllibHttpClient returns: the readback adapter reads
            # `Retry-After` from here, and loses it if this client drops it.
            raw_headers = getattr(response, "headers", None)
            headers = dict(raw_headers.items()) if raw_headers is not None else {}
            return ClientResponse(status=response.status, body=body, headers=headers)
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# SMTP. A self-contained block like the keep-alive one above: the message is
# exactly what the `mime` format builds, handed to a mail server in one SMTP
# transaction instead of POSTed.
# ---------------------------------------------------------------------------

import smtplib as _smtplib  # noqa: E402
import ssl as _ssl  # noqa: E402
from email.utils import getaddresses as _getaddresses  # noqa: E402
from typing import Sequence, Tuple  # noqa: E402

#: The transports a target may name. "http" is the default.
TRANSPORT_HTTP = "http"
TRANSPORT_SMTP = "smtp"
TRANSPORTS = (TRANSPORT_HTTP, TRANSPORT_SMTP)
DEFAULT_TRANSPORT = TRANSPORT_HTTP

#: The SMTP port when a target names none.
DEFAULT_SMTP_PORT = 25

#: The name this tool gives in EHLO. On a reserved TLD so it can never be read
#: as a real host, and fixed so smtplib does not look up the local FQDN, which
#: can take seconds on a machine with slow reverse DNS.
EHLO_NAME = "testinghq.localhost"


@dataclass(frozen=True)
class SmtpEndpoint:
    """Where and how to hand a message to a mail server: the resolved form of
    an SMTP target's keys. `username` and `password` are the values read from
    the environment, never the `env:` references; `password` is kept out of
    the repr so a traceback or a debug print does not carry it.

    `envelope_from` and `envelope_to` override the payload's own envelope when
    set. None (and an empty tuple) mean "use the payload's".
    """

    host: str
    port: int = DEFAULT_SMTP_PORT
    starttls: bool = False
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    envelope_from: Optional[str] = None
    envelope_to: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SmtpRequest:
    """One fully-built SMTP transaction, ready to hand to an SmtpClient."""

    endpoint: SmtpEndpoint
    envelope_from: str
    envelope_to: Tuple[str, ...]
    message: bytes
    timeout: float


@dataclass(frozen=True)
class SmtpReply:
    """The reply that ended a transaction: the `250` after DATA when it went
    through, or the first negative reply (to MAIL, RCPT or DATA) when not."""

    code: int
    text: str


class SmtpClient(Protocol):
    """The shape any injectable SMTP client must satisfy. Return an SmtpReply
    for any reply the server gave (positive or negative); raise on transport
    failure (connection refused, DNS failure, timeout, TLS). A negative reply
    to the greeting or to AUTH may be raised as `smtplib.SMTPResponseException`,
    which `send_smtp` records as the status, like a returned reply."""

    def send(self, request: SmtpRequest) -> SmtpReply: ...


def _reply_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _open_smtp(endpoint: SmtpEndpoint, timeout: float, factory=None):
    """Connect, read the greeting, EHLO, STARTTLS and log in. Returns the open
    `smtplib.SMTP`. Raises on any failure, closing what it opened."""
    factory = factory or _smtplib.SMTP
    conn = factory(endpoint.host, endpoint.port, local_hostname=EHLO_NAME, timeout=timeout)
    try:
        conn.ehlo_or_helo_if_needed()
        if endpoint.starttls:
            conn.starttls(context=_ssl.create_default_context())
            conn.ehlo()
        if endpoint.username:
            conn.login(endpoint.username, endpoint.password or "")
    except BaseException:
        _close_smtp(conn)
        raise
    return conn


def _close_smtp(conn) -> None:
    try:
        conn.quit()
    except Exception:  # noqa: BLE001 - closing is best effort
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _transaction(conn, request: SmtpRequest) -> SmtpReply:
    """MAIL, RCPT per recipient, DATA, on an open connection. Stops at the
    first negative reply, resets the transaction and returns that reply, so one
    message is one transaction whatever happens to it."""
    code, text = conn.mail(request.envelope_from)
    if code != 250:
        _rset(conn)
        return SmtpReply(code, _reply_text(text))
    for recipient in request.envelope_to:
        code, text = conn.rcpt(recipient)
        if code not in (250, 251):
            _rset(conn)
            return SmtpReply(code, _reply_text(text))
    try:
        code, text = conn.data(request.message)
    except _smtplib.SMTPDataError as exc:
        _rset(conn)
        return SmtpReply(exc.smtp_code, _reply_text(exc.smtp_error))
    return SmtpReply(code, _reply_text(text))


def _rset(conn) -> None:
    try:
        conn.rset()
    except _smtplib.SMTPServerDisconnected:
        pass


class SmtplibClient:
    """Default SmtpClient: one connection per message, closed after it. The
    connection setup is inside the latency, as it is for UrllibHttpClient."""

    def __init__(self, smtp_factory=None) -> None:
        self._factory = smtp_factory

    def send(self, request: SmtpRequest) -> SmtpReply:
        conn = _open_smtp(request.endpoint, request.timeout, self._factory)
        try:
            return _transaction(conn, request)
        finally:
            _close_smtp(conn)


class PersistentSmtpClient:
    """An SmtpClient that keeps one SMTP connection open across messages, for
    barrage: one per worker thread, NOT thread-safe.

    `connect_seconds` records the setup time (connect, greeting, EHLO,
    STARTTLS, AUTH) of every connection this instance opened, so a caller can
    report it separately from message latency, which then runs from MAIL to the
    reply after DATA. A connection the server dropped (`SMTPServerDisconnected`)
    is reopened once, and only when it had already carried a message, the same
    rule PersistentHttpClient follows.
    """

    def __init__(self, smtp_factory=None, clock=time.perf_counter) -> None:
        self._factory = smtp_factory
        self._clock = clock
        self._conn = None
        self._endpoint: Optional[SmtpEndpoint] = None
        self._used = False
        self.connect_seconds: List[float] = []
        self.reconnects = 0

    def connect(self, endpoint: SmtpEndpoint, timeout: float = 10.0) -> float:
        """Open the connection now and return its setup time in seconds."""
        self.close()
        started = self._clock()
        conn = _open_smtp(endpoint, timeout, self._factory)
        elapsed = self._clock() - started
        self._conn, self._endpoint, self._used = conn, endpoint, False
        self.connect_seconds.append(elapsed)
        return elapsed

    def close(self) -> None:
        conn, self._conn, self._endpoint = self._conn, None, None
        if conn is not None:
            _close_smtp(conn)

    def send(self, request: SmtpRequest) -> SmtpReply:
        for attempt in (0, 1):
            if self._conn is None or self._endpoint != request.endpoint:
                self.connect(request.endpoint, request.timeout)
            conn, reused = self._conn, self._used
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(request.timeout)
            try:
                reply = _transaction(conn, request)
            except _smtplib.SMTPServerDisconnected:
                self.close()
                if not reused or attempt:
                    raise
                self.reconnects += 1
                continue
            except BaseException:
                self.close()
                raise
            self._used = True
            if reply.code == 421:
                # 421 is the server closing the connection; the next message
                # needs a new one.
                self.close()
            return reply
        raise AssertionError("unreachable")  # pragma: no cover


def _bare_addresses(values: Sequence[str]) -> Tuple[str, ...]:
    return tuple(address for _name, address in _getaddresses(list(values)) if address)


def smtp_envelope(payload: InboundEmail, endpoint: SmtpEndpoint) -> Tuple[str, Tuple[str, ...]]:
    """(MAIL FROM, RCPT TO list) for `payload` at `endpoint`.

    The target's `envelope_from` and `envelope_to` win when set. Otherwise the
    sender is the payload's envelope sender, kept as it is when empty (a null
    reverse-path, which is what a bounce carries), and the recipients are the
    payload's envelope recipients, or its To addresses when it has none.
    """
    sender = endpoint.envelope_from
    if sender is None:
        sender = payload.envelope.from_addr or ""
        if sender:
            sender = (_bare_addresses([sender]) or (sender,))[0]
    recipients = tuple(endpoint.envelope_to)
    if not recipients:
        recipients = _bare_addresses(payload.envelope.to) or _bare_addresses(
            [payload.to] if payload.to else []
        )
    return sender, recipients


def build_smtp_request(
    payload: InboundEmail,
    endpoint: SmtpEndpoint,
    *,
    timeout: float = 10.0,
    wire_format: Optional[WireFormat] = None,
) -> SmtpRequest:
    """The SmtpRequest that would be sent for `payload`, without sending. The
    message is the body `wire_format` encodes (the `mime` format by default),
    byte for byte."""
    if wire_format is None:
        wire_format = get_format("mime", DEFAULT_BOUNDARY)
    sender, recipients = smtp_envelope(payload, endpoint)
    return SmtpRequest(
        endpoint=endpoint,
        envelope_from=sender,
        envelope_to=recipients,
        message=wire_format.encode(payload).body,
        timeout=timeout,
    )


def send_smtp(
    payload: InboundEmail,
    endpoint: SmtpEndpoint,
    client: Optional[SmtpClient] = None,
    *,
    timeout: float = 10.0,
    clock=time.monotonic,
    wire_format: Optional[WireFormat] = None,
) -> TransportResult:
    """Hand `payload` to the mail server at `endpoint` in one transaction.

    The result has the same shape as an HTTP one. `status` is the reply code
    that ended the transaction: the `250` after DATA, or the first 4xx or 5xx,
    whose text is then also the `error`. `body_snippet` is the reply text.
    `status` is None only when no reply came at all (refused, DNS, timeout,
    TLS), with the reason in `error`, which the report sorts into the same
    buckets as an HTTP failure. Latency runs to the reply after DATA.
    """
    if client is None:
        client = SmtplibClient()
    request = build_smtp_request(payload, endpoint, timeout=timeout, wire_format=wire_format)
    if not request.envelope_to:
        return TransportResult(
            status=None, latency_ms=0.0, body_snippet="",
            error=(
                "no envelope recipient: the payload has no envelope or To "
                "address and the target sets no envelope_to"
            ),
        )
    start = clock()
    try:
        reply = client.send(request)
    except _smtplib.SMTPResponseException as exc:
        elapsed_ms = (clock() - start) * 1000
        text = _reply_text(exc.smtp_error)
        return TransportResult(
            status=exc.smtp_code, latency_ms=elapsed_ms,
            body_snippet=text[:200], error=text,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is reportable
        elapsed_ms = (clock() - start) * 1000
        return TransportResult(
            status=None, latency_ms=elapsed_ms, body_snippet="",
            error=str(exc) or type(exc).__name__,
        )
    elapsed_ms = (clock() - start) * 1000
    text = _reply_text(reply.text)
    return TransportResult(
        status=reply.code,
        latency_ms=elapsed_ms,
        body_snippet=text[:200],
        error=None if 200 <= reply.code < 300 else text,
    )


# ---------------------------------------------------------------------------
# The one send entry point
# ---------------------------------------------------------------------------


class Destination(Protocol):
    """What `send` needs to know about where a payload goes.
    `core.targets.ResolvedTarget` is the real one; `HttpDestination` is the
    bare-URL one."""

    transport: str
    url: str
    wire_format: Optional[WireFormat]
    headers: Mapping[str, str]
    smtp: Optional[SmtpEndpoint]


@dataclass(frozen=True)
class HttpDestination:
    """An HTTP destination with no target behind it: a URL, and optionally a
    format and headers. For callers that hold a URL rather than a resolved
    target, such as tests and the library helpers that still accept one."""

    url: str
    wire_format: Optional[WireFormat] = None
    headers: Mapping[str, str] = field(default_factory=dict, hash=False)
    transport: str = TRANSPORT_HTTP
    smtp: Optional[SmtpEndpoint] = None


def as_destination(target, wire_format: Optional[WireFormat] = None):
    """`target` as a Destination: a URL string becomes an HttpDestination with
    `wire_format`, and anything else (a ResolvedTarget) is returned as is."""
    if isinstance(target, str):
        return HttpDestination(url=target, wire_format=wire_format)
    return target


def send(
    payload: InboundEmail,
    resolved: Destination,
    client=None,
    *,
    timeout: float = 10.0,
    clock=time.monotonic,
    wire_format: Optional[WireFormat] = None,
    extra_headers: Optional[HeaderHook] = None,
) -> TransportResult:
    """Send `payload` to `resolved` over its transport. Every send path calls
    this.

    `resolved` is a `core.targets.ResolvedTarget` (or anything with its
    `transport`, `url`, `wire_format`, `headers` and `smtp`). `client` is the
    transport's injectable client: an HttpClient for "http", an SmtpClient for
    "smtp"; None means the real one. `wire_format` overrides the target's own,
    for a caller that wraps it (loop's DSN encoder, sigcheck's signed Mailgun
    fields). `extra_headers` is the HTTP header hook (see `build_request`); an
    SMTP target has no request headers, so passing one there is a ValueError.
    """
    fmt = wire_format if wire_format is not None else resolved.wire_format
    if resolved.transport == TRANSPORT_SMTP:
        if extra_headers is not None:
            raise ValueError("an SMTP target takes no request headers")
        if resolved.smtp is None:
            raise ValueError("an SMTP destination needs its SmtpEndpoint")
        return send_smtp(
            payload, resolved.smtp, client, timeout=timeout, clock=clock, wire_format=fmt,
        )
    if resolved.transport != TRANSPORT_HTTP:
        raise ValueError(f"unknown transport {resolved.transport!r}")
    return post(
        payload, resolved.url, client, timeout=timeout, clock=clock,
        wire_format=fmt, extra_headers=extra_headers, headers=resolved.headers,
    )

