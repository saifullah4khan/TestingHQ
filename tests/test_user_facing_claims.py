"""Stale-claim guards for the user-facing files.

`tests/test_backlog_freshness.py` guards the backlog. This guards the files a
user actually reads: `README.md`, `examples/README.md`, and `site/index.html`.

Same class of failure, different blast radius. The backlog rotting misleads
whoever is about to do work. A landing page rotting misleads everyone, and the
landing page is where a claim is most likely to be written optimistically and
corrected never.

The specific rot this exists for, found in review:

    site/index.html:279
    "closed-loop holds concurrency fixed and lets offered load self-limit"

`--concurrency` is inert in BOTH modes, because `testinghq` has no executor. The
public page was telling visitors that closed mode does something it does not.

Mechanism, and why it is a word list rather than a marker scheme. The backlog
can use inline `<!-- stale-if-exists: -->` markers because it is written by this
repo's own agents and they can be taught the convention. A README and a
marketing page cannot: they are edited freely and nobody reads a comment
convention there. So the claims are instead pinned as forbidden phrases. Each
entry is a sentence that was once true, is now false, and is specific enough
that a future reader would not independently write it.

The limitation is worth stating plainly. This catches a fixed set of known
rotten claims. It cannot catch a new false claim, because there is no general
test for whether prose about software is true. What it can do is make sure the
sentences we have already been burned by do not come back, which is the half
that has actually been happening.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: User-facing files, and why each is here.
USER_FACING = {
    "README.md": "the first thing anyone reads",
    "examples/README.md": "the first thing anyone copies from",
    "site/index.html": "the public landing page, and the most optimistic prose in the repo",
}

#: (file glob or literal name, forbidden phrase, why it is forbidden).
#:
#: Phrases are matched case-insensitively and with runs of whitespace collapsed,
#: so HTML line wrapping and Markdown reflowing do not hide a match.
FORBIDDEN_CLAIMS: list[tuple[str, str, str]] = [
    (
        "site/index.html",
        "closed-loop holds concurrency fixed",
        "closed mode is serial today. --concurrency is inert in BOTH modes "
        "because testinghq has no executor. Issue #38.",
    ),
    (
        "site/index.html",
        "open-loop holds a fixed arrival rate and finds the breaking point",
        "open mode cannot hold its arrival schedule against a target slower "
        "than the interval, so it cannot find a breaking point. Issue #38.",
    ),
    (
        "site/index.html",
        "not yet",
        "this page told the world the CLI printed 'not implemented yet' "
        "placeholders and that Barrage was design-stage only, both long after "
        "they stopped being true.",
    ),
    (
        "README.md",
        "closed-loop holds concurrency fixed",
        "same rot as the landing page. Closed mode is serial.",
    ),
    (
        "README.md",
        "not yet",
        "the README claimed the generator, transport, reporting and web UI "
        "'land across the milestones below' four lines above a working "
        "--config example in the same file.",
    ),
    (
        "examples/README.md",
        "not yet",
        "the examples page claimed the TOML loader and demo.py were unwired; "
        "both work.",
    ),
    (
        "examples/README.md",
        "does not exist on this branch",
        "a stand-in described itself as waiting for a module that had already "
        "landed.",
    ),
]


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, so wrapping does not hide a match."""
    return re.sub(r"\s+", " ", text).lower()


def _read(name: str) -> str:
    path = REPO_ROOT / name
    return _normalise(path.read_text(encoding="utf-8-sig"))


def test_the_user_facing_files_are_all_present():
    """A guard over files that do not exist passes for the wrong reason."""
    for name in USER_FACING:
        assert (REPO_ROOT / name).is_file(), f"{name} is missing"


def test_no_user_facing_file_claims_something_that_is_now_false():
    offenders = []
    for name, phrase, why in FORBIDDEN_CLAIMS:
        if phrase.lower() in _read(name):
            offenders.append(f"{name}: {phrase!r} ({why})")

    assert not offenders, (
        "user-facing prose is claiming something the code no longer does:\n  "
        + "\n  ".join(offenders)
        + "\n\nThese are the exact sentences that have already been shipped "
        "falsely. Correct the prose; do not delete the entry."
    )


def test_the_forbidden_claim_list_is_not_vacuous():
    """If every entry were misspelled or pointing at a file that no longer
    exists, this would pass while checking nothing. Assert the list is the
    size we think it is and that every entry names a real file."""
    assert len(FORBIDDEN_CLAIMS) >= 7, (
        f"only {len(FORBIDDEN_CLAIMS)} forbidden claims are registered; "
        "entries have probably been dropped by accident"
    )
    for name, phrase, why in FORBIDDEN_CLAIMS:
        assert name in USER_FACING, f"{name!r} is not a guarded file"
        assert phrase and why, "an entry with no phrase or no reason is useless"
        assert phrase.lower() not in _read(name), (
            f"{name} still contains {phrase!r}, so the entry is not doing "
            "anything and the real fix is elsewhere"
        )


def test_the_guard_covers_every_user_facing_file_we_named():
    """A file added to the claim list but not scanned would be silently
    unguarded."""
    scanned = {name for name, _p, _w in FORBIDDEN_CLAIMS}
    assert scanned == set(USER_FACING), (
        f"guarded claims cover {sorted(scanned)} but USER_FACING names "
        f"{sorted(USER_FACING)}"
    )
