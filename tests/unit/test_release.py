"""Release checks: the version agrees, the changelog describes it, and a wheel
built from the tree carries the package data the tools read at run time."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from testinghq.core.config import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_the_package_and_the_cli_report_the_packaged_version():
    import testinghq

    version = _pyproject_version()
    assert testinghq.__version__ == version
    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", "--version"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert version in result.stdout


def test_the_changelog_has_a_section_for_the_version():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(_pyproject_version())}\]", text, re.MULTILINE)
    assert re.search(r"^## \[Unreleased\]", text, re.MULTILINE)


def test_the_wheel_builds_and_contains_the_package_data(tmp_path):
    """Built from a copy of the tree, so the build leaves nothing in the repo."""
    pytest.importorskip("setuptools", minversion="77")
    source = tmp_path / "src"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(REPO_ROOT / name, source / name)
    shutil.copytree(
        REPO_ROOT / "testinghq", source / "testinghq",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    out = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--no-index", "-q", "-w", str(out), str(source)],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(out.glob("testinghq-*.whl"))
    assert len(wheels) == 1, wheels
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for wanted in (
        "testinghq/pipeline/fixtures/steady_intents.json",
        "testinghq/report_html/template.html",
        "testinghq/report_html/render.js",
    ):
        assert wanted in names, f"{wanted} is missing from the wheel"
    assert not any(n.startswith(("tests/", "web/")) for n in names)
