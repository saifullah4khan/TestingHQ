"""The CLI surface is a contract, and this pins it.

`testinghq/cli.py` was 1570 lines holding ten commands, their parsers, their
handlers, the shared readback plumbing and the dispatch table. It is now a
package of eight modules, and the bodies were moved by AST line span so that
nothing could change.

That was checked once, by enumerating the old parser and the new one and
comparing: 22 commands and 172 options, identical. The suite proved the same
thing indirectly, and it caught four real problems during the move, but it does
not prove it directly, because a flag nobody happens to use is not exercised by
any test.

So this pins the shape rather than the behaviour. It is a small, deliberate
inconvenience in exchange for being able to move a command's code to the module
that owns it without first reading every caller.

What it does NOT do is pin the flag list itself. A new flag should be added, not
blocked, and a test that failed on an addition would get deleted. So this checks
structure and reachability, and the surface comparison above is a one-time
verification recorded in the PR.
"""
from __future__ import annotations

import argparse
import ast
import pathlib

import pytest

from testinghq import cli

PACKAGE = pathlib.Path(cli.__file__).resolve().parent

#: One module per concern. If a command's code grows a home that is not on this
#: list, the split has stopped meaning what its docstring says.
EXPECTED_MODULES = {
    "__init__.py": "the public surface: build_parser and main",
    "__main__.py": "python -m testinghq.cli",
    "main.py": "the dispatch table; the only module that knows what commands exist",
    "common.py": "what five or more commands share",
    "blast.py": "generate, fire, replay",
    "barrage.py": "load testing, and compare",
    "pipeline.py": "verify, ledger, redeliver, loop, steady",
    "utility.py": "report and config validate",
}


def test_the_package_has_the_modules_its_docstring_promises():
    present = {p.name for p in PACKAGE.glob("*.py")}
    assert present == set(EXPECTED_MODULES), (
        f"testinghq/cli/ holds {sorted(present)}, expected {sorted(EXPECTED_MODULES)}. "
        "The package docstring names these modules, so a new one or a missing "
        "one is a documentation bug as well as a structural one."
    )


def test_no_module_is_large_again():
    """The reason this package exists.

    `cli.py` was 61 KB. If a module here grows back past 25 KB the split has
    simply moved the problem, and the failure is worth catching at the point
    where it starts rather than years later in a review that nobody has time for.
    """
    limit = 25_000
    oversized = {
        p.name: p.stat().st_size
        for p in PACKAGE.glob("*.py")
        if p.stat().st_size > limit
    }
    assert not oversized, (
        f"these modules are over {limit} bytes: {oversized}. The largest was "
        "1560 lines in one file, which is what this package was made to end."
    )


def test_only_main_knows_the_list_of_commands():
    """`build_parser` and `main` are the assembly point. A command's logic
    discovering that another command exists is how this file grew to 1570 lines
    in the first place."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"main.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                src = ast.dump(node)
                # `args.tool == "..."` style dispatch, which belongs in main.py.
                if "tool" in src and "Eq" in src and "attr" in src:
                    pytest.fail(
                        f"{path.name} dispatches on args.tool, which is "
                        "main.py's job. A command module that knows what other "
                        "commands exist is how one file ends up holding all ten."
                    )


def test_every_command_is_reachable_through_main():
    """Each parser's command name has a handler, and each handler is called.

    A parser entry with no handler is a command that parses and then refuses
    with "no handler is wired up", which is what `_not_yet` is for. A handler
    nothing dispatches to is dead code with a name that says otherwise.
    """
    main_src = (PACKAGE / "main.py").read_text(encoding="utf-8")

    parser = cli.build_parser()
    sub = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ][0]

    for tool, subparser in sorted(sub.choices.items()):
        assert f'args.tool == "{tool}"' in main_src, (
            f"{tool} is a registered command but main() never dispatches on it. "
            "It would parse and then refuse."
        )
        inner = [
            a for a in subparser._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        for action in inner:
            # The dest is read rather than assumed. `config` registers its
            # subparser with dest="config_command" and every other tool with
            # dest="command", because `config` and its subcommand would
            # otherwise both be called "command" and collide on one namespace.
            dest = action.dest
            for name in action.choices:
                assert f'args.{dest} == "{name}"' in main_src, (
                    f"{tool} {name} parses into args.{dest} but main() does not "
                    "dispatch on it"
                )


def test_the_entry_points_still_resolve():
    """`pyproject.toml` points at `testinghq.cli:main` and the e2e suite runs
    `python -m testinghq.cli`. Both are part of the interface rather than
    conveniences, and a package conversion is exactly when one of them quietly
    stops working."""
    # Not `import tomllib`: that is stdlib only from 3.11, and this file has to
    # be collectable on the 3.10 leg. `core.config` already resolved the answer
    # and exposes it, which is the project's answer to the question.
    #
    # This is not hypothetical caution. The guard in test_release.py exists
    # because a test file in this repository did exactly this, failed to be
    # collected on 3.10, and took the release PR red. Writing it a second time
    # is the failure mode the guard was added for.
    from testinghq.core import config as config_module

    with open(pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml", "rb") as f:
        scripts = config_module.tomllib.load(f)["project"].get("scripts", {})
    assert scripts.get("testinghq") == "testinghq.cli:main", (
        f"the console script is {scripts.get('testinghq')!r}"
    )
    assert callable(cli.main)
    assert (PACKAGE / "__main__.py").exists(), (
        "`python -m testinghq.cli` needs __main__.py, and tests/e2e/"
        "test_real_sockets.py builds commands with exactly that string"
    )


def test_the_shared_readback_options_live_in_one_place():
    """Five of the ten commands take a readback, and they all take the same
    options. If those were copied per command, adding a readback option would
    be five edits and a four-way disagreement about when it landed."""
    common_src = (PACKAGE / "common.py").read_text(encoding="utf-8")
    assert "def _resolve_readback(" in common_src
    assert "def _add_readback_args(" in common_src
    assert "def _add_readback_poll_args(" in common_src

    others = [p for p in PACKAGE.glob("*.py") if p.name not in {"common.py", "__init__.py"}]
    for path in others:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                "_resolve_readback", "_add_readback_args", "_add_readback_poll_args",
            }:
                pytest.fail(
                    f"{path.name} defines {node.name}, which is common.py's. A "
                    "second copy of shared readback plumbing is the exact "
                    "failure this repository has already paid for twice."
                )
