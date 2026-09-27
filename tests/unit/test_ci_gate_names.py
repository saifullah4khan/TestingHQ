"""The CI gate's name is a contract with branch protection, and a contract
nobody checks is not a contract.

`main` is protected by the "protect main" ruleset, which requires a status
check whose context is exactly `tests`. GitHub matches that string, so a job
called `tests (py3.11)` does not satisfy a rule that says `tests`. The first
version of the interpreter matrix named its job after the matrix, the required
context stopped ever appearing, and the pull request sat blocked with every
visible check green. Nothing in the workflow was wrong. The name was.

So this asserts the shape rather than the contents: there is a job named
exactly `tests`, and it `needs` the matrix. Both properties have to hold for
the gate to be either satisfiable or meaningful.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
SECURITY = REPO_ROOT / ".github" / "workflows" / "security.yml"

#: The context strings the ruleset requires. Asserted here rather than read
#: from the API so that the suite is hermetic and the check still works on a
#: fork. If the ruleset changes, this has to change with it, which is the
#: point: a rename on one side only is what caused the problem.
#:
#: Two, not one. `tests` is the hermetic suite. `end-to-end` covers the job
#: that opens real sockets, which used to be advisory: a red socket test
#: changed nothing about whether a pull request could merge.
REQUIRED_CONTEXTS = {"tests", "end-to-end"}


def _jobs(text: str) -> dict[str, dict[str, str]]:
    """Job name -> the fields we care about.

    Hand-parsed. PyYAML is not a dependency, and adding one to read four
    strings out of a workflow file would be a poor trade for a project holding
    a hard line on third-party runtime dependencies.
    """
    jobs: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        job = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if job:
            current = job.group(1)
            jobs[current] = {}
            continue
        if current is None:
            continue
        needs = re.match(r"^    needs:\s*\[?(.*?)\]?\s*$", line)
        if needs:
            jobs[current]["needs"] = needs.group(1)
        name = re.match(r"^    name:\s*(.+)$", line)
        if name:
            jobs[current]["name"] = name.group(1).strip()
    return jobs


def _workflows() -> dict[str, dict[str, dict[str, str]]]:
    out = {}
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        out[path.name] = _jobs(path.read_text(encoding="utf-8"))
    return out


def test_a_job_exists_under_every_required_check_name():
    """The whole failure in one assertion.

    Branch protection requires a status check called exactly `tests`. If no job
    in any workflow has that context, the rule can never be satisfied and every
    pull request is blocked forever with nothing visibly wrong. The job's
    `name:` is what GitHub reports as the context, so the `name` is what is
    checked, not the YAML key.
    """
    contexts = set()
    for workflow, jobs in _workflows().items():
        for key, fields in jobs.items():
            contexts.add(fields.get("name", key))
    missing = REQUIRED_CONTEXTS - contexts
    assert not missing, (
        f"no CI job reports the status check name(s) {sorted(missing)}, which "
        "branch protection on main requires. A job whose name varies with a "
        "matrix, such as 'tests (py3.11)', does not satisfy a rule that says "
        f"'tests'. Jobs found: {sorted(contexts)}"
    )


def test_the_tests_gate_needs_the_interpreter_matrix():
    """A gate that needs nothing is a rubber stamp, and a gate that needs only
    one leg of a matrix is worse than no gate: it would go green on a broken
    interpreter."""
    jobs = _jobs(CI.read_text(encoding="utf-8"))
    assert "tests" in jobs, "the required job 'tests' is missing from ci.yml"

    needs = jobs["tests"].get("needs", "")
    assert needs, "the 'tests' gate needs no other job, so it cannot fail"

    assert "test-version" in needs, (
        f"the 'tests' gate needs {needs!r}, which does not include the "
        "interpreter matrix job 'test-version'. If the matrix job was renamed, "
        "the gate stops covering it and the required check goes green on a "
        "broken interpreter."
    )


def test_the_gate_fails_when_a_matrix_leg_fails():
    """`needs` alone is not enough. Without `if: always()` a skipped gate is
    reported as successful, which is the quiet version of the same bug."""
    text = CI.read_text(encoding="utf-8")
    block = text.split("  tests:", 1)[1].split("\n  [A-Za-z0-9_-]+:", 1)[0]
    assert "if: always()" in block, (
        "the 'tests' gate has no `if: always()`, so when a matrix leg fails the "
        "gate is skipped and GitHub reports a skipped job as successful"
    )
    assert "needs.test-version.result" in block, (
        "the gate does not inspect the matrix result, so a failing leg would "
        "not fail the gate"
    )


def test_the_end_to_end_gate_needs_its_matrix():
    """The same contract as the hermetic gate, for the job that opens sockets.

    Without it the required `end-to-end` context would be satisfied by a
    single leg, or by nothing at all, while the other interpreter's socket
    tests went red.
    """
    jobs = _jobs(E2E.read_text(encoding="utf-8"))
    assert "end-to-end" in jobs, (
        "the required job 'end-to-end' is missing from e2e.yml"
    )

    needs = jobs["end-to-end"].get("needs", "")
    assert needs, "the 'end-to-end' gate needs no other job, so it cannot fail"

    assert "e2e-version" in needs, (
        f"the 'end-to-end' gate needs {needs!r}, which does not include the "
        "e2e matrix job 'e2e-version'. If the matrix job was renamed, the gate "
        "stops covering it and the required check goes green while a socket "
        "test is failing."
    )


def test_the_end_to_end_gate_fails_when_a_leg_fails():
    """`if: always()` or the gate is skipped, and GitHub calls a skipped job
    successful, which is the same bug in a quieter form."""
    text = E2E.read_text(encoding="utf-8")
    block = text.split("  end-to-end:", 1)[1].split("\n  [A-Za-z0-9_-]+:", 1)[0]
    assert "if: always()" in block, (
        "the 'end-to-end' gate has no `if: always()`, so when a leg fails the "
        "gate is skipped and GitHub reports a skipped job as successful"
    )
    assert "needs.e2e-version.result" in block, (
        "the 'end-to-end' gate does not inspect the matrix result, so a "
        "failing leg would not fail the gate"
    )


def test_the_e2e_matrix_job_does_not_report_the_required_name_itself():
    """The half that is easy to get wrong in the other direction.

    If the matrix job kept the name `end-to-end`, then the gate below it would
    be a second job reporting the same context, and whichever finished last
    would decide the result. That is how a green tick hides a red leg.
    """
    jobs = _jobs(E2E.read_text(encoding="utf-8"))
    reported = [key for key, f in jobs.items() if f.get("name", key) == "end-to-end"]
    assert reported == ["end-to-end"], (
        f"jobs {reported} report the required context 'end-to-end'; exactly the "
        "gate may report it, and the matrix leg must keep a per-interpreter "
        "name"
    )


@pytest.mark.parametrize("name", sorted(REQUIRED_CONTEXTS))
def test_every_required_name_is_produced_by_exactly_one_job(name):
    """Two jobs reporting the same context is ambiguous: whichever finishes
    last decides the result. That is how a green tick hides a red leg."""
    matches = []
    for workflow, jobs in _workflows().items():
        for key, fields in jobs.items():
            if fields.get("name", key) == name:
                matches.append(f"{workflow}:{key}")
    assert len(matches) == 1, (
        f"the required check name {name!r} is produced by {matches}; exactly one "
        "job must report it"
    )
