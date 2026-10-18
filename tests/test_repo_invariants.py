"""An invariant of the test suite itself."""
from __future__ import annotations

import ast
import collections
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_duplicate_test_function_names_within_a_file():
    """A second `def test_x` in one module shadows the first, so the first is
    dead code that reads exactly like a passing test."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        names = collections.Counter(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        offenders.extend(
            f"{path.relative_to(REPO_ROOT)}: {name} x{count}"
            for name, count in names.items()
            if count > 1
        )
    assert not offenders, f"test functions defined more than once: {offenders}"
