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
ITEM = re.compile(r"^(?P<indent>\s*)- \[(?P<state>[ xX])\] (?P<title>.*)$")
BACKTICKED = re.compile(r"`([^`]+)`")
LEADING_TAGS = re.compile(r"^\[[A-Z]\](?:\[[A-Z]\])*\s*")

#: How much of two item titles has to overlap before "same subject" is claimed
#: from a shared path alone. Calibrated so that a real contradiction fires and
#: the legitimate case of two different tasks touching one file does not.
TITLE_OVERLAP_THRESHOLD = 0.6

#: Stop words, removed before comparing titles so that "the", "and", "to" and
#: friends do not manufacture overlap between unrelated items.
_NOISE = frozenset(
    """a an and are as at be by for from in into is it its of on onto or that the
    to with without so not now out up over under then than these this those""".split()
)


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


# ---------------------------------------------------------------------------
# The contradiction guard.
#
# Everything above catches a single claim that has stopped being true, and only
# when the claim declared a falsifier. It cannot catch a claim that is still
# true, contradicted by another claim about the same thing. That is not a
# hypothetical: merging #21 through #27 in September 2026 resolved the UI v1
# conflict by concatenating both sides instead of choosing, and main spent a
# while carrying this:
#
#   - [x] [B][M] Swap `web/adapter.py` from the fixture stand-in to the real engine.
#   - [x] [B][M] Delegate `web/expectations.py` to `core/report.py`. DONE.
#   - [ ] [B][M] Delegate `web/expectations.py` to `core/report.py`. The two copies
#   - [ ] [B][M] Swap `web/adapter.py` from the fixture stand-in to the real engine.
#   - [ ] [B][M] Delegate `web/expectations.py` to `core/report.py`. The two copies
#
# Every sentence in that was once true. Both stale-claim guards passed
# throughout, because the contradiction was between entries rather than within
# one, and a coder reading it finds "NOT DONE and NOT BLOCKED, this is the
# highest-value item available" sitting directly under an entry saying it was
# done. The instruction at the top of the file is "pick the highest-priority
# not-done item in your lane", so that is a live instruction to redo finished
# work.
# ---------------------------------------------------------------------------


def _items():
    """Parse the backlog into (line, done, title, backticked_paths) records.

    A top-level `- [x] ...` starts an item; following indented non-blank lines
    are its body, which is where the backticked paths usually are, because the
    title line is short and the detail is not.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    items = []
    current = None
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            current = None
            continue
        match = ITEM.match(line)
        if match and not match.group("indent"):
            title = match.group("title")
            current = {
                "line": line_no,
                "done": match.group("state").lower() == "x",
                "title": title,
                # The title's own backticks count. Dropping them means an item
                # whose only reference to a file is in its title is grouped
                # under no path at all, and the whole comparison silently
                # misses it. Bodies are added below.
                "paths": set(BACKTICKED.findall(title)),
            }
            items.append(current)
            continue
        if current is not None and line.strip() and not line.startswith(("-", "#", "|")):
            current["paths"].update(BACKTICKED.findall(line))
    return items


def _subject(title):
    """A comparable identity for an item: leading lane tags dropped, backticks
    and punctuation removed, lowercased. `[A][M] Swap `web/adapter.py` ...` and
    `Swap `web/adapter.py` ...` are the same subject."""
    return re.sub(r"[^a-z0-9]+", " ", LEADING_TAGS.sub("", title).replace("`", "").lower()).strip()


def _content_words(title):
    return {w for w in _subject(title).split() if w not in _NOISE}


def _overlap(a, b):
    left, right = _content_words(a), _content_words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_code_reference(path):
    """Backticked spans that name a file, as opposed to a setting, a category,
    a flag, or a field. Only files are treated as an item's subject: two items
    both mentioning `clean` are not about the same thing, two items both
    mentioning `web/adapter.py` very plausibly are."""
    return "/" in path or path.endswith((".py", ".md", ".toml", ".json", ".js", ".html"))


def test_no_item_is_marked_both_done_and_not_done():
    """The load-bearing new test. Two entries about the same thing, in
    opposite states, is a file that is lying to whoever reads it."""
    items = _items()
    by_subject = {}
    for item in items:
        by_subject.setdefault(_subject(item["title"]), []).append(item)

    contradictions = []
    for subject, group in by_subject.items():
        if not subject:
            continue
        states = {g["done"] for g in group}
        if len(states) > 1:
            rendered = ", ".join(
                f"{BACKLOG.name}:{g['line']} [{'x' if g['done'] else ' '}] {g['title'][:60]}"
                for g in sorted(group, key=lambda g: g["line"])
            )
            contradictions.append(f"same subject in both states: {subject!r} -> {rendered}")

    assert not contradictions, (
        "the backlog marks the same work both done and not done, which makes the "
        "'pick the highest-priority not-done item' instruction at the top of the "
        "file point at finished work: " + "; ".join(contradictions) + ". Keep one "
        "entry and one state; delete the stale copy rather than unticking the "
        "current one."
    )


def test_no_code_file_is_claimed_done_by_one_item_and_open_by_another():
    """The subtler version of the same defect: two entries that word the same
    task differently, so their titles do not match, but which name the same
    file and disagree about its state.

    Needs an overlap threshold rather than a shared path alone, because two
    genuinely different tasks can both mention one file. "Transport in
    core/transport.py" and "Document core/transport.py" are not a
    contradiction; "Swap `web/adapter.py` onto the engine" and "Swap
    `web/adapter.py` from the fixture stand-in to the real engine" are.

    Known limitation, measured rather than assumed: the comparison uses the
    item's first line only, because a body is long enough that two entries
    about the same file would share most of their words and the threshold
    would stop discriminating. A title that wraps loses the wrapped words, so
    the same pair of entries scores lower the more the first line is split. A
    reworded title that also wraps can therefore fall under the threshold.
    The identical-subject test above has no such weakness, which is why the
    exact-contradiction case is its job and this one is the net.
    """
    items = _items()
    by_path = {}
    for item in items:
        for path in item["paths"]:
            if _is_code_reference(path):
                by_path.setdefault(path, []).append(item)

    contradictions = []
    for path, group in sorted(by_path.items()):
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                if left["done"] == right["done"]:
                    continue
                if _subject(left["title"]) == _subject(right["title"]):
                    continue  # already caught by the identical-subject test
                score = _overlap(left["title"], right["title"])
                if score < TITLE_OVERLAP_THRESHOLD:
                    continue
                contradictions.append(
                    f"{path!r} is done at {BACKLOG.name}:{left['line']} "
                    f"({left['title'][:50]!r}) and open at "
                    f"{BACKLOG.name}:{right['line']} ({right['title'][:50]!r}), "
                    f"titles {score:.0%} similar"
                )

    assert not contradictions, (
        "the backlog has two entries about the same file that disagree about "
        "whether it is done: " + "; ".join(contradictions) + ". Keep one entry."
    )


def test_the_contradiction_guard_understands_the_file_it_reads():
    """A guard that finds nothing because it parsed nothing is
    indistinguishable from a guard that is passing.

    Asserted against the real file, so the item count and the done/open split
    have to stay plausible. If a future edit changes the list syntax these
    counts collapse to near zero and this fails rather than the two tests
    above passing vacuously.
    """
    items = _items()
    assert len(items) >= 20, (
        f"only {len(items)} items parsed out of the backlog; the item syntax "
        "this guard depends on has probably changed"
    )
    assert any(i["done"] for i in items), "no items parsed as done"
    assert sum(1 for i in items if i["paths"]), "no item captured a code reference"
