"""Tests for the suite-wide network block in tests/conftest.py.

A guard that blocks the network has to be tested, because the failure it
prevents is invisible: a test that quietly reaches the network tends to pass,
just slowly, and the slowdown is easy to mistake for a slow test rather than a
broken one. Every assertion here is about the guard being real, not about the
code it guards.

Two things this module deliberately does not do.

It does not `import conftest`. pytest imports that file as top-level
`conftest`, so `from tests import conftest` yields a second copy with a
different `NetworkBlocked` class and an empty originals dict, and every
assertion below then fails for a reason unrelated to what it asserts. The
guard is reached through the `network_guard` fixture instead, which
guarantees one class and one set of originals.

It does not assert cross-test isolation, that is, that a marked test cannot
leak an open door into the test after it. That is a real property and the
`honour_allow_network_marker` fixture restores the block in a `finally`-shaped
teardown for it, but it cannot be asserted from inside the suite without making
this file's own result depend on execution order. The mechanism is exercised
directly through `network_guard.install` instead, which is deterministic.
"""
import ast
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_block_is_in_force_for_an_unmarked_test(network_guard):
    """This test carries no `allow_network`, so the block applies to it. If the
    session fixture is not running, this fails, which is the point: the guard
    asserts the guard."""
    with pytest.raises(network_guard.error_class) as exc:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    assert "socket.create_connection" in str(exc.value)


def test_socket_connect_is_blocked_too_not_just_create_connection(network_guard):
    """Three doors, all of them shut. A guard that patched only one would be
    decoration: the stdlib prefers `create_connection` for some calls and
    reaches for `sock.connect` in others."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(network_guard.error_class) as exc:
            sock.connect(("127.0.0.1", 9))
        assert "socket.connect" in str(exc.value)
    finally:
        sock.close()


def test_connect_ex_is_blocked_as_well(network_guard):
    """The third door. Nothing in this suite uses it today, which is exactly
    why it would go unblocked by accident: the day something does, it would be
    new code."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(network_guard.error_class):
            sock.connect_ex(("127.0.0.1", 9))
    finally:
        sock.close()


def test_the_error_is_an_assertion_error_not_a_crash(network_guard):
    """It subclasses AssertionError so it reads as a test failure rather than
    an error, and, more importantly, so the code under test cannot swallow it
    inside an `except OSError` or a bare `except Exception` and carry on
    thinking it sent something."""
    assert issubclass(network_guard.error_class, AssertionError)
    assert not issubclass(network_guard.error_class, OSError)


def test_the_error_says_how_to_proceed(network_guard):
    """A bare "a socket was opened" with no next step is close to useless when
    you are reading five hundred test names. The message names both escapes:
    inject a fake client, or mark the test."""
    with pytest.raises(network_guard.error_class) as exc:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    message = str(exc.value)
    assert "inject a fake" in message
    assert "allow_network" in message


def test_the_block_can_be_lifted_and_put_back(network_guard):
    """The mechanism behind the marker, exercised directly rather than through
    two ordered tests, which would make this file's result depend on collection
    order. The originals are read from the guard rather than from
    `socket.socket.connect`, because at this point that attribute is already
    the blocker, and comparing a blocker to itself proves nothing."""
    real_connect = network_guard.originals["connect"]

    network_guard.install(blocking=False)
    assert socket.socket.connect is real_connect, "lifting should restore the real one"
    assert network_guard.is_blocked() is False

    network_guard.install(blocking=True)
    assert network_guard.is_blocked() is True
    with pytest.raises(network_guard.error_class):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)


def test_the_block_is_back_on_after_the_previous_test(network_guard):
    """The previous test lifts and restores the block itself. This one runs
    immediately after and must find it shut, which is the cheapest available
    check that a marked test cannot leak an open door into its neighbour."""
    assert network_guard.is_blocked() is True
    with pytest.raises(network_guard.error_class):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)


def test_the_marker_is_registered_so_a_typo_is_an_error(pytestconfig):
    """pytest silently ignores an unregistered marker, so a misspelled
    `allow_network` would leave a test quietly blocked rather than telling you
    the name is wrong. Registering it in pyproject.toml is what makes a typo
    loud."""
    markers = pytestconfig.getini("markers")
    assert any("allow_network" in m for m in markers), (
        "allow_network is not registered in pyproject.toml, so pytest will "
        "ignore the marker instead of warning about it"
    )


def test_the_only_module_that_opts_out_is_the_one_that_binds_loopback():
    """The opt-out should be rare and visible. If this fails, some other file
    has started taking loopback, which is a decision someone should make
    deliberately rather than inherit from a copy-pasted marker."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        marked = False
        for node in ast.walk(tree):
            # a module-level `pytestmark = pytest.mark.allow_network`
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "pytestmark":
                        marked = True
            # a decorated test carrying the marker
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if "allow_network" in ast.dump(decorator):
                        marked = True
        if marked:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == ["tests/web/test_server.py"], (
        f"unexpected modules taking the allow_network exemption: {offenders}. "
        "Only tests that genuinely need loopback should be exempt."
    )


def test_the_loopback_exemption_is_actually_load_bearing():
    """The module-level marker on test_server.py is only honest if the tests in
    it really do connect. If they stopped needing loopback, the exemption
    should be deleted, not left in place where it quietly widens what the guard
    does not catch."""
    server_tests = (REPO_ROOT / "tests" / "web" / "test_server.py").read_text(
        encoding="utf-8-sig"
    )
    assert "urllib.request.urlopen" in server_tests, (
        "test_server.py no longer opens a loopback connection, so its "
        "allow_network exemption should be removed"
    )
    assert "make_server" in server_tests
