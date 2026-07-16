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
from typing import Dict, List, Optional, Protocol

from ..blast.payload import InboundEmail
from ..blast.serialize import FormField, FormFile, FormPart, to_multipart_parts

# Fixed multipart boundary. Blast fires at endpoints the operator controls
# for testing, not at adversarial third parties, so a fixed boundary is
# sufficient and keeps the wire body byte-identical for the same payload
# (determinism), which matters for replay and for hermetic tests that assert
# on exact request bytes.
DEFAULT_BOUNDARY = "----testinghq-boundary-2f6a9c"


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
            data=request.body,
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
    payload: InboundEmail, target_url: str, *, timeout: float = 10.0
) -> PreparedRequest:
    """Serialize `payload` and build the PreparedRequest that would be sent
    for it, without sending anything. Exposed separately so callers (e.g. a
    dry-run reporter) can inspect exactly what would go over the wire."""
    parts = to_multipart_parts(payload)
    body = encode_multipart(parts)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={DEFAULT_BOUNDARY}",
        "Content-Length": str(len(body)),
    }
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
) -> TransportResult:
    """POST a serialized InboundEmail to `target_url` via `client`.

    `client` defaults to UrllibHttpClient (a real network call); tests should
    always inject a fake. `clock` is injectable for hermetic latency
    assertions and defaults to time.monotonic; it measures wall time around
    the client call only, it never contributes to generated payload content,
    so it does not affect Blast's determinism guarantee.

    A timeout is always enforced: it defaults to 10 seconds and is passed to
    the client on every call via PreparedRequest.timeout.
    """
    if client is None:
        client = UrllibHttpClient()

    request = build_request(payload, target_url, timeout=timeout)

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
