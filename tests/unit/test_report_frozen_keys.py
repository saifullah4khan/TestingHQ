"""`report --json` emits the frozen 1.0 key set for every tool, and one
schema_version check guards every reader.

One artifact per tool lives in tests/fixtures (blast uses the shipped
sample run). Each goes through the real command, JSON and HTML together.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from testinghq.cli import main
from testinghq.core.exit_codes import EXIT_OK, EXIT_REFUSED
from testinghq.report_html import render_html
from testinghq.reporting import KNOWN_TOOLS, summarize

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

FROZEN_KEYS = {
    "tool", "verdict", "verdict_source", "exit_code", "exit_code_source",
    "counts", "findings", "seed", "target", "dry_run", "schema_version",
    "aborted",
}

ARTIFACTS = {
    tool: FIXTURES / "artifacts" / f"{tool}.json"
    for tool in KNOWN_TOOLS if tool != "blast"
}
ARTIFACTS["blast"] = FIXTURES / "sample_run_with_failures.json"

#: Tools whose artifact can record an interrupted run.
ABORTABLE = sorted(set(ARTIFACTS) - {"compare"})


def _load(tool):
    return json.loads(ARTIFACTS[tool].read_text(encoding="utf-8"))


def _report_json(path, capsys, *extra):
    code = main(["report", str(path), "--json", *extra])
    captured = capsys.readouterr()
    return code, captured


def test_there_is_one_fixture_per_tool():
    assert set(ARTIFACTS) == set(KNOWN_TOOLS)
    for path in ARTIFACTS.values():
        assert path.is_file(), path


@pytest.mark.parametrize("tool", sorted(ARTIFACTS))
def test_report_json_keys_are_exactly_the_frozen_set(tool, tmp_path, capsys):
    page_path = tmp_path / f"{tool}.html"
    code, captured = _report_json(ARTIFACTS[tool], capsys, "--html", str(page_path))
    assert code == EXIT_OK, captured.err
    payload = json.loads(captured.out)
    assert set(payload) == FROZEN_KEYS
    assert payload["tool"].split()[0] == tool
    assert payload["schema_version"] == 1
    assert payload["aborted"] is False

    page = page_path.read_text(encoding="utf-8")
    assert "<span>schema 1</span>" in page
    assert "Aborted:" not in page
    assert f'data-exit-code="{payload["exit_code"]}"' in page


@pytest.mark.parametrize("tool", sorted(ARTIFACTS))
def test_a_legacy_artifact_reads_as_schema_0_not_aborted(tool, tmp_path, capsys):
    artifact = _load(tool)
    artifact.pop("schema_version", None)
    artifact.pop("aborted", None)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    code, captured = _report_json(path, capsys)
    assert code == EXIT_OK, captured.err
    payload = json.loads(captured.out)
    assert set(payload) == FROZEN_KEYS
    assert payload["schema_version"] == 0
    assert payload["aborted"] is False
    assert "schema 0 (not recorded)" in render_html(payload, artifact)


@pytest.mark.parametrize("tool", ABORTABLE)
def test_an_aborted_artifact_says_so_in_the_summary_and_the_page(tool):
    artifact = dict(_load(tool), aborted=True)
    payload = summarize(artifact).to_json()
    assert payload["aborted"] is True
    assert "Aborted: the run was interrupted" in render_html(payload, artifact)


def test_loop_aborted_in_its_summary_is_aborted_too():
    artifact = _load("loop")
    artifact["summary"] = dict(artifact["summary"], aborted=True)
    assert summarize(artifact).to_json()["aborted"] is True


@pytest.mark.parametrize("tool", sorted(ARTIFACTS))
@pytest.mark.parametrize("version", [2, -1, "1", True, 1.0])
def test_every_reader_refuses_a_schema_version_it_does_not_know(
    tool, version, tmp_path, capsys
):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(dict(_load(tool), schema_version=version)), encoding="utf-8")
    code, captured = _report_json(path, capsys)
    assert code == EXIT_REFUSED
    assert "schema_version" in captured.err
    assert captured.out == ""
