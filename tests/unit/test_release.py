"""Release checks: the version agrees, the changelog describes it, and a wheel
built from the tree carries the package data the tools read at run time."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
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


def _source_tree(tmp_path) -> Path:
    """A copy of what the build reads, plus what a working checkout of the
    sweep holds but must never ship: a setup script's credentials, a run's
    artifacts, the download cache and a sidecar's state."""
    source = tmp_path / "src"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in",
                 "CHANGELOG.md", "CONTRIBUTING.md"):
        shutil.copy2(REPO_ROOT / name, source / name)
    for tree in ("testinghq", "sweep", "docs", "examples"):
        shutil.copytree(
            REPO_ROOT / tree, source / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "runs", ".cache", ".state",
                                          ".env.*"),
        )
    # examples/runs is published; sweep/runs is not. The ignore above skipped
    # both, so put the published one back.
    if (REPO_ROOT / "examples" / "runs").is_dir():
        shutil.copytree(REPO_ROOT / "examples" / "runs", source / "examples" / "runs")
    (source / "sweep" / ".env.chatwoot").write_text("CHATWOOT_TOKEN=secret\n", encoding="utf-8")
    run = source / "sweep" / "runs" / "chatwoot" / "20261019T000000Z"
    run.mkdir(parents=True)
    (run / "summary.json").write_text("{}", encoding="utf-8")
    cache = source / "sweep" / ".cache" / "zammad" / "test" / "data" / "mail"
    cache.mkdir(parents=True)
    (cache / "mail001.box").write_text("From: a@example.com\n\n", encoding="utf-8")
    state = source / "sweep" / ".state" / "freescout"
    state.mkdir(parents=True)
    (state / "outbound.jsonl").write_text("{}\n", encoding="utf-8")
    return source


def test_the_wheel_builds_and_contains_the_package_data(tmp_path):
    """Built from a copy of the tree, so the build leaves nothing in the repo.
    The sdist is built from the same copy: it carries the sweep harness and
    none of a checkout's credentials, runs or cache; the wheel carries none of
    the sweep at all."""
    pytest.importorskip("setuptools", minversion="77")
    source = _source_tree(tmp_path)
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
    assert not any(n.startswith(("tests/", "web/", "sweep/")) for n in names)

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, setuptools.build_meta as b; print(b.build_sdist(sys.argv[1]))",
         str(out)],
        cwd=source, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (sdist,) = out.glob("testinghq-*.tar.gz")
    with tarfile.open(sdist) as bundle:
        members = {name.split("/", 1)[1] for name in bundle.getnames() if "/" in name}
    for wanted in ("sweep/run.sh", "sweep/harness.py", "sweep/README.md",
                   "sweep/mailpit_pipeline.py", "sweep/profiles/mailpit.env",
                   "examples/pipeline_demo.py", "examples/readback/mailpit.toml"):
        assert wanted in members, f"{wanted} is missing from the sdist"
    assert not any(n.startswith(("sweep/runs", "sweep/.cache", "sweep/.state", "sweep/.env.",
                                 "tests/"))
                   for n in members), sorted(members)
    assert not any("__pycache__" in n for n in members)
