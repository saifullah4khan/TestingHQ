"""`testinghq compare` on two barrage artifacts: load metrics, not payloads.

Every test goes through the command with small hand-built artifacts in the
shape barrage writes (one `records` row per scheduled request).
"""
from __future__ import annotations

import json

import pytest

from testinghq.barrage import report as barrage_report
from testinghq.cli import main
from testinghq.compare import load
from testinghq.core.exit_codes import EXIT_FINDING, EXIT_OK, EXIT_REFUSED

from test_compare import _artifact as _blast_artifact
from test_compare import _record as _blast_record


def _row(i, status=200, service=10.0, outcome="completed", spacing=0.1):
    sent = outcome in ("completed", "abandoned")
    done = outcome == "completed"
    return {
        "index": i,
        "scheduled_at": round(i * 0.1, 6),
        "dispatched_at": round(i * spacing, 6) if sent else None,
        "status": status if done else None,
        "service_ms": service if done else None,
        "offered_ms": service + 1.0 if done else None,
        "outcome": outcome,
    }


def _barrage(rows=None, n=100, rate=10.0, mode="open", dry_run=False, seed=3, **extra):
    rows = rows if rows is not None else [_row(i) for i in range(n)]
    artifact = {
        "schema_version": 1, "tool": "barrage", "aborted": False, "seed": seed,
        "config": {"mode": mode, "rate": rate, "duration": 10.0, "warmup": 0.0,
                   "ramp_steps": 10, "concurrency": 4, "seed": seed, "pool_size": 4,
                   "target": "local", "keepalive": True, "timeout": 10.0,
                   "dry_run": dry_run},
        "summary": {}, "buckets": [], "records": rows,
    }
    artifact.update(extra)
    return artifact


def _with(n=100, **kinds):
    """n completed 200s at 10ms, the first few replaced: kinds maps a count to
    a row override, e.g. errors=dict(count=5, status=503)."""
    rows = [_row(i) for i in range(n)]
    at = 0
    for spec in kinds.values():
        spec = dict(spec)
        count = spec.pop("count")
        for i in range(at, at + count):
            rows[i] = _row(i, **spec)
        at += count
    return rows


def _write(tmp_path, name, artifact):
    path = tmp_path / name
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return str(path)


def _compare(tmp_path, capsys, baseline, candidate, *extra):
    out = tmp_path / "diff.json"
    code = main([
        "compare", "--baseline", _write(tmp_path, "base.json", baseline),
        "--candidate", _write(tmp_path, "cand.json", candidate),
        "--out", str(out), *extra,
    ])
    captured = capsys.readouterr()
    diff = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, captured, diff


def _regressed_metrics(diff):
    return {entry["id"] for entry in diff["regressions"]["records"]}


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_identical_runs_report_no_regression(tmp_path, capsys):
    code, captured, diff = _compare(tmp_path, capsys, _barrage(), _barrage())
    assert code == EXIT_OK
    assert "compare (barrage): no regressions" in captured.out
    assert diff["kind"] == "barrage" and diff["regressed"] is False
    assert diff["comparable"] is True
    assert "thresholds:" in captured.out


@pytest.mark.parametrize("override, part", [
    (dict(status=503), "5xx_rate"),
    (dict(status=429), "shed_rate"),
    (dict(status=None), "no_response_rate"),
    (dict(outcome="abandoned"), "abandoned_rate"),
])
def test_each_kind_of_error_counts_toward_the_error_rate(tmp_path, capsys, override, part):
    candidate = _barrage(_with(bad=dict(count=5, **override)))
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(), candidate, "--fail-on-regression"
    )
    assert code == EXIT_FINDING
    assert "REGRESSED" in captured.out
    assert _regressed_metrics(diff) == {"error_rate"}
    rows = {row["metric"]: row for row in diff["metrics"]}
    assert rows["error_rate"]["candidate"] == pytest.approx(0.05)
    assert rows[part]["candidate"] == pytest.approx(0.05)


def test_a_regression_exits_zero_without_fail_on_regression(tmp_path, capsys):
    candidate = _barrage(_with(bad=dict(count=5, status=500)))
    code, captured, diff = _compare(tmp_path, capsys, _barrage(), candidate)
    assert code == EXIT_OK
    assert diff["regressed"] is True


