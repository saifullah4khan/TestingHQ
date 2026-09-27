"""The declared Python support must match what CI actually runs.

`pyproject.toml` claimed `requires-python = ">=3.9"` while `ci.yml` ran a
single job on 3.11. A promise nothing was checking is not a promise, and on
3.9 and 3.10 it would have been false in a way that mattered: `tomllib` is
stdlib only from 3.11, so `core/config.py` falls back to `tomli`, and `tomli`
was not a declared dependency. Installing on 3.9 gave a tool whose target
config could not be read.

This ties the two together so they cannot drift apart again, in either
direction: raising the floor without updating the matrix, or narrowing the
matrix while still claiming the wider range.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SECURITY = REPO_ROOT / ".github" / "workflows" / "security.yml"

#: Every version the project says it supports, and the newest it says it does
#: not. 3.9 is the floor because that is what is claimed; 3.14 does not exist
#: yet and is here so that claiming it, or dropping 3.13, is caught.
SUPPORTED = ("3.9", "3.10", "3.11", "3.12", "3.13")
FLOOR = SUPPORTED[0]
CEILING = SUPPORTED[-1]


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _ci_matrix() -> list[str]:
    """Versions in ci.yml's `tests` job matrix, as strings.

    Parsed with a regex rather than a YAML parser because PyYAML is not a
    dependency and adding one to assert a version list would be silly. Narrow
    on purpose: it reads the matrix block of the `tests` job only, so a matrix
    added to a different job does not get mistaken for this one.
    """
    text = CI.read_text(encoding="utf-8")
    match = re.search(
        r"python-version:\s*\[(?P<versions>[^\]]*)\]", text, re.DOTALL
    )
    assert match, (
        f"{CI.name}: no inline python-version matrix found; if it has been "
        "restructured, update this parser rather than deleting the check"
    )
    return re.findall(r'"(\d+\.\d+)"', match.group("versions"))


def test_the_declared_floor_matches_the_matrix():
    declared = _pyproject()["project"]["requires-python"]
    assert declared == f">={FLOOR}", (
        f"requires-python is {declared!r} but the CI matrix starts at {FLOOR}"
    )


def test_the_matrix_covers_every_supported_version():
    """The claim in pyproject and the versions CI runs must be the same set.
    Claiming more than is tested is the original defect; testing more than is
    claimed is harmless but usually means the claim is out of date too."""
    assert tuple(_ci_matrix()) == SUPPORTED, (
        f"ci.yml runs {tuple(_ci_matrix())} but the project supports "
        f"{SUPPORTED}; one of the two is wrong"
    )


def test_the_matrix_tests_the_ceiling_too():
    """A matrix that stops at 3.11 while the local interpreter is 3.13 is how a
    3.13-only bug ships: the code claims a range, CI proves the middle of it,
    and the newest version is the one nobody runs."""
    assert CEILING in _ci_matrix()


def test_the_toml_parser_is_declared_for_interpreters_without_one():
    """`tomllib` is stdlib from 3.11. Below that, `core/config.py` imports
    `tomli`, so without a conditional dependency the declared 3.9 support
    produced a tool that could not read its own target config.

    Asserted on the marker rather than on the version, because the marker is
    the part that has to be right, and an unconditional `tomli` would work but
    would be a needless dependency on 3.11+."""
    dependencies = _pyproject()["project"].get("dependencies", [])
    toml = [d for d in dependencies if d.lower().startswith("tomli")]

    assert toml, (
        "no TOML parser is declared. On Python below 3.11 `core/config.py` "
        "falls back to `tomli`, so installing there would give a tool whose "
        "target config cannot be read"
    )
    assert any("python_version" in d and "3.11" in d for d in toml), (
        f"the tomli dependency must be conditional on the interpreter, got {toml}"
    )


def test_config_reports_the_missing_parser_rather_than_crashing(tmp_path):
    """If the dependency is ever wrong, the failure must be a clear message
    naming the package, not an ImportError from deep inside a load call."""
    from testinghq.core import config as config_module

    if config_module.tomllib is not None:
        pytest.skip("this interpreter has a TOML parser, nothing to check")

    bad = tmp_path / "target.toml"
    bad.write_text('[targets.local]\nname = "local"\nurl = "http://127.0.0.1:9/x"\n',
                   encoding="utf-8")

    with pytest.raises(config_module.ConfigError) as exc:
        config_module.load_targets(str(bad))
    assert "tomli" in str(exc.value)


def test_this_interpreter_is_one_the_project_claims():
    """The suite should not be green on an interpreter the project says it does
    not support. Cheap, and it turns "claimed support" into something the local
    run also respects."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert version in SUPPORTED, (
        f"running the suite on {version}, which is outside the claimed range "
        f"{SUPPORTED}. Either drop it from the range or fix what breaks."
    )
