"""Every non-Python file the package reads must be declared as package data.

`steady` shipped in 0.1.0 unable to run from a wheel: its intent fixture was not
declared, setuptools left it out, and `steady` loads it by path. The failure is
invisible to an editable install, which is what the suite and every developer
use, so it is checked here against the declaration directly.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from testinghq.core.config import tomllib

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "testinghq"


def _declared():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"].get("package-data", {})


def test_every_data_file_in_the_package_is_declared():
    declared = _declared()
    missing = []
    for path in PACKAGE.rglob("*"):
        if not path.is_file() or path.suffix in (".py", ".pyc") or "__pycache__" in path.parts:
            continue
        covered = False
        for package, patterns in declared.items():
            root = REPO / Path(*package.split("."))
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
                covered = True
        if not covered:
            missing.append(path.relative_to(REPO).as_posix())
    assert not missing, (
        f"not declared in [tool.setuptools.package-data], so a wheel would ship "
        f"without them: {missing}"
    )


def test_the_steady_fixture_is_the_one_that_prompted_this():
    assert (PACKAGE / "pipeline" / "fixtures" / "steady_intents.json").is_file()
    assert "testinghq.pipeline" in _declared()
