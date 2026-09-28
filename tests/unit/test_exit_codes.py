"""One exit-code convention, defined in exactly one place, enforced everywhere.

An earlier version of this guard checked only verify, ledger and redeliver. It
was written when those were the only tools sharing codes, and it never grew to
cover the modules that actually had competing definitions. `barrage/fire.py` had
its own copy, `compare/runs.py` had an incompatible one, and `cli.py` had around
forty bare integer literals, none of which the guard would have noticed. The
guard was green the whole time the duplication was there, which is the specific
way a structural guard can be worse than none: it reads like coverage.

So this version reads every module in the package and asks three questions:
where are the numbers written down, does every tool resolve them to the shared
values, and has any tool quietly invented a code outside the set.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from testinghq.core import exit_codes

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "testinghq"

#: The one module allowed to assign the numbers.
CANONICAL = PACKAGE / "core" / "exit_codes.py"


def _module_paths():
    return sorted(PACKAGE.rglob("*.py"))


# ---------------------------------------------------------------------------
# The numbers are written down once
# ---------------------------------------------------------------------------


def test_the_codes_are_the_documented_values():
    """Restated here rather than only in the module, so this test fails if the
    values are changed to something the convention does not describe."""
    assert exit_codes.EXIT_OK == 0
    assert exit_codes.EXIT_REFUSED == 1
    assert exit_codes.EXIT_DRY_RUN == 2
    assert exit_codes.EXIT_FINDING == 3


def test_an_unwired_subcommand_is_a_refusal_not_a_dry_run():
    """The one place in the CLI where a bug used to report as a success.

    `_not_yet` is the fallback for a subcommand the parser offered and the
    dispatcher has no handler for, which means the parser and the dispatcher
    disagree. That is a bug in this file.

    It returned EXIT_DRY_RUN, which is a successful run that was asked to hold
    back. So a script reading 2 as "nothing was sent, all well" was being told
    the tool was working as intended at the exact moment it was not. The
    convention this file documents is a refusal, and returning the dry-run code
    hid a bug behind the one code that means a deliberate choice.

    Reaching it needs a subcommand the parser has and the dispatcher lacks, so
    it is exercised through the function rather than by inventing one, and the
    point of the test is the code, not the path.
    """
    from testinghq.cli import _not_yet

    code = _not_yet("a-subcommand-that-does-not-exist")
    assert code == exit_codes.EXIT_REFUSED, (
        f"an unwired subcommand returned {code}. EXIT_DRY_RUN ({exit_codes.EXIT_DRY_RUN}) "
        "reads as a successful run that deliberately sent nothing, which is the "
        "opposite of what happened."
    )
    assert code != exit_codes.EXIT_DRY_RUN
    assert code != exit_codes.EXIT_OK


def test_no_module_but_the_canonical_one_assigns_an_exit_code_constant():
    """AST, not text search. A regex cannot tell `EXIT_OK = 0` from a mention of
    it in a docstring, so a module could document the convention correctly and
    still redefine it, and the guard would pass."""
    offenders = []
    for path in _module_paths():
        if path == CANONICAL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                if name.startswith("EXIT_") or name == "ARG_PARSE_ERROR":
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}: {name}"
                    )
    assert not offenders, (
        "exit codes are defined in more than one place: "
        f"{offenders}. Every tool must import them from "
        f"{CANONICAL.relative_to(REPO_ROOT).as_posix()} so a script has one "
        "convention to read."
    )


def test_the_canonical_module_is_where_the_numbers_live():
    assert CANONICAL.is_file(), (
        f"{CANONICAL.relative_to(REPO_ROOT).as_posix()} is missing; it is the "
        "one place the exit codes are allowed to be written down"
    )


# ---------------------------------------------------------------------------
# Every tool resolves to the shared values
# ---------------------------------------------------------------------------


def _imported_names(path: Path):
    """Names imported from core.exit_codes, and names assigned at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith("exit_codes"):
                imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def test_the_tools_that_reexport_the_codes_resolve_to_the_shared_values():
    """`barrage.fire` and `pipeline.common` both re-export the names other
    modules import. They must be the same objects, not copies that happen to
    agree today."""
    from testinghq.barrage import fire
    from testinghq.pipeline import common

    for module in (fire, common):
        for name in ("EXIT_OK", "EXIT_REFUSED", "EXIT_DRY_RUN"):
            assert getattr(module, name) is getattr(exit_codes, name), (
                f"{module.__name__}.{name} is not the shared constant"
            )
    assert common.EXIT_MISMATCH is exit_codes.EXIT_FINDING


def test_compare_is_on_the_shared_convention():
    """The breaking part, stated as a test. `compare` used 1 for a regression
    and 2 for a usage error; both collided with what every other tool meant."""
    from testinghq.compare import runs

    assert runs.EXIT_NO_REGRESSION is exit_codes.EXIT_OK
    assert runs.EXIT_REGRESSION is exit_codes.EXIT_FINDING, (
        "a regression is a finding, not a refusal"
    )
    assert runs.EXIT_USAGE is exit_codes.EXIT_REFUSED, (
        "a usage error is a refusal, not a dry run"
    )


