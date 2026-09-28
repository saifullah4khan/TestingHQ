"""The README's Status section has to be true.

This exists because it was not. The section said "All five tools work end to
end", then described six of them, and omitted Loop, Steady and Compare, which
had shipped and shipped before the sentence was written. It also claimed a test
count nobody was maintaining, so the number it gave was wrong within weeks of
being correct.

A status section is the part of a README a reader trusts most and the part
nothing checks, which makes drift there the most expensive kind: a reader who
catches one false claim in a document about what the tool can do has a reason
to distrust the rest, including the parts that are true and that took a year to
build.

The count is derived from the CLI rather than written by hand, so adding a
subcommand without adding a paragraph here fails the build.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from testinghq import cli

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"


def _status_section() -> str:
    text = README.read_text(encoding="utf-8")
    assert "## Status" in text, "the README has no Status section"
    start = text.index("## Status")
    rest = text[start + len("## Status"):]
    end = rest.find("\n## ")
    return rest[:end] if end != -1 else rest


def _registered_subcommands() -> set[str]:
    """Top-level commands the CLI actually accepts.

    Read from the live parser rather than a list kept alongside it, because a
    list is exactly what drifts.
    """
    import argparse

    parser = cli.build_parser()
    subparsers = [
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert subparsers, "the CLI has no subparsers, which cannot be right"
    return set(subparsers[0].choices)


def _bold_leads_in(section: str) -> set[str]:
    """The `**Name**:` paragraph leads in the Status section.

    Matched on the whole bold run, not on `**Name:`, because the section is
    written as `**Blast**: ...` and a pattern that only matched the other form
    would find nothing at all and pass vacuously.

    Backticks are stripped and only the first word is kept, so a lead written as
    ``**`config validate`**:`` registers as `config` and matches the subcommand
    the CLI actually creates. A command with a subcommand of its own is named by
    its top-level name here for the same reason the reader types it that way.
    """
    found = set()
    for raw in re.findall(r"\*\*([^*\n]+)\*\*", section):
        cleaned = raw.strip().strip("`").strip()
        if cleaned:
            found.add(cleaned.split()[0].casefold())
    return found


def test_every_registered_command_appears_in_the_status_section():
    leads = _bold_leads_in(_status_section())
    assert leads, (
        "no bold paragraph leads were found in the Status section, so this "
        "test would pass without checking anything"
    )
    missing = {c for c in _registered_subcommands() if c.casefold() not in leads}
    assert not missing, (
        f"the Status section does not mention {sorted(missing)}. A command the "
        "CLI registers is something this package ships, and a status section "
        "that omits one is not a status."
    )


def test_the_status_section_does_not_claim_a_tool_count_it_cannot_keep():
    """No hand-maintained "all N tools" sentence.

    A count in prose is a number nobody updates, and the number this README used
    to carry was wrong. The count is derived from the CLI in the test above, so
    a sentence like "all N tools" is redundant as well as unmaintained: it can
    only ever contradict the list right underneath it.
    """
    section = _status_section().lower()
    for number in ("five", "six", "seven", "eight", "nine", "ten", "eleven",
                   "twelve"):
        assert f"all {number}" not in section, (
            f"the Status section says 'all {number}'. A count in prose goes "
            "stale silently, and the list below it is checked against the CLI "
            "by test_every_registered_command_appears_in_the_status_section."
        )


def test_no_command_is_claimed_as_in_progress_and_also_as_shipped():
    """The contradiction this section had, in one line each and 150 lines apart.

    The Tools section described Blast as "in progress" while the Status section
    said Blast "ships", and nothing anywhere said which was true. A reader who
    finds that once has to re-read everything else.
    """
    text = README.read_text(encoding="utf-8")
    assert "in progress" not in text.lower(), (
        "the README says something is in progress. Every tool listed here "
        "ships and is tested end to end; if one genuinely does not, say so in "
        "the Status section rather than in the heading, so there is one place "
        "to look."
    )


def test_the_status_section_does_not_point_at_a_workflow_document():
    """It used to end with a pointer to a 27 KB internal backlog, which is
    process documentation and was not something a user of the package could
    act on."""
    section = _status_section()
    for gone in ("BLAST_BACKLOG", "docs/agents", "docs/fleet", "GOALS.md",
                 "FLEET.md", "REQUESTS.md"):
        assert gone not in section, (
            f"the Status section points at {gone!r}, which is not shipped "
            "documentation. If a milestone matters to a user, write it here."
        )


def test_the_removed_workflow_documents_are_really_gone():
    """And not merely unlinked.

    A README that stops mentioning a document leaves the document in place,
    where it is still one `git grep` away from being read as part of the
    product.
    """
    for gone in (
        "docs/agents/BLAST_BACKLOG.md",
        "docs/agents/FLEET.md",
        "docs/agents/GOALS.md",
        "docs/agents/REQUESTS.md",
        "docs/agents/notes/coder-a-NOTE.md",
        "docs/agents/notes/coder-b-NOTE.md",
        "docs/fleet/PROTOCOL.md",
    ):
        assert not (REPO_ROOT / gone).exists(), (
            f"{gone} is still in the tree. It is development scaffolding, not "
            "documentation for a user of the package."
        )


def test_the_backlog_freshness_test_is_gone_with_the_backlog():
    """Every test in it read docs/agents/BLAST_BACKLOG.md and asserted things
    about that file's prose. None touched a line of shipped code, so with the
    backlog removed it has nothing left to check."""
    assert not (REPO_ROOT / "tests" / "test_backlog_freshness.py").exists(), (
        "test_backlog_freshness.py asserted properties of a deleted document. "
        "Keeping it would mean keeping a test suite that protects nothing."
    )


def test_no_shipped_document_references_a_removed_one():
    """The failure mode of deleting a file is the dangling reference, and it is
    silent: nothing breaks, the link is just wrong."""
    import subprocess

    result = subprocess.run(
        ["git", "grep", "-n", "-e", "BLAST_BACKLOG", "-e", "docs/agents",
         "-e", "docs/fleet", "-e", "GOALS.md", "-e", "REQUESTS.md",
         "-e", "FLEET.md"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
    )
    assert result.returncode != 0 or not result.stdout.strip(), (
        "files still reference the removed workflow documents:\n"
        f"{result.stdout.strip()}"
    )


def test_the_barrage_paragraph_says_what_the_build_can_do():
    """The one place the README was right about a limitation, and the guard on
    it going stale in either direction.

    It is a claim about the code's capability, so it cannot be a fixed string
    and it cannot be absent. The check is the file, not the sentence: if
    executor.py exists and the section still says there is no executor, one of
    them is a bug, and this is the test that says which.
    """
    section = _status_section().lower()
    executor_exists = (
        REPO_ROOT / "testinghq" / "barrage" / "executor.py"
    ).exists()

    if executor_exists:
        assert "no executor" not in section, (
            "the Status section says Barrage has no executor, but "
            "testinghq/barrage/executor.py exists. Either the paragraph is "
            "stale or the executor was removed; one of those is a bug."
        )
    else:
        assert "no executor" in section, (
            "Barrage has no executor, and the Status section does not say so. "
            "This is the exact gap that let the README describe six tools in a "
            "section that said five."
        )
    assert "barrage" in section


def test_the_slow_retry_paragraph_appears_once():
    """It appeared verbatim twice: once correctly after the redeliver example,
    and once orphaned between the barrage block and the verify heading, where a
    reader would reasonably take it as being about barrage.

    Same class as the duplicate test functions: an exact copy of a paragraph is
    indistinguishable from one paragraph that happens to be pasted, and the
    orphan is the copy nobody chose.
    """
    text = README.read_text(encoding="utf-8")
    marker = "The slow-retry scenario is the duplicate half of a provider retry"
    assert text.count(marker) == 1, (
        f"the slow-retry paragraph appears {text.count(marker)} times. It "
        "belongs after the redeliver example, where it explains a scenario the "
        "reader has just been shown."
    )
