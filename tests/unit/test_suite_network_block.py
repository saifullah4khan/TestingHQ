"""The suite-wide network block in tests/conftest.py, and that every send
path in the product hits it rather than reaching a socket.

The guard is reached through the `network_guard` fixture, never by importing
conftest: pytest loads it as top-level `conftest`, and a second import would
carry a different `NetworkBlocked` class.
"""
import socket

import pytest


def test_the_block_is_in_force_and_cannot_be_swallowed(network_guard):
    """This test carries no `allow_network`, so the block applies to it, and
    its error must sit outside `except Exception`.

    `core/transport.py` wraps every `client.send()` in `except Exception` so a
    transport failure is reportable as a result. When `NetworkBlocked` was an
    `AssertionError` subclass, that clause swallowed the block: `post()`
    returned a result with `status=None`, the caller recorded a timeout, and a
    test that had reached for the network passed green.
    """
    with pytest.raises(network_guard.error_class) as exc:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    assert "socket.create_connection" in str(exc.value)
    assert not issubclass(network_guard.error_class, Exception)
    assert issubclass(network_guard.error_class, BaseException)


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


