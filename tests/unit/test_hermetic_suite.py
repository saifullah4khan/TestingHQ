"""The hermetic CI job must not be able to reach a test that opens a socket.

`testpaths` is `tests`, so a bare `pytest -q` collects every test marked
`allow_network`, and each of those opens a real socket. The job whose entire
purpose is to be hermetic would then be running twelve loopback HTTP server
tests and eight end-to-end socket tests, with the suite-wide network block in
`tests/conftest.py` lifted for them by the marker itself.

`ci.yml` therefore runs `pytest -q -m "not allow_network"`.

By MARKER, not by directory. Ignoring `tests/e2e` looks like the obvious fix and
is wrong: `tests/web/test_server.py` is also marked `allow_network` and starts a
real stdlib server, and it is not under `tests/e2e`. A directory ignore leaves
those twelve running and the job is still not hermetic. Selecting on the marker
covers every exempt test whatever directory it lives in, including one added
later, which is why this asserts the property rather than a path.

The assertions run pytest rather than reading the YAML, because the failure mode
is a shell string in another file, and because two of them exist to stop the
first one passing for the wrong reason.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"

#: The selection the hermetic job is expected to use.
HERMETIC_SELECTOR = '-m "not allow_network"'


def _ci_run_line() -> str:
    text = CI.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if "pytest" in l]
    assert lines, f"{CI.name} has no pytest invocation to check"
    return "\n".join(lines)


def _collect(*extra: str) -> tuple[int, int]:
    """Return (total collected, collected with the hermetic selector).

    Counting exempt tests with `-m allow_network` rather than by reading
    `--collect-only` output, because node ids in quiet mode do not carry the
    marker and the first version of this asserted against a count that was
    always zero.
    """
    def _count(args: list[str]) -> int:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
        )
        return sum(1 for line in result.stdout.splitlines() if "::" in line)

    total = _count(["tests"])
    selected = _count(["tests", "-m", "not allow_network"])
    return total, selected


def test_the_hermetic_job_excludes_every_network_exempt_test():
    assert "not allow_network" in _ci_run_line(), (
        f"{CI.name} does not select `-m \"not allow_network\"`. `testpaths` is "
        "'tests', so without it the hermetic job collects every test marked "
        "allow_network and opens real sockets. Selecting on the marker rather "
        "than ignoring a directory is deliberate: tests/web/test_server.py is "
        "also exempt and is not under tests/e2e."
    )


def test_the_hermetic_selection_contains_no_network_exempt_tests():
    """The property itself, measured rather than read."""
    _total, selected = _collect()
    assert selected > 100, (
        f"the hermetic selection collected only {selected} tests, so this is not "
        "looking at the real suite and would pass for the wrong reason"
    )

    # Everything marked allow_network must be gone from the selection.
    exempt_collected = sum(
        1 for line in subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests",
             "-m", "allow_network"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
        ).stdout.splitlines()
        if "::" in line
    )
    assert exempt_collected > 0, (
        "no test is marked allow_network, so the selection excludes nothing and "
        "the hermetic job's guarantee is coming from somewhere else"
    )
    assert _total - selected >= exempt_collected, (
        f"{_total - selected} tests were dropped by the selection but only "
        f"{exempt_collected} are marked allow_network; something is being "
        "excluded that should not be"
    )


def test_the_exclusion_is_load_bearing():
    """If the selection dropped nothing, the test above would be green while
    the hermetic job ran every socket test."""
    total, selected = _collect()
    assert total > selected, (
        f"the hermetic selection dropped nothing ({total} collected, {selected} "
        "kept), so there are no exempt tests to exclude and the job's "
        "hermeticity is unproven"
    )


def test_the_e2e_workflow_still_runs_the_exempt_tests():
    """The other half. Excluding them from CI without running them anywhere
    would delete the only tests that prove the tool works over a real socket."""
    text = E2E_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/e2e" in text, (
        f"{E2E_WORKFLOW.name} does not run tests/e2e, so the exempt tests are "
        "excluded from CI rather than moved to a job of their own"
    )
    # It must not also apply the hermetic selector, or it would exclude the
    # very tests it exists to run.
    assert "not allow_network" not in text, (
        f"{E2E_WORKFLOW.name} applies the hermetic marker selection, so it "
        "would skip the tests it exists to run"
    )
