"""Shared fixtures for the security test suite.

Keep this hermetic: no real network, no real sleeps, no live services.
"""
import socket

import pytest


class NetworkCallAttempted(AssertionError):
    """Raised by the forbid_network fixture when code under test tries to
    open a real socket."""


@pytest.fixture
def forbid_network(monkeypatch):
    """Fail the test immediately if anything under test opens a socket.

    This is a hard, syscall-level backstop on top of any fake network
    client a test uses, so "dry-run makes zero network calls" is proven
    at more than one layer.
    """

    def _blocked(*args, **kwargs):
        raise NetworkCallAttempted(
            "a real socket was opened during a test that must be network-free"
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield
