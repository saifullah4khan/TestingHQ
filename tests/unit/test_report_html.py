"""`testinghq report --html`: every tool's artifact renders to one
self-contained page carrying the verdict, the counts and the exit code."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from test_report_command import ALL_FIXTURES
from testinghq.cli import main
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.report_html import render_html, script_path
from testinghq.reporting import summarize

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TAG_WITH_RESOURCE = re.compile(r"<[a-zA-Z][^>]*\s(src|href|action|srcset)=")

#: Summaries in the frozen `report --json` shape for the two tools whose
#: readers land in other workstreams, so the renderer is proven on them now.
STEADY_SUMMARY = {
    "tool": "steady", "verdict": "UNSTABLE", "verdict_source": "derived",
    "exit_code": EXIT_FINDING, "exit_code_source": "derived",
    "counts": {"sent": 36, "found": 36, "passed": 30, "failed": 6, "missing": 0,
               "duplicated": 0, "extra": 0, "produced": 0, "records": 36,
               "scenarios": 0, "buckets": 0},
    "findings": [{"kind": "dissent:wrap-forward",
                  "detail": "billing family: 2 of 6 variants read a different label"}],
    "seed": 21, "target": "local", "dry_run": False, "schema_version": 1,
    "aborted": False,
}
SIGCHECK_SUMMARY = {
    "tool": "sigcheck", "verdict": "SIGNATURES-ENFORCED", "verdict_source": "recorded",
    "exit_code": EXIT_OK, "exit_code_source": "derived",
    "counts": {"sent": 6, "found": 0, "passed": 6, "failed": 0, "missing": 0,
               "duplicated": 0, "extra": 0, "produced": 0, "records": 6,
               "scenarios": 0, "buckets": 0},
    "findings": [], "seed": 4, "target": "local", "dry_run": False,
    "schema_version": 1, "aborted": False,
}


def _cases():
    for tool, artifact in sorted(ALL_FIXTURES.items()):
        yield pytest.param(summarize(artifact).to_json(), artifact, id=tool)
    for name in ("sample_run_clean.json", "sample_run_with_failures.json"):
        artifact = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        yield pytest.param(summarize(artifact).to_json(), artifact, id=name)
    yield pytest.param(STEADY_SUMMARY, None, id="steady")
    yield pytest.param(SIGCHECK_SUMMARY, None, id="sigcheck")


def _assert_shows(page: str, summary: dict) -> None:
    verdict = html.escape(summary["verdict"])
    assert f"data-verdict>{verdict}</span>" in page
    assert f'data-exit-code="{summary["exit_code"]}"' in page
    for name, value in summary["counts"].items():
        if value:
            assert (
                f'data-count="{name}"><dt>{name}</dt><dd>{value}</dd>' in page
            ), f"count {name}={value} is not shown"
    for finding in summary["findings"]:
        assert html.escape(finding["detail"]) in page


def _assert_self_contained(page: str) -> None:
    assert not re.search(r"https?://", page), "the page names a URL"
    assert not TAG_WITH_RESOURCE.search(page), "the page loads a resource"
    assert "default-src 'none'" in html.unescape(page)


@pytest.mark.parametrize("summary, artifact", list(_cases()))
def test_every_tool_renders_its_verdict_counts_and_exit_code(summary, artifact):
    page = render_html(summary, artifact)
    _assert_shows(page, summary)
    _assert_self_contained(page)


@pytest.mark.parametrize("tool", sorted(ALL_FIXTURES))
def test_the_command_writes_the_page(tool, tmp_path, capsys):
    path = tmp_path / f"{tool}.json"
    path.write_text(json.dumps(ALL_FIXTURES[tool]), encoding="utf-8")
    out = tmp_path / f"{tool}.html"

    assert main(["report", str(path), "--html", str(out), "--json"]) == EXIT_OK
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert f"wrote {out}" in captured.err
    page = out.read_text(encoding="utf-8")
    _assert_shows(page, summary)
    _assert_self_contained(page)


def test_a_summary_without_the_1_0_keys_still_renders():
    summary = summarize(ALL_FIXTURES["ledger"]).to_json()
    summary.pop("schema_version", None)
    summary.pop("aborted", None)
    page = render_html(summary)
    assert "schema 0 (not recorded)" in page
    assert "Aborted" not in page
    _assert_shows(page, summary)


def test_dry_run_and_aborted_are_said_plainly():
    page = render_html(dict(STEADY_SUMMARY, dry_run=True, aborted=True))
    assert "Dry run: nothing was sent." in page
    assert "Aborted: the run was interrupted" in page


def test_barrage_buckets_are_an_inline_chart_with_the_knee_marked():
    artifact = ALL_FIXTURES["barrage"]
    artifact = dict(artifact, summary=dict(artifact["summary"], knee={
        "at_seconds": 1.0, "reason": "shedding", "detail": "p50 doubled"}))
    page = render_html(summarize(artifact).to_json(), artifact)
    assert page.count("<svg") == 2
    assert "<polyline" in page and 'class="knee"' in page
    for key in ("targeted_rps", "achieved_rps", "p50_latency_ms"):
        assert key in page


def test_records_and_config_come_from_the_artifact():
    artifact = json.loads(
        (FIXTURES / "sample_run_with_failures.json").read_text(encoding="utf-8")
    )
    page = render_html(summarize(artifact).to_json(), artifact)
    assert "Records (6)" in page
    assert page.count('<tr class="row-bad">') == 3, "the three failed records"
    assert "staging-example" in page, "the config block"


def test_artifact_text_is_escaped_never_markup():
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    summary = dict(SIGCHECK_SUMMARY, verdict=hostile,
                   findings=[{"kind": hostile, "detail": hostile}])
    artifact = {"config": {"note": hostile, "url": "http://192.0.2.1/intake"},
                "records": [{"id": hostile}]}
    page = render_html(summary, artifact)
    assert hostile not in page
    assert page.count("<script>") == 1, "only the page's own script"
    assert "<img" not in page
    # A URL in the artifact is shown as text, never as a link or a resource.
    assert "http://192.0.2.1/intake" in page
    assert not TAG_WITH_RESOURCE.search(page)


def test_the_policy_allows_exactly_the_inline_script_and_style():
    page = render_html(SIGCHECK_SUMMARY)
    policy = html.unescape(re.search(r'Content-Security-Policy" content="([^"]+)"', page)[1])
    for tag in ("script", "style"):
        body = re.search(rf"<{tag}>(.*?)</{tag}>", page, re.DOTALL)[1]
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"{tag}-src 'sha256-{digest}'" in policy, tag


def test_the_inlined_script_is_the_packaged_render_js():
    page = render_html(SIGCHECK_SUMMARY)
    assert script_path().read_text(encoding="utf-8") in page


def test_a_long_record_list_is_capped_and_says_so():
    artifact = {"records": [{"id": f"r-{i}"} for i in range(600)]}
    page = render_html(SIGCHECK_SUMMARY, artifact)
    assert "First 500 of 600 shown." in page
    assert page.count("<td>r-") == 500


def test_an_unwritable_output_is_a_refusal(tmp_path, capsys):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(ALL_FIXTURES["blast"]), encoding="utf-8")
    code = main(["report", str(path), "--html", str(tmp_path / "missing" / "out.html")])
    assert code == EXIT_REFUSED
    assert "could not write" in capsys.readouterr().err


def test_a_bad_artifact_writes_no_page(tmp_path):
    path = tmp_path / "run.json"
    path.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.html"
    assert main(["report", str(path), "--html", str(out)]) == EXIT_REFUSED
    assert not out.exists()


def test_the_command_runs_as_a_subprocess(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(ALL_FIXTURES["compare"]), encoding="utf-8")
    out = tmp_path / "out.html"
    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", "report", str(path), "--html", str(out)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "REGRESSED" in out.read_text(encoding="utf-8")