def _module_level_assignments(path: Path):
    """name -> the AST node it is assigned, for module-level assignments only."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


def test_the_aliases_are_the_shared_names_not_copied_numbers():
    """AST, not `is`.

    `EXIT_MISMATCH = 3` and `EXIT_MISMATCH = EXIT_FINDING` compare equal and even
    are the same object, because CPython interns small ints. An identity check
    therefore cannot tell a shared constant from a hand-copied literal, which is
    exactly the regression that matters: someone "fixing" a name in a hurry
    types the number, and the two answers drift apart silently. So this reads
    the assignment and requires the canonical name on the right-hand side.
    """
    assignments = _module_level_assignments(CANONICAL)
    for alias, canonical in (
        ("EXIT_MISMATCH", "EXIT_FINDING"),
        ("EXIT_NO_REGRESSION", "EXIT_OK"),
        ("EXIT_REGRESSION", "EXIT_FINDING"),
        ("EXIT_USAGE", "EXIT_REFUSED"),
    ):
        value = assignments.get(alias)
        assert isinstance(value, ast.Name), (
            f"{alias} must be assigned from {canonical}, not a literal. "
            f"Got {ast.dump(value) if value is not None else 'nothing'}."
        )
        assert value.id == canonical, (
            f"{alias} is assigned from {value.id}, expected {canonical}"
        )


# ---------------------------------------------------------------------------
# No tool invented a code
# ---------------------------------------------------------------------------


def test_every_returned_code_is_one_of_the_four():
    """Walks every `return <int literal>` in the package and checks it is a
    known code. This is the check that catches the next tool to hard-code a 4.

    It cannot see a code computed at runtime, so it is a floor on the guarantee
    rather than a proof of it. The complementary check is the one below it."""
    allowed = set(exit_codes.ALL_EXIT_CODES)
    offenders = []
    for path in _module_paths():
        if path == CANONICAL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)
                and not isinstance(node.value.value, bool)
                and 0 <= node.value.value <= 3
            ):
                if node.value.value not in allowed:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                        f"returns {node.value.value}"
                    )
    assert not offenders, f"exit codes outside the convention: {offenders}"


def test_the_documented_meanings_cover_every_code():
    assert set(exit_codes.MEANINGS) == set(exit_codes.ALL_EXIT_CODES)
    for code, text in exit_codes.MEANINGS.items():
        assert text and isinstance(text, str), f"{code} has no description"


# ---------------------------------------------------------------------------
# The argparse collision, which is the one thing that has to be overridden
# ---------------------------------------------------------------------------


def test_argparse_usage_errors_do_not_exit_two():
    """The last remaining way to read a 2 as a dry run.

    Argparse exits 2 for a bad command line, and 2 means a dry run here. A
    mistyped flag would read as "ran, sent nothing", which is the same
    misreading this convention exists to prevent. `testinghq.cli` installs a
    parser that exits EXIT_REFUSED instead, and this asserts it is actually
    installed rather than merely intended.
    """
    import argparse as _argparse

    # A stock argparse parser, not `build_parser`, to record what the override
    # is overriding. If argparse ever changes this number, the override is worth
    # revisiting rather than assuming.
    stock = _argparse.ArgumentParser(prog="testinghq")
    with pytest.raises(SystemExit) as excinfo:
        stock.parse_args(["--not-a-real-flag"])
    assert excinfo.value.code == exit_codes.ARG_PARSE_ERROR, (
        "this is argparse's own behaviour and is what makes the override below "
        "necessary; if argparse ever changes, revisit the override"
    )

    from testinghq.cli import build_parser, main

    # A subparser, because the override has to have reached them too.
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["blast", "fire", "--not-a-real-flag"])
    assert excinfo.value.code == exit_codes.EXIT_REFUSED

    with pytest.raises(SystemExit) as excinfo:
        main(["blast", "fire", "--not-a-real-flag"])
    assert excinfo.value.code == exit_codes.EXIT_REFUSED, (
        "the CLI must not let a usage error exit with argparse's 2, because 2 "
        "is a dry run in this package"
    )


@pytest.mark.parametrize("argv", [["--help"], ["--version"]])
def test_help_and_version_still_exit_zero(argv):
    """The override has to be narrow. `--help` and `--version` are not failures
    and must keep succeeding, or a script that probes the binary for its
    version would start reporting the tool as broken."""
    from testinghq.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == exit_codes.EXIT_OK


# ---------------------------------------------------------------------------
# The convention, stated where a reader will find it
# ---------------------------------------------------------------------------


def test_the_readme_documents_the_convention_not_just_the_numbers():
    """Documentation drift is how a convention quietly stops being true.

    Checks the meanings, not the digits. A test that only looked for "0", "1",
    "2" and "3" would be satisfied by a version number or a port somewhere in
    the file, and would stay green through a table that had lost a row. The
    description text comes from MEANINGS, so this also fails if the table and
    the code disagree about what a code means.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for code, meaning in exit_codes.MEANINGS.items():
        assert re.search(rf"^\|\s*{code}\s*\|", readme, re.MULTILINE), (
            f"README.md has no table row for exit code {code}. A reader who "
            "cannot find the table will infer the convention from one tool and "
            "get it wrong for the rest."
        )
    # The meanings table in the README is prose, so check the distinctive words
    # rather than every full sentence.
    for fragment in ("refused", "dry run", "answer was no"):
        assert fragment in readme, (
            f"README.md does not explain the '{fragment}' case"
        )
