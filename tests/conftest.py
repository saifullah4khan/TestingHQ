"""Suite-wide network block.

WHY THIS EXISTS. This repository already had four separate network blockers,
one per file, and every one of them was opt-in: a test had to remember to ask
for it. That is a bad default, because the failure it protects against is the
one nobody notices. During the 2026-09-27 session a single duplicate test
definition quietly made the suite open real sockets for 12 seconds and it stayed
green, and a missing `client` seam had the same shape, waiting 81 seconds on
real connection attempts. Both were caught by accident, not by a guard.

So the block is now the default for the whole suite and opting out is the thing
you have to do on purpose. That inversion is the entire point.

WHAT IS BLOCKED. `socket.socket.connect`, `socket.socket.connect_ex` and
`socket.create_connection`. Those are the three doors out. `bind` and `accept`
are left alone on purpose: `tests/web/test_server.py` genuinely starts a real
HTTP server on a loopback port and pytest's own machinery may bind a socket, so
blocking the server side would break things that are supposed to work while
catching nothing. A client that reaches the network still has to connect, and
that is the case worth stopping.

OPTING OUT. Mark the test with `@pytest.mark.allow_network`. Applied at module
level in `tests/web/test_server.py`, which is the only place that legitimately
needs loopback.

Two things worth knowing about the marker:

1. It is not inherited from a conftest `pytestmark`, only from the node itself or
   an enclosing class, so a module-level marker is the honest way to say "every
   test in this file". A blanket marker on a whole directory would defeat the
   purpose of having to be deliberate.
2. An unrecognised marker is silently ignored by pytest, so if the name were
   misspelled the tests would just be blocked and fail confusingly. It is
   registered in `pyproject.toml` under `markers` for that reason, and there is
   a test in `tests/unit/test_suite_network_block.py` that asserts the name is
   exactly right.

The error message names the test that tried to reach out, because "a real
socket was opened" with no context is close to useless when you are staring at
a 500-test run.
"""
from __future__ import annotations

import socket

import pytest

#: Attribute names restored and blocked on `socket.socket`. Captured once at
#: import time so the block is reversible and idempotent.
_PATCHED = ("connect", "connect_ex")
_ORIGINALS: dict = {}


class NetworkBlocked(BaseException):
    """Raised when a test that must stay hermetic tries to reach the network.

    Derives from `BaseException` rather than `Exception` so that it cannot be
    caught by an `except Exception` in the code under test.
    `core/transport.py` wraps every `client.send()` in one, to report any
    transport failure as a result rather than a crash, and that clause would
    otherwise turn a blocked connect into a recorded timeout and a green test.

    This is the technique pytest's own control-flow exceptions use: `Skipped`
    and `Exit` both derive from `OutcomeException(BaseException)` for the same
    reason. A consequence is that it surfaces as an error rather than a
    failure, which is correct here: a test that reached the network is not a
    wrong assertion, it is code that should not have run.
    """



def _blocked(name):
    def _raise(*args, **kwargs):
        raise NetworkBlocked(
            f"a test tried to open a network connection via socket.{name}.\n\n"
            "The whole suite is network-free by default; see tests/conftest.py.\n"
            "If the code under test is supposed to send something, inject a fake\n"
            "client rather than letting it reach a real socket. If the test itself\n"
            "genuinely needs loopback, mark it @pytest.mark.allow_network."
        )

    _raise.__name__ = f"_blocked_{name}"
    return _raise


def _install(blocking: bool) -> None:
    for name, original in _ORIGINALS.items():
        setattr(socket.socket, name, _blocked(name) if blocking else original)
    if "create_connection" in _ORIGINALS:
        socket.create_connection = (
            _blocked("create_connection")
            if blocking
            else _ORIGINALS["create_connection"]
        )


class _NetworkGuard:
    """The one handle onto this guard.

    Exposed as a fixture rather than imported, because a test cannot
    `from tests import conftest` and get the same module object pytest loaded:
    pytest imports it as top-level `conftest`, so that import gives a second
    copy with a different `NetworkBlocked` class and an empty `_ORIGINALS`.
    Every assertion then fails for a reason that has nothing to do with what it
    is asserting. Reaching the guard through a fixture guarantees one class and
    one set of originals, which is the only way its own tests can mean
    anything.
    """

    def __init__(self):
        self.error_class = NetworkBlocked
        self.marked_attr = "connect"

    @property
    def originals(self):
        return dict(_ORIGINALS)

    def install(self, blocking: bool) -> None:
        _install(blocking)

    def is_blocked(self) -> bool:
        return getattr(socket.socket, "connect") is not _ORIGINALS.get("connect")


@pytest.fixture(scope="session")
def network_guard():
    """Handle for asserting on the network block itself.

    Session-scoped so the state it reports is the state the rest of the run
    sees, rather than a per-test re-snapshot that could differ.
    """
    return _NetworkGuard()


@pytest.fixture(scope="session", autouse=True)
def block_network_for_the_entire_session():
    """Patch the three network doors shut for every test in the session.

    Session-scoped so the patching is genuinely once-per-run rather than
    once-per-test, and so it cannot be lost by a fixture ordering accident in
    one file.
    """
    for name in _PATCHED:
        _ORIGINALS[name] = getattr(socket.socket, name)
    _ORIGINALS["create_connection"] = socket.create_connection
    _install(blocking=True)
    yield
    _install(blocking=False)


@pytest.fixture(autouse=True)
def honour_allow_network_marker(request):
    """Lift the block for the duration of a single marked test.

    Function-scoped, because only the function-scope fixture can see which test
    is running. The session fixture does the patching; this one decides whether
    the patch is currently in force, and puts it back afterwards so one test
    opting out cannot leak into the next.
    """
    if request.node.get_closest_marker("allow_network") is not None:
        _install(blocking=False)
    yield
    _install(blocking=True)
