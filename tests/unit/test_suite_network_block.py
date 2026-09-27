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


def test_the_error_is_not_swallowable_by_except_exception(network_guard):
    """The class must sit outside `except Exception`, or the guard is
    decorative.

    This is not hypothetical. `NetworkBlocked` was an `AssertionError`
    subclass on the reasoning that an `except Exception` in the code under test
    could not catch it. `AssertionError` IS an `Exception`.
    `core/transport.py` wraps every `client.send()` in `except Exception` so
    that any transport failure is reportable as a result, which is right for
    production. That clause swallowed the block: `post()` returned a result
    with `status=None` and the message in `error`, the caller recorded a
    timeout, and the test passed green.

    Asserted directly rather than through a real send, because the direct
    assertion is the invariant and the end-to-end one below is the symptom.
    """
    assert not issubclass(network_guard.error_class, Exception), (
        "NetworkBlocked must not derive from Exception, or any `except "
        "Exception` in the code under test can swallow it and a hermetic test "
        "will pass while reaching the network"
    )
    # Still an exception pytest reports, and still catchable deliberately.
    assert issubclass(network_guard.error_class, BaseException)
    with pytest.raises(network_guard.error_class):
        try:
            raise network_guard.error_class("deliberate")
        except Exception:  # noqa: BLE001 - the point of the test
            pytest.fail("caught by except Exception, which is the bug")


def test_the_send_path_raises_rather_than_returning_a_result(network_guard):
    """The symptom the assertion above protects, end to end through the real
    transport.

    `post()` with no injected client, inside a suite that blocks the network,
    must RAISE. It must not come back as a `TransportResult` with a null
    status, because that is what lets a hermetic test pass while its code
    under test reached the network.

    Before the fix this returned a result whose `error` held the guard's own
    message, and a record built from it classified as `clean_failed`, so a
    test could assert that a payload failed as expected and be right when the
    real reason was that it never left the machine.
    """
    from testinghq.blast.generate import generate_corpus
    from testinghq.core.transport import post

    email = generate_corpus(7, 1)[0]

    with pytest.raises(network_guard.error_class):
        post(email, "http://127.0.0.1:9/intake")


def test_the_corrupted_send_path_raises_too(network_guard):
    """`blast fire --send` reaches the network through the same `post()`, with
    a real corrupted payload rather than a clean one. The path a user runs gets
    its own assertion rather than being assumed to follow from the unit above.
    """
    from testinghq.blast.corrupt import DEFAULT_MIX, corrupt_corpus
    from testinghq.blast.generate import generate_corpus
    from testinghq.core.transport import post

    corrupted = corrupt_corpus(generate_corpus(7, 3), 7, DEFAULT_MIX)

    for email, _recipe in corrupted:
        with pytest.raises(network_guard.error_class):
            post(email, "http://127.0.0.1:9/intake")


def test_the_barrage_send_path_raises_too(network_guard):
    """Barrage reaches the network the same way, through the same
    `transport.post`. Checking only Blast would leave half the product's send
    paths unverified."""
    from testinghq.barrage.fire import make_send_fn
    from testinghq.blast.generate import generate_corpus

    pool = generate_corpus(11, 2)
    send_fn = make_send_fn(pool, "http://127.0.0.1:9/intake")

    with pytest.raises(network_guard.error_class):
        send_fn(0)


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
