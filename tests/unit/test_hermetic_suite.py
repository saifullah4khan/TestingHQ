"""The two CI jobs must cover the suite exactly once between them.

`ci.yml` runs `pytest -q -m "not allow_network"` and `e2e.yml` runs
`pytest -q -m allow_network`. Those two selections partition the suite, and
between them they must account for every test exactly once.

The failure this exists for is quiet in both directions. If the hermetic job
stops selecting on the marker, it collects tests marked `allow_network`, lifts
the suite-wide network block for them, and opens real sockets in the job whose
purpose is to be hermetic. If the e2e job runs a directory rather than the
marker, the exempt tests outside that directory are run by neither job: the
hermetic job skips them and the e2e job never asks for them. That is what
happened when e2e.yml ran `pytest -q tests/e2e`, which left 23 of the 31
exempt tests unrun in CI while every check stayed green.

Both directions are caught by arithmetic on what pytest actually collects,
rather than by reading the two YAML files for a string. Counting the partitions
is the property; the exact command each job uses is an implementation detail
that a reformat can change without changing what is covered.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"


def _collect(*selector: str) -> int:
    """How many tests pytest collects with this selection."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests", *selector],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=600,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"collection failed for {selector}:\n{result.stdout[-1500:]}\n"
            f"{result.stderr[-1500:]}"
        )
    return sum(1 for line in result.stdout.splitlines() if "::" in line)


def _pytest_commands(path: Path) -> list[str]:
    """The pytest invocations in a workflow, as written.

    Command lines only, never the whole file. A guard that greps the file for
    `not allow_network` is satisfied by a comment explaining why the marker is
    there, which is exactly what happened: removing the marker from the command
    while leaving the comment above it left the check green.
    """
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("pytest")
    ]


def test_the_two_jobs_partition_the_suite_exactly():
    """The core assertion, and the one that fails for either mistake.

    Counted, not read. `hermetic + exempt == total` fails if a test is in both
    selections, in neither, or if a job's selection has drifted.
    """
    total = _collect()
    hermetic = _collect("-m", "not allow_network")
    exempt = _collect("-m", "allow_network")

    assert total > 100, (
        f"only collected {total} tests, so this is not looking at the real "
        "suite and every other assertion here would pass for the wrong reason"
    )
    assert exempt > 0, (
        "no test is marked allow_network, so the e2e job selects nothing and "
        "the split between the two jobs does not exist"
    )
    assert hermetic + exempt == total, (
        f"the two CI jobs do not cover the suite exactly once between them: "
        f"{hermetic} hermetic + {exempt} exempt = {hermetic + exempt}, but the "
        f"suite has {total}. A difference of {abs(hermetic + exempt - total)} "
        "means tests are either run by neither job or run by both."
    )


def test_the_e2e_job_selects_by_marker_not_by_directory():
    """The half that was wrong. Selecting a directory runs that directory and
    nothing else, and the exempt tests outside it are then run by nobody."""
    commands = _pytest_commands(E2E_WORKFLOW)
    assert commands, f"{E2E_WORKFLOW.name} has no pytest invocation"
    for command in commands:
        assert "-m allow_network" in command, (
            f"{E2E_WORKFLOW.name} does not select on the allow_network marker, "
            "so exempt tests outside the directory it names are run by neither "
            f"job: {command!r}"
        )
        assert "not allow_network" not in command, (
            f"{E2E_WORKFLOW.name} applies the hermetic selection and would skip "
            f"the tests it exists to run: {command!r}"
        )
        # A path argument alongside the marker would silently narrow the
        # selection back to a directory.
        assert "tests/e2e" not in command, (
            f"{E2E_WORKFLOW.name} names a directory as well as the marker, so "
            f"the selection is narrower than every exempt test: {command!r}"
        )


def test_the_hermetic_job_selects_by_marker():
    commands = _pytest_commands(CI)
    assert commands, f"{CI.name} has no pytest invocation"
    for command in commands:
        assert 'not allow_network' in command, (
            f"{CI.name} does not exclude the network-exempt tests, so the job "
            f"whose purpose is to be hermetic will open real sockets: {command!r}"
        )
        # And it must not also name a path, or the exclusion is only partial
        # and the split between the two jobs is harder to reason about.
        assert "--ignore" not in command and "tests/e2e" not in command, (
            f"{CI.name} narrows the hermetic run by path as well as by "
            f"marker: {command!r}"
        )


def test_a_local_run_still_covers_everything():
    """Neither selection is in `addopts`, so `pytest -q` locally runs the whole
    suite including the socket tests. Worth pinning, because moving the marker
    into `addopts` would make a local run quietly match CI and stop being a
    way to check the socket tests without a separate command."""
    total = _collect()
    assert total > 100
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("addopts"):
            assert "allow_network" in line, (
                "addopts applies to every run including local ones; the marker "
                "selection belongs in the workflow commands"
            )
