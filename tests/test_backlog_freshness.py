"""Fail the build when the backlog makes a claim that main has already falsified.

docs/agents/BLAST_BACKLOG.md is the file the coders self-direct from. It has gone
stale twice in one day:

1. The 07:00 planner note said M1 was "not yet started" while 1,235 lines of it
   already sat on two agent branches. Nothing was scheduled to reconcile that
   before the overnight digest, and the planner does not run again until Monday,
   so it would have read as true for four days.
2. It was hand-corrected at 15:50. By 17:07 it was lying again: still saying M3
   was IN PROGRESS and Barrage was BLOCKED on M3, forty minutes after M3 merged
   and unblocked Barrage.

Both times the file was true when written. Both times the world moved and nothing
was watching. A rotted backlog is worse than an empty one, because the instruction
in it is "if your items are done or missing, pick the highest-priority not-done
item in your lane" - so a coder reads "do not claim this", finds nothing else
legitimate, and improvises.

The fix is not another hand-correction. It is to make the claims falsifiable. An
item that a future merge will invalidate declares the condition inline:

    <!-- stale-if-exists: testinghq/barrage/runner.py -->

and this test fails the moment that path exists. The build then forces someone to
update the sentence above it.

This is the same principle as PR #1, which proved the CI gate could go red before
anyone trusted it going green. A claim that cannot be proven wrong is not a
safeguard. It is just a sentence.
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "docs" / "agents" / "BLAST_BACKLOG.md"

MARKER = re.compile(r"<!--\s*stale-if-exists:\s*(?P<path>[^\s>]+?)\s*-->")


def _markers():
    """Yield (line_number, declared_path) for every staleness marker."""
    text = BACKLOG.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), 1):
        for match in MARKER.finditer(line):
            yield line_no, match.group("path")


def test_backlog_exists():
    """The coders self-direct from this file. Its absence is a fleet outage."""
    assert BACKLOG.is_file(), f"{BACKLOG} is missing; the coders have nothing to claim from"


def test_staleness_markers_point_at_real_repo_paths():
    """A marker naming a path that could never exist would never fire.

    A guard that cannot fail is indistinguishable from one that is passing, which
    is the exact failure PR #1 was built to prevent. A typo in a marker path
    silently disarms it, so require that each one is at least a plausible
    repo-relative path rather than an absolute path or a URL.
    """
    for line_no, path in _markers():
        assert not path.startswith(("/", "http://", "https://")), (
            f"{BACKLOG.name}:{line_no}: stale-if-exists marker must be a "
            f"repo-relative path, got {path!r}"
        )
        assert ".." not in pathlib.PurePosixPath(path).parts, (
            f"{BACKLOG.name}:{line_no}: stale-if-exists path must not escape the "
            f"repo, got {path!r}"
        )


def test_no_backlog_claim_is_already_falsified():
    """The load-bearing test. If a declared path exists, the claim above it is stale.

    Read the failure message literally: it is not telling you a test is broken, it
    is telling you the backlog is lying to your coders right now. Fix the backlog.
    Do not delete the marker to get green; that is the whole failure mode this
    exists to prevent, and it is how the file rotted the first two times.
    """
    stale = []
    for line_no, path in _markers():
        if (REPO_ROOT / path).exists():
            stale.append(f"{BACKLOG.name}:{line_no} claims something that {path} disproves")

    assert not stale, (
        "the backlog is out of date with main and will mislead the next coder that "
        "reads it: " + "; ".join(stale) + ". Update the claim, do not remove the marker."
    )
