"""sweep/harness.py: how a sweep run's step exit codes become its verdict, and
the portable helpers sweep/run.sh leans on instead of shell tools."""
from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_harness():
    spec = importlib.util.spec_from_file_location("sweep_harness", REPO_ROOT / "sweep" / "harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


def _line(phase, step, tool, code="", artifact="", html="", log="", skipped="", command=""):
    return "\t".join([phase, step, tool, str(code), artifact, html, log, skipped, command])


def _write_steps(path, *lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _step(tool, code, skipped=None, report=None):
    return {"step": tool, "tool": tool, "exit_code": code, "skipped": skipped,
            "report_exit_code": report}


def test_every_tool_exiting_0_is_a_passed_run():
    assert harness.classify([_step("verify", 0), _step("ledger", 0)]) == "passed"


def test_a_finding_is_a_completed_run_with_findings_not_an_error():
    assert harness.classify([_step("verify", 0), _step("ledger", 3)]) == "findings"


@pytest.mark.parametrize("code", [1, 2, 127, 139])
def test_a_tool_exit_outside_0_and_3_is_an_error(code):
    """1 is a refusal; 2 means a step that was given --send sent nothing; any
    other code is outside the convention, a crash or a missing command."""
    assert harness.classify([_step("ledger", 3), _step("verify", code)]) == "error"


def test_a_harness_step_completes_only_on_0():
    """Exit 3 from docker or a setup script is not a TestingHQ finding."""
    assert harness.classify([{"step": "setup", "tool": "bash", "exit_code": 3}]) == "error"


def test_a_profile_skip_is_not_an_error_but_a_step_that_could_not_run_is():
    switched_off = _step("blast", None, skipped="SWEEP_CORPUS=0")
    assert harness.classify([_step("verify", 0), switched_off]) == "passed"
    blocked = _step("verify", None, skipped="blocked: setup failed")
    assert harness.classify([blocked]) == "error"


def test_a_report_that_could_not_render_is_an_error():
    assert harness.classify([_step("verify", 0, report=1)]) == "error"


def test_the_step_log_is_read_in_order_with_reports_folded_in(tmp_path):
    steps = _write_steps(
        tmp_path / "steps.tsv",
        _line("step", "compose-up", "docker", 0, log="compose-up.log",
              command="docker compose -f sweep/compose/zammad.yml up -d"),
        _line("step", "verify", "verify", 3, artifact="verify.json", log="verify.log",
              command="testinghq verify fire --send --note 'a\tb'"),
        _line("step", "barrage", "barrage", skipped="SWEEP_BARRAGE=0"),
        _line("report", "verify", "report", 0, html="verify.html"),
    )
    read = harness.read_steps(steps)
    assert [s["step"] for s in read] == ["compose-up", "verify", "barrage"]
    compose, verify, barrage = read
    assert compose["meaning"] == "completed"
    assert verify["exit_code"] == 3
    assert verify["meaning"] == "ran, answer was no"
    assert verify["html"] == "verify.html" and verify["report_exit_code"] == 0
    assert verify["command"] == "testinghq verify fire --send --note 'a\tb'"
    assert barrage["exit_code"] is None and barrage["skipped"] == "SWEEP_BARRAGE=0"


def test_summary_writes_the_shape_and_exits_1_only_on_error(tmp_path, capsys):
    steps = _write_steps(
        tmp_path / "steps.tsv",
        _line("step", "ledger", "ledger", 3, artifact="ledger.json"),
    )
    out = tmp_path / "summary.json"
    argv = ["summary", str(steps), "--out", str(out), "--helpdesk", "zammad",
            "--started", "2026-10-19T00:00:00Z", "--finished", "2026-10-19T00:05:00Z",
            "--version", "1.0.0", "--config", "sweep/configs/zammad.toml",
            "--target", "zammad", "--setting", "SWEEP_MAX_WAIT=180"]
    assert harness.main(argv) == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["helpdesk"] == "zammad"
    assert summary["testinghq_version"] == "1.0.0"
    assert summary["settings"] == {"SWEEP_MAX_WAIT": "180"}
    assert summary["result"] == "findings" and summary["exit_code"] == 0
    assert summary["completed_exit_codes"] == [0, 3]
    assert set(summary["steps"][0]) == {
        "step", "tool", "command", "exit_code", "meaning", "artifact", "html",
        "report_exit_code", "log", "skipped",
    }

    _write_steps(steps, _line("step", "ledger", "ledger", 1))
    assert harness.main(argv) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["result"] == "error"
    assert "ledger" in capsys.readouterr().out


def test_relativize_strips_the_repository_root_from_artifact_strings(tmp_path):
    root = tmp_path / "repo"
    run = root / "sweep" / "runs" / "mailpit" / "t"
    run.mkdir(parents=True)
    absolute = f"{root.resolve().as_posix()}/sweep/runs/mailpit/t/outbound.jsonl"
    (run / "loop.json").write_text(json.dumps({
        "config": {"outbound": {"path": absolute}},
        "notes": [absolute, "http://127.0.0.1:8025/api/v1/search"],
    }), encoding="utf-8")
    (run / "ledger.json").write_text(json.dumps({"path": "relative.jsonl"}), encoding="utf-8")
    summary = json.dumps({"config": absolute})
    (run / "summary.json").write_text(summary, encoding="utf-8")

    assert harness.relativize_artifacts(run, root) == ["loop.json"]
    loop = json.loads((run / "loop.json").read_text(encoding="utf-8"))
    assert loop["config"]["outbound"]["path"] == "sweep/runs/mailpit/t/outbound.jsonl"
    assert loop["notes"][1] == "http://127.0.0.1:8025/api/v1/search"
    assert (run / "summary.json").read_text(encoding="utf-8") == summary


def test_extract_unpacks_a_tarball_and_keeps_the_binary_executable(tmp_path):
    archive = tmp_path / "mailpit-linux-amd64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("mailpit")
        info.size, info.mode = len(data), 0o755
        bundle.addfile(info, io.BytesIO(data))
    harness.extract(archive, tmp_path / "bin")
    binary = tmp_path / "bin" / "mailpit"
    assert binary.read_bytes() == b"#!/bin/sh\n"
    assert binary.stat().st_mode & 0o100


def test_extract_refuses_a_member_that_escapes_the_destination(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../escaped")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(SystemExit, match="outside"):
        harness.extract(archive, tmp_path / "bin")
    assert not (tmp_path / "escaped").exists()


def test_sha256_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "blob"
    path.write_bytes(b"mailpit" * 1000)
    assert harness.sha256_of(path) == hashlib.sha256(b"mailpit" * 1000).hexdigest()
