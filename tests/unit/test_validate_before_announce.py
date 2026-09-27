"""Nothing is announced until the command is known to be legal.

The complaint: `testinghq barrage fire --target local --rate 500` printed

    barrage fire: explicit --send flag set
    refused: refusing to run: rate 500.0 req/s exceeds the safety ceiling...

The first line is a decision about what the command is *about to do*, printed
before the command knew it was allowed to do it. It reads as though the run had
reached a decision about sending and then changed its mind. It had not. It never
got that far.

The rule these tests pin: a plan that will be refused produces no announcement at
all, and the announcement is the last thing printed before real work starts.

The `blast` half of this was subtler than it looks. `blast fire` announced, then
built the corpus, then validated the target, so a refusal was preceded by both
the announcement and possibly a stack of work. The corpus is now built first.
"""
from pathlib import Path

import pytest

from testinghq import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(args, capsys):
    """Invoke the CLI in process, not as a subprocess.

    In process on purpose. A subprocess is a separate interpreter, so the
    suite-wide network block in tests/conftest.py does not apply to it, and a
    test that passes --send would then be one refactor away from actually
    transmitting. Calling `cli.main` directly means the block applies and a
    mistake here fails loudly instead of quietly.
    """
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


#: Substrings that announce what a command is about to do. A refused command must
#: print none of them.
ANNOUNCEMENTS = ("dry-run default", "explicit --send flag set", "fire:")

REFUSALS = [
    ("blast", ["blast", "fire", "--target", "no-such-target", "--send"]),
    ("barrage rate ceiling",
     ["barrage", "fire", "--target", "local", "--rate", "500"]),
    ("barrage duration ceiling",
     ["barrage", "fire", "--target", "local", "--rate", "10", "--duration", "5000"]),
    ("barrage plan invalid",
     ["barrage", "fire", "--target", "local", "--rate", "10", "--duration", "5",
      "--warmup", "5"]),
    ("barrage inert concurrency",
     ["barrage", "fire", "--target", "local", "--mode", "open",
      "--concurrency", "8"]),
]


@pytest.mark.parametrize(
    "label,args", REFUSALS, ids=[label for label, _a in REFUSALS]
)
def test_a_refused_command_announces_nothing(label, args, capsys):
    """The load-bearing assertion.

    Both streams are checked, because the announcement could be on either and a
    check of stdout alone would miss a move to stderr.
    """
    code, out, err = _run(args, capsys)

    assert code != 0, f"{label} was not refused at all"
    assert "refused" in err.lower(), f"{label} exited {code} without saying why: {err!r}"

    combined = out + err
    for announcement in ANNOUNCEMENTS:
        assert announcement not in combined, (
            f"{label} printed {announcement!r} before refusing. An announcement "
            f"describes what the command is about to do, and this command is "
            f"not going to do anything. Full output:\n{combined}"
        )


def test_the_rate_ceiling_message_is_not_doubled(capsys):
    """`refused: refusing to run: rate 500.0 req/s ...`

    The handler prefixes every refusal with `refused:` and the ceiling error
    already began with `refusing to run:`, so the user got both. The message is
    now written to be read after the handler's prefix rather than around it.
    """
    _code, _out, err = _run(
        ["barrage", "fire", "--target", "local", "--rate", "500"], capsys
    )
    err = err.strip()

    assert err.startswith("refused: ")
    assert not err.startswith("refused: refusing"), (
        f"the prefix is doubled: {err!r}"
    )
    assert "safety ceiling" in err
    # And the useful part survived: how to actually do what you were trying to.
    assert "--allow-high-rate" in err


def test_a_legal_dry_run_still_announces(capsys):
    """The other direction. Reordering must not silence the announcement for a
    command that is going to run, or the dry-run preview would appear with no
    explanation of why nothing was sent."""
    code, out, err = _run(
        ["barrage", "fire", "--target", "local", "--rate", "10"], capsys
    )

    assert code == 2, err
    assert "dry-run default" in out
    assert "dry-run preview" in out


def test_the_announcement_still_appears_before_the_preview(capsys):
    """Ordering within a successful run: what the command is doing, then the
    details. A preview appearing first would leave the reader guessing whether
    anything was sent."""
    _code, out, _err = _run(
        ["barrage", "fire", "--target", "local", "--rate", "10"], capsys
    )
    lines = [l for l in out.splitlines() if l.strip()]

    announcement = next(i for i, l in enumerate(lines) if "dry-run default" in l)
    preview = next(i for i, l in enumerate(lines) if "dry-run preview" in l)
    assert announcement < preview
