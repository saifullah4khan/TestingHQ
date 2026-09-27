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

THE TIME BUDGET, and why it is here rather than in a file. A hermetic test that
waits on a real clock is a test that makes the suite slower every run and a green
tick that hides why. Three of them did: the `verify check` tests, which polled a
real five-second quiet window, and one documented command the readback-examples
guard ran as a subprocess. Seventeen seconds of a twenty-six second hermetic run,
and every test still said nothing was left to do.

A per-test budget catches the next one at the point it lands. It is session-wide
rather than a test because a test can only see what ran before it, and
"slowest so far" is a different claim from "slowest". The exemption list is
asserted to have no stale entries, so it cannot rot into a blanket.
"""
from __future__ import annotations

import os
import socket
import time
from typing import Dict, List, Tuple

import pytest

#: Attribute names restored and blocked on `socket.socket`. Captured once at
#: import time so the block is reversible and idempotent.
_PATCHED = ("connect", "connect_ex")
_ORIGINALS: dict = {}

#: Wall-clock budget for one hermetic test. A test that exceeds it is either
#: waiting on a real clock or doing real work, and both belong in a place that
#: says so.
#:
#: Two seconds, not one, and the number comes from the failure it has to catch
#: rather than from a round number. The bug that produced this guard was three
#: tests each waiting 5.5s, so anything under about 3s catches it. Meanwhile a
#: pytest subprocess costs roughly 0.8s on a developer machine before it runs a
#: single test, and about twice that on a CI runner, so a 1s budget would leave
#: the guard's own tests sitting at the threshold and failing in CI for being
#: the thing that proves the guard works. A guard that is flaky is worse than no
#: guard, because it gets switched off.
SLOW_TEST_BUDGET = 2.0

#: Overrides the budget, so a test can prove the guard without waiting a real
#: second and without the suite taking one per proof. Read from the environment
#: rather than as a pytest option because the consumer is a subprocess: an
#: option would have to be threaded through argv for no benefit.
SLOW_TEST_BUDGET_ENV = "TESTINGHQ_SLOW_TEST_BUDGET"

#: Tests allowed to exceed the budget, by node id, with the reason. Kept small
#: on purpose: every entry is a test that shells out, and a list of ten is a
#: list of ten.
SLOW_TEST_EXEMPTIONS: Dict[str, str] = {
    "tests/unit/test_hermetic_suite.py::test_the_two_jobs_partition_the_suite_exactly": (
        "runs pytest --collect-only three times to count what each CI selection "
        "collects. Counting is the assertion; there is no cheaper way to count "
        "what pytest collects."
    ),
    "tests/unit/test_hermetic_suite.py::test_a_local_run_still_covers_everything": (
        "collects once, for the same reason: what pytest collects by default is "
        "the thing being asserted about."
    ),
}

#: Below this many tests, the run is a targeted one (`pytest -k`, a single node)
#: rather than a suite run, and the budget is not enforced. A deliberate
#: `pytest tests/unit/test_x.py::test_y` is debugging, not CI, and failing it
#: for being slow would train people to pass `-p no:...` and then stop reading.
SLOW_TEST_MIN_TESTS = 20

_TIMINGS: List[Tuple[float, str, bool]] = []
_COLLECTED: set = set()
_WHOLE_SUITE: set = set()


def slow_test_budget() -> float:
    """The budget for this run, from the environment or the default.

    A malformed override raises rather than falling back. A budget that quietly
    reverts to the default is the exact failure this guard exists to prevent,
    applied to the guard: it would look enforced, enforce nothing, and the
    reading of the environment variable would be the only way to tell.
    """
    raw = os.environ.get(SLOW_TEST_BUDGET_ENV)
    if raw is None or not raw.strip():
        return SLOW_TEST_BUDGET
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{SLOW_TEST_BUDGET_ENV}={raw!r} is not a number. Set it to a number "
            f"of seconds, or unset it to use the {SLOW_TEST_BUDGET}s default."
        ) from exc
    if value <= 0:
        raise ValueError(
            f"{SLOW_TEST_BUDGET_ENV}={raw!r} must be greater than zero; a budget of "
            "zero or less fails every test including the empty one"
        )
    return value


def pytest_collection_modifyitems(session, config, items):
    """Record every collected node id, and whether this is a whole-suite run.

    Both are needed. The node ids tell a stale exemption from one whose test did
    not run in this invocation, and the whole-suite flag is what says whether
    "not collected" means anything at all: in `pytest tests/unit/test_x.py` the
    rest of the suite is not collected on purpose, so calling its exemptions
    stale would make every partial run fail.
    """
    _COLLECTED.update(item.nodeid for item in items)
    # CI invokes `pytest -q -m "not allow_network"` with no positional argument,
    # so an empty `config.args` is the whole suite and nothing else is.
    _WHOLE_SUITE.add(not config.args)


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Time every test.

    Measured around the call phase rather than taken from pytest's own duration
    report, because the report is only printed under `--durations` and a guard
    that runs only when someone remembered a flag is not a guard.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        exempt = item.get_closest_marker("allow_network") is not None
        _TIMINGS.append((time.perf_counter() - started, item.nodeid, exempt))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the session if a hermetic test blew the time budget.

    `session.exitstatus` is set rather than a value returned, because that is
    the one the runner actually uses.

    Two directions are checked, and the second is the one that keeps the first
    honest. A stale exemption is reported, because a list of exemptions that
    nobody prunes is a list that eventually contains every slow test and a guard
    that no longer guards.
    """
    if exitstatus != 0:
        return
    budget = slow_test_budget()
    measured = [(d, node) for d, node, exempt in _TIMINGS if not exempt]
    if len(measured) < SLOW_TEST_MIN_TESTS:
        return

    ran = {node for _d, node in measured}
    # Only a whole-suite run can say an exemption is stale. A targeted run
    # collects part of the suite on purpose, and reporting the rest as stale
    # would make `pytest tests/unit/test_one_thing.py` fail every time.
    stale = sorted(set(SLOW_TEST_EXEMPTIONS) - _COLLECTED) if True in _WHOLE_SUITE else []
    unexercised = sorted(set(SLOW_TEST_EXEMPTIONS) & _COLLECTED - ran)
    slow = sorted(
        ((d, node) for d, node in measured if d > budget),
        reverse=True,
    )
    legitimately_slow = [(d, node) for d, node in slow if node in SLOW_TEST_EXEMPTIONS]
    unexpected = [(d, node) for d, node in slow if node not in SLOW_TEST_EXEMPTIONS]

    if stale or unexpected:
        lines = ["", "test timing budget:", ""]
        for node in stale:
            lines.append(
                f"  EXEMPTION IS STALE  {budget:.3g}s budget, {node}"
            )
            lines.append(f"      reason given: {SLOW_TEST_EXEMPTIONS[node]}")
            lines.append(
                "      no test with that id was collected, so it was renamed, "
                "deleted, or the exemption was copied here."
            )
        for duration, node in unexpected:
            lines.append(
                f"  TOO SLOW  {duration:.2f}s over a {budget:.3g}s budget: {node}"
            )
        if legitimately_slow:
            lines.append("")
            lines.append("  allowed over budget, with the reason on file:")
            for duration, node in legitimately_slow:
                lines.append(
                    f"    {duration:.2f}s  {node}\n"
                    f"      {SLOW_TEST_EXEMPTIONS[node]}"
                )
        if unexercised:
            lines.append("")
            lines.append(
                f"  {len(unexercised)} exemption(s) were not exercised in this "
                "run, so they are unproven here rather than stale."
            )
        lines.append("")
        lines.append(
            "  A hermetic test that waits on a real clock makes every run slower "
            "and reports nothing. Inject a clock and a sleep, or ask the code "
            "under test for a zero window: the poll semantics are asserted in "
            "tests/unit/test_readback_poll.py on a virtual clock, so the timing "
            "of a test that is not about timing is only ever overhead."
        )
        if unexpected:
            lines.append(
                "  To allow one, add it to SLOW_TEST_EXEMPTIONS in "
                "tests/conftest.py with the reason it is slow."
            )
        for line in lines:
            print(line)
        session.exitstatus = 1