def test_an_error_rate_rise_inside_one_point_is_not_a_regression(tmp_path, capsys):
    candidate = _barrage(_with(n=200, bad=dict(count=2, status=500)))  # +1.0 pt
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(n=200), candidate, "--fail-on-regression"
    )
    assert code == EXIT_OK
    assert diff["regressed"] is False
    assert "+1.0 pt" in captured.out


def test_requests_never_sent_are_an_unsent_rate_regression(tmp_path, capsys):
    candidate = _barrage(_with(
        dropped=dict(count=3, outcome="dropped_backlog"),
        late=dict(count=2, outcome="not_dispatched"),
    ))
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(), candidate, "--fail-on-regression"
    )
    assert code == EXIT_FINDING
    assert _regressed_metrics(diff) == {"unsent_rate"}
    rows = {row["metric"]: row for row in diff["metrics"]}
    assert rows["dropped_backlog_rate"]["candidate"] == pytest.approx(0.03)
    assert rows["not_dispatched_rate"]["candidate"] == pytest.approx(0.02)


def test_p99_latency_growth_beyond_the_margin_is_a_regression(tmp_path, capsys):
    candidate = _barrage(_with(slow=dict(count=2, service=40.0)))
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(), candidate, "--fail-on-regression"
    )
    assert code == EXIT_FINDING
    assert _regressed_metrics(diff) == {"service_p99_ms", "offered_p99_ms"}
    assert "service p99 (hold) 10.0ms -> 40.0ms" in captured.out


def test_a_small_absolute_p99_move_is_not_a_regression(tmp_path, capsys):
    """2ms to 4ms is 100% but under the 5ms floor: loopback noise."""
    baseline = _barrage([_row(i, service=2.0) for i in range(100)])
    candidate = _barrage(
        [_row(i, service=4.0 if i < 2 else 2.0) for i in range(100)]
    )
    code, _, diff = _compare(tmp_path, capsys, baseline, candidate, "--fail-on-regression")
    assert code == EXIT_OK
    assert diff["regressed"] is False


def test_p50_moves_are_reported_but_not_judged(tmp_path, capsys):
    candidate = _barrage([_row(i, service=10.0 if i < 2 else 30.0) for i in range(100)])
    baseline = _barrage([_row(i, service=10.0 if i >= 98 else 9.0) for i in range(100)])
    code, captured, diff = _compare(tmp_path, capsys, baseline, candidate)
    rows = {row["metric"]: row for row in diff["metrics"]}
    assert rows["service_p50_ms"]["judged"] is False
    assert rows["service_p50_ms"]["delta"] == pytest.approx(21.0)


def test_a_lower_achieved_rate_is_a_regression(tmp_path, capsys):
    slow = _barrage([_row(i, spacing=0.125) for i in range(100)])  # 8/s vs 10/s
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(), slow, "--fail-on-regression"
    )
    assert code == EXIT_FINDING
    assert _regressed_metrics(diff) == {"achieved_rps"}


def test_achieved_rate_is_not_judged_when_the_target_rates_differ(tmp_path, capsys):
    slow = _barrage([_row(i, spacing=0.2) for i in range(100)], rate=5.0)
    code, captured, diff = _compare(
        tmp_path, capsys, _barrage(), slow, "--fail-on-regression"
    )
    assert code == EXIT_OK
    assert diff["achieved_rps_judged"] is False
    assert diff["comparable"] is False
    assert "warning: rate differs (10.0 vs 5.0)" in captured.out
    assert "not judged: mode or rate differs" in captured.out


def test_a_better_candidate_is_an_improvement_not_a_regression(tmp_path, capsys):
    baseline = _barrage(_with(bad=dict(count=10, status=502)))
    code, captured, diff = _compare(
        tmp_path, capsys, baseline, _barrage(), "--fail-on-regression"
    )
    assert code == EXIT_OK
    assert diff["regressed"] is False
    assert diff["fixes"]["count"] == 1
    assert "IMPROVEMENTS: 1" in captured.out


