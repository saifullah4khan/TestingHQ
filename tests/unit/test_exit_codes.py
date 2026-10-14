"""One exit-code convention for every tool, checked through the code that
uses it."""
from __future__ import annotations

import pytest

from testinghq.core import exit_codes


def test_the_codes_are_the_documented_values():
    """Restated here rather than only in the module, so this test fails if the
    values are changed to something the convention does not describe."""
    assert exit_codes.EXIT_OK == 0
    assert exit_codes.EXIT_REFUSED == 1
    assert exit_codes.EXIT_DRY_RUN == 2
    assert exit_codes.EXIT_FINDING == 3


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
