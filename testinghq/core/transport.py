"""HTTP transport for firing (or attempting to fire) a serialized
InboundEmail at a target endpoint.

`post` takes an injectable client so tests never touch the real network. The
default client, UrllibHttpClient, is a thin wrapper around urllib.request and
is the only place in this module that opens a socket. Tests inject a fake
client that implements the same `send(PreparedRequest) -> ClientResponse`
shape and records what it received.

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
from typing import Callable, Dict, List, Optional, Protocol

from ..blast.payload import InboundEmail
from ..blast.serialize import FormField, FormFile, FormPart, to_multipart_parts
from .formats import WireFormat, get_format

# Fixed multipart boundary. Blast fires at endpoints the operator controls
# for testing, not at adversarial third parties, so a fixed boundary is
# sufficient and keeps the wire body byte-identical for the same payload
# (determinism), which matters for replay and for hermetic tests that assert
# on exact request bytes.
DEFAULT_BOUNDARY = "----testinghq-boundary-2f6a9c"

#: The format used when a target does not name one. SendGrid, because that is
#: what every config in the wild was written against before formats were
#: selectable, and changing a request's bytes without being asked would break
#: every recorded artifact and replay.
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
    """What an HttpClient hands back after a request completes."""

    status: int
    body: bytes


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
            buf.write(
                f'Content-Disposition: form-data; name="{part.name}"\r\n\r\n'.encode(
                    "utf-8"
                )
            )
            buf.write(part.value.encode("utf-8"))
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
                return ClientResponse(status=resp.status, body=resp.read())
        except urllib.error.HTTPError as exc:
            # A non-2xx status is still a response, not a transport failure.
            body = exc.read() if hasattr(exc, "read") else b""
            return ClientResponse(status=exc.code, body=body)


def build_request(
    payload: InboundEmail,
    target_url: str,
    *,
    timeout: float = 10.0,
    wire_format: Optional[WireFormat] = None,
    extra_headers: Optional[HeaderHook] = None,
) -> PreparedRequest:
    """Serialize `payload` and build the PreparedRequest that would be sent
    for it, without sending anything. Exposed separately so callers (e.g. a
    dry-run reporter) can inspect exactly what would go over the wire.

    `wire_format` defaults to SendGrid Inbound Parse, which is what this function
    emitted before the format layer existed, so an existing config with no
    `format` key produces the identical bytes.

    `extra_headers` is a callable taking the finished body bytes and returning a
    header dict. It runs after the body is built and never before, which is the
    only order that works: an HMAC signature or a checksum covers the exact
    bytes that go on the wire, so a hook that ran earlier would sign something
    the server never receives. A hook that changes nothing about the body cannot
    invalidate the Content-Length already computed from it.
    """
    if wire_format is None:
        wire_format = get_format(DEFAULT_FORMAT, DEFAULT_BOUNDARY)

    encoded = wire_format.encode(payload)
    body = encoded.body
    headers = {
        "Content-Type": encoded.content_type,
        "Content-Length": str(len(body)),
    }

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
            if name in headers:
                raise ValueError(
                    f"the extra_headers hook tried to set {name!r}, which the "
                    "transport already computed. A header hook may add headers, "
                    "not replace them: a replaced Content-Type or Content-Length "
                    "would not describe the body actually being sent."
                )
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
) -> TransportResult:
    """POST a serialized InboundEmail to `target_url` via `client`.

    `client` defaults to UrllibHttpClient (a real network call); tests should
    always inject a fake. `clock` is injectable for hermetic latency
    assertions and defaults to time.monotonic; it measures wall time around
    the client call only, it never contributes to generated payload content,
    so it does not affect Blast's determinism guarantee.

    A timeout is always enforced: it defaults to 10 seconds and is passed to
    the client on every call via PreparedRequest.timeout.

    `wire_format` and `extra_headers` are forwarded to `build_request` unchanged;
    see that function for why the hook runs after the body is built.
    """
    if client is None:
        client = UrllibHttpClient()

    request = build_request(
        payload,
        target_url,
        timeout=timeout,
        wire_format=wire_format,
        extra_headers=extra_headers,
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