def test_an_artifact_from_the_barrage_builder_compares(tmp_path, capsys):
    """The shape barrage really writes, not just the hand-built one."""
    samples = [
        barrage_report.Sample(scheduled_at=i * 0.1, dispatched_at=i * 0.1, status=200,
                              service_ms=10.0, offered_ms=11.0, target_rate=10.0, index=i)
        for i in range(50)
    ]
    config = _barrage()["config"]
    artifact = barrage_report.build_artifact(3, config, samples)
    code, captured, diff = _compare(tmp_path, capsys, artifact, artifact)
    assert code == EXIT_OK
    assert diff["requests"]["baseline"]["sent"] == 50


def test_report_reads_the_barrage_difference_report(tmp_path, capsys):
    candidate = _barrage(_with(bad=dict(count=5, status=500)))
    _, _, diff = _compare(tmp_path, capsys, _barrage(), candidate)
    path = _write(tmp_path, "diff-copy.json", diff)
    assert main(["report", path, "--json"]) == EXIT_OK
    summary = json.loads(capsys.readouterr().out)
    assert summary["tool"] == "compare"
    assert summary["verdict"] == "REGRESSED"
    assert summary["exit_code"] == EXIT_FINDING
    assert any("error rate 0.0% -> 5.0%" in f["detail"] for f in summary["findings"])


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _blast():
    return _blast_artifact([_blast_record("clean-1-0000", "clean", 200)])


@pytest.mark.parametrize("order", ["blast-first", "barrage-first"])
def test_mixing_a_blast_and_a_barrage_artifact_is_refused(tmp_path, capsys, order):
    pair = (_blast(), _barrage()) if order == "blast-first" else (_barrage(), _blast())
    code, captured, diff = _compare(tmp_path, capsys, *pair)
    assert code == EXIT_REFUSED
    assert "cannot compare a" in captured.err
    assert "blast artifact" in captured.err and "barrage artifact" in captured.err
    assert "Traceback" not in captured.err
    assert diff is None


def test_another_tools_artifact_is_refused(tmp_path, capsys):
    verify = {"schema_version": 1, "tool": "verify", "seed": 1,
              "config": {"tool": "verify", "seed": 1, "count": 1},
              "summary": {}, "records": []}
    code, captured, _ = _compare(tmp_path, capsys, verify, verify)
    assert code == EXIT_REFUSED
    assert "is a verify artifact" in captured.err


@pytest.mark.parametrize("side", ["baseline", "candidate"])
def test_a_barrage_dry_run_on_either_side_is_refused(tmp_path, capsys, side):
    pair = {"baseline": _barrage(), "candidate": _barrage()}
    pair[side] = _barrage(dry_run=True)
    code, captured, diff = _compare(tmp_path, capsys, pair["baseline"], pair["candidate"])
    assert code == EXIT_REFUSED
    assert "dry run" in captured.err
    assert diff is None


def test_a_barrage_artifact_without_records_is_refused(tmp_path, capsys):
    old = _barrage()
    del old["records"]
    code, captured, _ = _compare(tmp_path, capsys, old, _barrage())
    assert code == EXIT_REFUSED
    assert "no per-request 'records'" in captured.err


def test_a_run_that_sent_nothing_is_nothing_to_compare(tmp_path, capsys):
    nothing = _barrage([_row(i, outcome="not_dispatched") for i in range(10)])
    code, captured, _ = _compare(tmp_path, capsys, _barrage(), nothing)
    assert code == EXIT_REFUSED
    assert "nothing to compare" in captured.err


def test_a_newer_barrage_schema_is_refused(tmp_path, capsys):
    code, captured, _ = _compare(tmp_path, capsys, _barrage(schema_version=99), _barrage())
    assert code == EXIT_REFUSED
    assert "schema_version 99" in captured.err


def test_a_malformed_record_is_refused(tmp_path, capsys):
    bad = _barrage([{"index": 0, "outcome": "exploded", "scheduled_at": 0.0}])
    code, captured, _ = _compare(tmp_path, capsys, bad, _barrage())
    assert code == EXIT_REFUSED
    assert "record 0" in captured.err


def test_the_thresholds_are_stated_in_help(capsys):
    with pytest.raises(SystemExit):
        main(["compare", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "1 point" in out and "20%" in out and "10%" in out
    assert load.P99_MIN_MS == 5.0
