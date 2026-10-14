"""PersistentHttpClient, driven through a fake connection factory.

The real-socket behaviour (one TCP connection for many requests) is pinned in
tests/e2e/test_real_sockets.py. These pin the retry and timeout rules, which a
loopback server cannot provoke on demand.
"""
from __future__ import annotations

import http.client

import pytest

from testinghq.core.transport import (
    ClientResponse,
    PersistentHttpClient,
    PreparedRequest,
)


class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeResponse:
    def __init__(self, status=200, body=b"ok", will_close=False):
        self.status = status
        self._body = body
        self.will_close = will_close

    def read(self):
        return self._body


class FakeConnection:
    """Plays back a script: each entry is a FakeResponse or an exception."""

    def __init__(self, script, log):
        self.script = list(script)
        self.log = log
        self.sock = None
        self.timeout = None
        self.closed = False

    def connect(self):
        self.sock = FakeSock()
        self.log.append(("connect",))

    def request(self, method, target, body=None, headers=None):
        self.log.append(("request", method, target, body))

    def getresponse(self):
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self):
        self.closed = True


def _factory(scripts, log, opened):
    def make(scheme, host, port, timeout):
        conn = FakeConnection(scripts.pop(0), log)
        opened.append((scheme, host, port, timeout, conn))
        return conn

    return make


def _request(url="http://127.0.0.1:8000/intake?x=1", timeout=3.0):
    return PreparedRequest(url=url, method="POST", headers={"Content-Length": "2"},
                           body=b"hi", timeout=timeout)


def test_one_connection_carries_every_request():
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse(), FakeResponse(), FakeResponse()]], log, opened)
    )
    for _ in range(3):
        assert client.send(_request()) == ClientResponse(status=200, body=b"ok")
    assert len(opened) == 1
    assert [e[0] for e in log].count("connect") == 1
    assert log[1] == ("request", "POST", "/intake?x=1", b"hi")
    assert len(client.connect_seconds) == 1


def test_any_status_is_a_response_not_an_error():
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse(503, b"busy"), FakeResponse(429, b"")]], log, opened)
    )
    assert client.send(_request()).status == 503
    assert client.send(_request()).status == 429


@pytest.mark.parametrize("stale", [
    http.client.RemoteDisconnected("closed"),
    http.client.BadStatusLine(""),
    ConnectionResetError("reset"),
])
def test_a_stale_reused_connection_is_reopened_once(stale):
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse(), stale], [FakeResponse(201)]], log, opened)
    )
    client.send(_request())
    assert client.send(_request()).status == 201
    assert len(opened) == 2
    assert client.reconnects == 1
    assert opened[0][4].closed


def test_a_fresh_connection_failing_is_not_retried():
    """The server never answered a request on a brand new connection. A retry
    could deliver the request twice, so it is reported instead."""
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[ConnectionResetError("reset")], [FakeResponse()]], log, opened)
    )
    with pytest.raises(ConnectionResetError):
        client.send(_request())
    assert len(opened) == 1


def test_the_retry_happens_once_per_request_not_forever():
    log, opened = [], []
    client = PersistentHttpClient(connection_factory=_factory(
        [[FakeResponse(), ConnectionResetError("a")], [ConnectionResetError("b")]], log, opened
    ))
    client.send(_request())
    with pytest.raises(ConnectionResetError):
        client.send(_request())
    assert len(opened) == 2


def test_the_request_timeout_is_applied_to_every_request():
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse(), FakeResponse()]], log, opened)
    )
    client.send(_request(timeout=2.5))
    client.send(_request(timeout=0.75))
    conn = opened[0][4]
    assert opened[0][3] == 2.5, "the connect itself is bounded by the request timeout"
    assert conn.sock.timeouts == [2.5, 0.75]


def test_a_server_that_closes_after_the_response_gets_a_new_connection_next_time():
    log, opened = [], []
    client = PersistentHttpClient(connection_factory=_factory(
        [[FakeResponse(will_close=True)], [FakeResponse()]], log, opened
    ))
    client.send(_request())
    client.send(_request())
    assert len(opened) == 2
    assert client.reconnects == 0


def test_a_different_origin_opens_its_own_connection():
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse()], [FakeResponse()]], log, opened)
    )
    client.send(_request("http://127.0.0.1:8000/a"))
    client.send(_request("https://127.0.0.1:8443/b"))
    assert [(o[0], o[2]) for o in opened] == [("http", 8000), ("https", 8443)]


def test_connect_measures_setup_separately():
    ticks = iter([1.0, 1.25])
    log, opened = [], []
    client = PersistentHttpClient(
        connection_factory=_factory([[FakeResponse()]], log, opened), clock=lambda: next(ticks)
    )
    assert client.connect("http://127.0.0.1:8000/intake", timeout=1.0) == pytest.approx(0.25)
    client.send(_request())
    assert len(opened) == 1, "send reuses the connection connect() opened"
