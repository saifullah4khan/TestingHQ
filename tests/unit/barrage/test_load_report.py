"""Barrage reporting math, on hand-built samples. No clock, no network."""
import json

import pytest

from testinghq.barrage.report import (
    Sample,
    bucketize,
    build_artifact,
    error_rate,
    find_knee,
    format_summary,
    latency_percentiles,
    latency_summary,
    percentile,
    request_counts,
    status_classes,
    throughput,
)
from testinghq.reporting import summarize


def _s(t, latency_ms=10.0, status=200, target_rate=10.0, outcome="completed",
       queued=0.0, index=0, in_hold=True):
    sent = outcome in ("completed", "abandoned")
    done = outcome == "completed"
    return Sample(
        scheduled_at=t - queued,
        dispatched_at=t if sent else None,
        status=status if done else None,
        service_ms=latency_ms if done else None,
        offered_ms=(latency_ms + queued * 1000.0) if done else None,
        target_rate=target_rate,
        outcome=outcome,
        index=index,
        in_hold=in_hold,
    )


def _steady(n, start=0.0, step=0.1, **kw):
    return [_s(start + i * step, index=i, **kw) for i in range(n)]


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------


def test_percentile_of_empty_is_none_not_zero():
    assert percentile([], 0.5) is None


def test_percentile_uses_nearest_rank_and_returns_an_observed_value():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.9) == 50.0
    for fraction in (0.1, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert percentile(values, fraction) in values


def test_percentile_p99_of_100_values_picks_the_99th():
    values = [float(i) for i in range(1, 101)]
    assert (percentile(values, 0.5), percentile(values, 0.9), percentile(values, 0.99)) == (
        50.0, 90.0, 99.0)


def test_percentile_rejects_out_of_range_fraction():
    with pytest.raises(ValueError):
        percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


# ---------------------------------------------------------------------------
# Latency: service and offered, overall and hold
# ---------------------------------------------------------------------------


def test_service_and_offered_latency_are_separate_series():
    """Offered includes the time a request waited before it went out, so a
    queue shows there and not in service latency. That is R8: queue wait used
    to be a count and a max, never part of a percentile."""
    samples = [_s(i * 0.1, latency_ms=10.0, queued=i * 0.05) for i in range(10)]
    assert latency_percentiles(samples, "service")["p99"] == 10.0
    assert latency_percentiles(samples, "offered")["p99"] == pytest.approx(10.0 + 450.0)


def test_latency_is_reported_overall_and_for_the_hold_only():
    ramp = [_s(i * 0.1, latency_ms=100.0, in_hold=False) for i in range(10)]
    hold = [_s(1 + i * 0.1, latency_ms=10.0) for i in range(10)]
    summary = latency_summary(ramp + hold)
    assert summary["service"]["overall"]["p99"] == 100.0
    assert summary["service"]["hold"] == {"p50": 10.0, "p90": 10.0, "p99": 10.0}


def test_latency_excludes_requests_without_a_response():
    samples = _steady(9) + [_s(1.0, status=None, latency_ms=10000.0)]
    assert latency_percentiles(samples)["p99"] == 10.0


# ---------------------------------------------------------------------------
# Errors, shedding, and the status mix
# ---------------------------------------------------------------------------


def test_429_counts_as_an_error_and_is_named_shed():
    samples = [_s(0.0, status=200), _s(0.1, status=400), _s(0.2, status=429),
               _s(0.3, status=503), _s(0.4, status=None)]
    assert error_rate(samples) == pytest.approx(3 / 5)
    assert sum(1 for s in samples if s.is_shed) == 1
    artifact = build_artifact(1, {}, samples)
    assert artifact["summary"]["shed"] == 1


def test_an_endpoint_that_sheds_with_429_has_a_knee():
    samples = _steady(20, status=200) + _steady(20, start=2.0, status=429)
    knee = find_knee(bucketize(samples))
    assert knee is not None and knee["reason"] == "shedding"
    assert knee["at_seconds"] == pytest.approx(2.0)
    assert "shed 429 10" in knee["detail"]


def test_status_classes_cover_every_sent_request():
    samples = [_s(0.0, status=204), _s(0.1, status=302), _s(0.2, status=404),
               _s(0.3, status=500), _s(0.4, status=None), _s(0.5, outcome="abandoned"),
               _s(0.6, outcome="dropped_backlog")]
    assert status_classes(samples) == {
        "1xx": 0, "2xx": 1, "3xx": 1, "4xx": 1, "5xx": 1, "no_response": 2,
    }


def test_abandoned_counts_as_an_error_and_dropped_is_not_sent():
    samples = [_s(0.0), _s(0.1, outcome="abandoned"), _s(0.2, outcome="dropped_backlog"),
               _s(0.3, outcome="not_dispatched")]
    assert error_rate(samples) == pytest.approx(0.5)
    assert request_counts(samples) == {
        "scheduled": 4, "sent": 2, "completed": 1, "abandoned": 1,
        "dropped_backlog": 1, "not_dispatched": 1,
    }


def test_bucket_histograms_and_percentiles():
    samples = [_s(0.1, latency_ms=float(i), status=200 if i < 8 else 503) for i in range(10)]
    bucket = bucketize(samples)[0]
    assert bucket.status_classes["2xx"] == 8 and bucket.status_classes["5xx"] == 2
    assert bucket.service_ms == {"p50": 4.0, "p90": 8.0, "p99": 9.0}
    assert bucket.offered_ms["p99"] == 9.0


# ---------------------------------------------------------------------------
# Throughput over the real dispatch span
# ---------------------------------------------------------------------------


def test_achieved_rps_is_over_the_real_dispatch_span_not_whole_buckets():
    """25 requests exactly 0.1s apart are 10/s. Dividing by whole buckets
    reported 8.33."""
    result = throughput(_steady(25))
    assert result["achieved_rps"] == pytest.approx(10.0)
    assert result["dispatch_span_seconds"] == pytest.approx(2.4)
    assert result["total_requests"] == 25


def test_throughput_with_fewer_than_two_sent_has_no_rate():
    assert throughput([_s(0.0)])["achieved_rps"] is None
    assert throughput([])["achieved_rps"] is None


# ---------------------------------------------------------------------------
# Buckets and their targets
# ---------------------------------------------------------------------------


def test_bucketize_emits_empty_middle_buckets():
    buckets = bucketize([_s(0.0), _s(3.5)])
    assert [b.count for b in buckets] == [1, 0, 0, 1]


def test_bucket_targets_come_from_the_plan_when_stages_are_given():
    """A bucket spanning several ramp steps is judged against what the plan
    asked for in it, and the series covers the planned duration even where
    nothing went out."""
    stages = [(5.0, 0.5), (10.0, 0.5), (10.0, 2.0)]
    buckets = bucketize(_steady(5), origin=0.0, stages=stages, hold_start=1.0)
    assert len(buckets) == 3
    assert buckets[0].targeted_rate == pytest.approx(7.5)
    assert buckets[0].hold is False and buckets[1].hold is True
    assert [b.count for b in buckets] == [5, 0, 0]


def test_dropped_requests_land_in_the_bucket_they_were_due_in():
    samples = _steady(5) + [_s(1.2, outcome="dropped_backlog")]
    assert [b.dropped_backlog for b in bucketize(samples)] == [0, 1]


# ---------------------------------------------------------------------------
# The knee
# ---------------------------------------------------------------------------


def test_find_knee_returns_none_when_the_endpoint_held():
    assert find_knee(bucketize(_steady(50))) is None


def test_one_bad_bucket_is_not_a_knee():
    """Two-bucket persistence: one jittery second is not a breaking point."""
    samples = _steady(10) + _steady(10, start=1.0, status=500) + _steady(10, start=2.0)
    assert find_knee(bucketize(samples)) is None


def test_two_bad_buckets_are_a_knee_at_the_first():
    samples = _steady(10) + _steady(20, start=1.0, status=500)
    knee = find_knee(bucketize(samples))
    assert knee["reason"] == "shedding" and knee["at_seconds"] == pytest.approx(1.0)


def test_falling_behind_the_target_is_a_knee():
    """The target took 10/s for a second, then only 4/s of the 10 asked."""
    samples = _steady(10) + _steady(8, start=1.0, step=0.25)
    knee = find_knee(bucketize(samples, origin=0.0, stages=[(10.0, 3.0)]))
    assert knee is not None and knee["reason"] == "falling behind"
    assert knee["at_seconds"] == pytest.approx(1.0)


def test_slowing_is_measured_against_the_hold_baseline_not_the_first_request():
    """A slow first ramp request (a cold handshake) does not set the
    baseline; the hold stage's first two buckets do."""
    ramp = [_s(0.0, latency_ms=500.0, in_hold=False)]
    hold = _steady(20, start=1.0, latency_ms=10.0) + _steady(20, start=3.0, latency_ms=100.0)
    buckets = bucketize(ramp + hold, origin=0.0, hold_start=1.0)
    knee = find_knee(buckets)
    assert knee is not None and knee["reason"] == "slowing"
    assert knee["at_seconds"] == pytest.approx(3.0)
    assert "10.0ms hold baseline" in knee["detail"]


def test_find_knee_does_not_fire_on_latency_noise_below_the_factor():
    samples = _steady(20) + _steady(20, start=2.0, latency_ms=15.0)
    assert find_knee(bucketize(samples)) is None


def test_find_knee_of_empty_is_none():
    assert find_knee([]) is None


# ---------------------------------------------------------------------------
# Artifact and summary
# ---------------------------------------------------------------------------


def test_build_artifact_shape_and_determinism():
    samples = _steady(20)
    first = build_artifact(7, {"target": "local"}, samples)
    assert first == build_artifact(7, {"target": "local"}, samples)
    assert first["schema_version"] == 1 and first["tool"] == "barrage"
    assert first["aborted"] is False
    assert set(first["summary"]) == {
        "requests", "throughput", "latency_ms", "status_classes", "shed", "error_rate", "knee",
        "concurrency_limited",
    }
    assert json.loads(json.dumps(first)) == first


def test_records_carry_one_row_per_scheduled_request():
    samples = [_s(0.0, index=0), _s(0.1, index=1, outcome="abandoned"),
               _s(0.2, index=2, outcome="dropped_backlog")]
    rows = build_artifact(1, {}, samples)["records"]
    assert [set(r) for r in rows] == [{
        "index", "scheduled_at", "dispatched_at", "status", "service_ms", "offered_ms", "outcome",
    }] * 3
    assert [r["outcome"] for r in rows] == ["completed", "abandoned", "dropped_backlog"]
    assert rows[2]["dispatched_at"] is None and rows[1]["status"] is None


def test_format_summary_reads_differently_for_a_healthy_and_a_failing_run():
    healthy = format_summary(build_artifact(1, {}, _steady(20)))
    failing = format_summary(build_artifact(1, {}, _steady(10) + _steady(20, start=1.0, status=500)))
    assert "the endpoint held for the whole run" in healthy
    assert "began shedding" in failing


def test_format_summary_shows_both_latency_series_and_the_status_mix():
    text = format_summary(build_artifact(3, {"mode": "closed"}, _steady(20)))
    for needle in ("closed-loop", "service overall", "offered hold", "Status: 2xx 20",
                   "shed (429): 0", "achieved: 10.00/s", "Per bucket:"):
        assert needle in text, needle


def test_format_summary_flags_an_aborted_run():
    text = format_summary(build_artifact(1, {}, _steady(3), aborted=True))
    assert "ABORTED" in text


def test_format_summary_handles_an_empty_run_without_crashing():
    text = format_summary(build_artifact(0, {}, []))
    assert "0 request(s) sent" in text and "n/a" in text


# ---------------------------------------------------------------------------
# The `report` reader
# ---------------------------------------------------------------------------


def test_report_reads_a_completed_run_as_exit_zero_whatever_it_measured():
    summary = summarize(build_artifact(1, {}, _steady(10) + _steady(20, start=1.0, status=500)))
    assert summary.tool == "barrage"
    assert summary.verdict == "KNEE"
    assert summary.exit_code == 0


def test_report_reads_an_aborted_run_as_exit_three():
    summary = summarize(build_artifact(1, {}, _steady(3), aborted=True))
    assert summary.verdict == "ABORTED"
    assert summary.exit_code == 3
    assert summary.findings[0].kind == "aborted"


def test_report_names_what_the_deadline_and_backlog_cut():
    samples = _steady(5) + [_s(0.6, outcome="dropped_backlog"), _s(0.7, outcome="abandoned")]
    summary = summarize(build_artifact(1, {}, samples))
    kinds = {f.kind for f in summary.findings}
    assert {"dropped_backlog", "abandoned"} <= kinds
    assert summary.counts.sent == 6 and summary.counts.records == 7


def test_report_reads_a_barrage_artifact_without_schema_version_as_version_zero():
    legacy = build_artifact(1, {}, _steady(10))
    del legacy["schema_version"]
    assert summarize(legacy).verdict == "STEADY"


def test_report_refuses_a_barrage_artifact_from_a_newer_schema():
    from testinghq.reporting import ArtifactError

    newer = build_artifact(1, {}, _steady(10))
    newer["schema_version"] = 2
    with pytest.raises(ArtifactError, match="schema_version 2"):
        summarize(newer)


# ---------------------------------------------------------------------------
# Closed loop: a shortfall the workers caused is not the endpoint's knee
# ---------------------------------------------------------------------------


def _paced(rate, seconds, latency_ms, target_rate=20.0):
    """Requests dispatched evenly at `rate` per second for `seconds`."""
    n = int(rate * seconds)
    return [_s(i / rate, latency_ms=latency_ms, target_rate=target_rate, index=i)
            for i in range(n)]


CLOSED_2 = {"mode": "closed", "concurrency": 2}


def test_closed_loop_shortfall_at_the_worker_ceiling_is_not_a_knee():
    """2 workers at 200ms can offer at most 10/s. Sending 10/s of the 20/s
    asked is the client's limit, not the endpoint falling behind."""
    artifact = build_artifact(1, CLOSED_2, _paced(10, 4, 200.0),
                              stages=[(20.0, 4.0)], origin=0.0)
    summary = artifact["summary"]
    assert summary["knee"] is None
    limited = summary["concurrency_limited"]
    assert limited["concurrency"] == 2 and limited["buckets"] == 4
    assert limited["ceiling_rps"] == pytest.approx(10.0)
    assert limited["targeted_rps"] == pytest.approx(20.0)
    text = format_summary(artifact)
    assert "falling behind" not in text
    assert "limited by --concurrency" in text


def test_the_same_shortfall_in_open_loop_is_still_a_knee():
    """Open loop has no worker ceiling on its schedule, so the rule there is
    unchanged."""
    artifact = build_artifact(1, {"mode": "open", "concurrency": 2}, _paced(10, 4, 200.0),
                              stages=[(20.0, 4.0)], origin=0.0)
    assert artifact["summary"]["knee"]["reason"] == "falling behind"
    assert artifact["summary"]["concurrency_limited"] is None


def test_closed_loop_still_reports_an_endpoint_that_falls_below_the_ceiling():
    """At 200ms two workers could send 10/s. Sending 4/s is below that, so the
    shortfall is not explained by the workers and is still a knee."""
    artifact = build_artifact(1, CLOSED_2, _paced(4, 4, 200.0),
                              stages=[(20.0, 4.0)], origin=0.0)
    assert artifact["summary"]["knee"]["reason"] == "falling behind"
    assert artifact["summary"]["concurrency_limited"] is None


def test_a_short_last_bucket_is_held_to_what_the_workers_fit_in_it():
    """A 3.3s plan ends with a 0.3s bucket. Two workers at 200ms fit 3 requests
    in it, so sending 3 of the 6 planned is the client's limit too."""
    artifact = build_artifact(1, CLOSED_2, _paced(10, 3.3, 200.0),
                              stages=[(20.0, 3.3)], origin=0.0)
    summary = artifact["summary"]
    assert summary["knee"] is None
    assert summary["concurrency_limited"]["buckets"] == 4
    assert summary["concurrency_limited"]["targeted_rps"] == pytest.approx(20.0)
