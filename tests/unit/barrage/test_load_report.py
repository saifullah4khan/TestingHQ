"""Barrage reporting math, tested on synthetic inputs only.

Every sample here is hand-built, so no test needs a clock, a network, or a
real run. The reporting math is pure, so it can be pinned exactly.
"""
import pytest

from testinghq.barrage.report import (
    Sample,
    bucketize,
    build_artifact,
    error_rate,
    find_knee,
    format_summary,
    latency_percentiles,
    percentile,
    throughput,
)


def _sample(dispatch_time, latency_ms=10.0, status=200, target_rate=10.0):
    return Sample(
        dispatch_time=dispatch_time,
        latency_ms=latency_ms,
        status=status,
        target_rate=target_rate,
    )


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------


def test_percentile_of_empty_is_none_not_zero():
    # A silent 0.0 would read as "extremely fast", which is the opposite of
    # the truth when there is no data at all.
    assert percentile([], 0.5) is None


def test_percentile_uses_nearest_rank_and_returns_an_observed_value():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.9) == 50.0
    assert percentile(values, 1.0) == 50.0
    # Every result is a value that actually appears in the input.
    for fraction in (0.1, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert percentile(values, fraction) in values


def test_percentile_is_order_independent():
    assert percentile([50.0, 10.0, 30.0, 20.0, 40.0], 0.5) == 30.0


def test_percentile_single_value():
    assert percentile([7.0], 0.5) == 7.0
    assert percentile([7.0], 0.99) == 7.0


def test_percentile_rejects_out_of_range_fraction():
    with pytest.raises(ValueError):
        percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_percentile_p99_of_100_values_picks_the_99th():
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0.99) == 99.0
    assert percentile(values, 0.5) == 50.0
    assert percentile(values, 0.9) == 90.0


# ---------------------------------------------------------------------------
# latency_percentiles
# ---------------------------------------------------------------------------


def test_latency_percentiles_over_synthetic_distribution():
    samples = [_sample(0.0, latency_ms=float(i)) for i in range(1, 101)]
    result = latency_percentiles(samples)
    assert result == {"p50": 50.0, "p90": 90.0, "p99": 99.0}


def test_latency_percentiles_exclude_timeouts_from_the_distribution():
    # A timeout's latency reflects the timeout setting, not the endpoint's
    # speed. Including it would skew every percentile toward the timeout.
    samples = [_sample(0.0, latency_ms=10.0) for _ in range(9)]
    samples.append(_sample(0.0, latency_ms=10000.0, status=None))
    result = latency_percentiles(samples)
    assert result["p50"] == 10.0
    assert result["p99"] == 10.0


def test_latency_percentiles_of_no_responses_is_none():
    samples = [_sample(0.0, latency_ms=5000.0, status=None) for _ in range(3)]
    assert latency_percentiles(samples) == {"p50": None, "p90": None, "p99": None}


# ---------------------------------------------------------------------------
# error_rate
# ---------------------------------------------------------------------------


def test_error_rate_counts_5xx_and_timeouts_but_not_4xx():
    samples = [
        _sample(0.0, status=200),
        _sample(0.0, status=400),  # endpoint correctly rejecting, not a failure
        _sample(0.0, status=500),
        _sample(0.0, status=None),
    ]
    assert error_rate(samples) == 0.5


def test_error_rate_of_empty_is_zero():
    assert error_rate([]) == 0.0


def test_error_rate_all_clean_is_zero():
    assert error_rate([_sample(0.0) for _ in range(5)]) == 0.0


# ---------------------------------------------------------------------------
# bucketize
# ---------------------------------------------------------------------------


def test_bucketize_groups_by_second_and_computes_achieved_rate():
    samples = [_sample(0.1), _sample(0.5), _sample(1.2), _sample(1.7), _sample(1.9)]
    buckets = bucketize(samples, bucket_seconds=1.0)
    assert len(buckets) == 2
    assert buckets[0].count == 2
    assert buckets[0].achieved_rate == 2.0
    assert buckets[1].count == 3
    assert buckets[1].achieved_rate == 3.0


def test_bucketize_is_relative_to_the_earliest_dispatch():
    samples = [_sample(100.0), _sample(100.5), _sample(101.5)]
    buckets = bucketize(samples, bucket_seconds=1.0)
    assert len(buckets) == 2
    assert buckets[0].start == 100.0


def test_bucketize_emits_empty_middle_buckets_rather_than_skipping_them():
    # A stretch where the endpoint accepted nothing is exactly what this
    # report exists to show; dropping the bucket would hide it.
    samples = [_sample(0.0), _sample(3.5)]
    buckets = bucketize(samples, bucket_seconds=1.0)
    assert len(buckets) == 4
    assert [b.count for b in buckets] == [1, 0, 0, 1]
    assert buckets[1].achieved_rate == 0.0


def test_bucketize_carries_targeted_rate_across_an_empty_bucket():
    samples = [_sample(0.0, target_rate=10.0), _sample(3.5, target_rate=10.0)]
    buckets = bucketize(samples, bucket_seconds=1.0)
    assert [b.targeted_rate for b in buckets] == [10.0, 10.0, 10.0, 10.0]


def test_bucketize_computes_per_bucket_error_rate():
    samples = [
        _sample(0.1, status=200),
        _sample(0.2, status=500),
        _sample(1.1, status=500),
        _sample(1.2, status=500),
    ]
    buckets = bucketize(samples, bucket_seconds=1.0)
    assert buckets[0].error_rate == 0.5
    assert buckets[1].error_rate == 1.0


def test_bucketize_of_empty_is_empty():
    assert bucketize([]) == []


def test_bucketize_rejects_non_positive_bucket_width():
    with pytest.raises(ValueError):
        bucketize([_sample(0.0)], bucket_seconds=0)


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------


def test_throughput_reports_achieved_versus_targeted():
    # Targeted 10/s, but only 5 dispatched over a 2 second span.
    samples = [_sample(float(i) * 0.4, target_rate=10.0) for i in range(5)]
    result = throughput(samples, bucket_seconds=1.0)
    assert result["targeted_rps"] == 10.0
    assert result["total_requests"] == 5
    assert result["achieved_rps"] == pytest.approx(2.5)


def test_throughput_of_empty_reports_none_not_zero():
    result = throughput([])
    assert result == {"targeted_rps": None, "achieved_rps": None, "total_requests": 0}


def test_throughput_achieved_matches_targeted_for_a_healthy_run():
    samples = [_sample(float(i) * 0.1, target_rate=10.0) for i in range(20)]
    result = throughput(samples, bucket_seconds=1.0)
    assert result["achieved_rps"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# find_knee
# ---------------------------------------------------------------------------


def test_find_knee_returns_none_when_the_endpoint_held():
    samples = [_sample(float(i) * 0.1, latency_ms=10.0) for i in range(50)]
    assert find_knee(bucketize(samples)) is None


def test_find_knee_detects_shedding_when_errors_climb():
    healthy = [_sample(float(i) * 0.1, status=200) for i in range(10)]
    shedding = [_sample(1.0 + float(i) * 0.1, status=500) for i in range(10)]
    knee = find_knee(bucketize(healthy + shedding))
    assert knee is not None
    assert knee["reason"] == "shedding"
    assert knee["at_seconds"] == pytest.approx(1.0)


def test_find_knee_detects_slowing_when_latency_degrades():
    healthy = [_sample(float(i) * 0.1, latency_ms=10.0) for i in range(10)]
    slow = [_sample(1.0 + float(i) * 0.1, latency_ms=100.0) for i in range(10)]
    knee = find_knee(bucketize(healthy + slow))
    assert knee is not None
    assert knee["reason"] == "slowing"
    assert knee["at_seconds"] == pytest.approx(1.0)


def test_find_knee_does_not_fire_on_latency_noise_below_the_factor():
    healthy = [_sample(float(i) * 0.1, latency_ms=10.0) for i in range(10)]
    noisy = [_sample(1.0 + float(i) * 0.1, latency_ms=15.0) for i in range(10)]
    assert find_knee(bucketize(healthy + noisy)) is None


def test_find_knee_reports_shedding_first_when_both_signals_fire():
    healthy = [_sample(float(i) * 0.1, latency_ms=10.0) for i in range(10)]
    broken = [_sample(1.0 + float(i) * 0.1, latency_ms=500.0, status=500) for i in range(10)]
    knee = find_knee(bucketize(healthy + broken))
    assert knee["reason"] == "shedding"


def test_find_knee_of_empty_is_none():
    assert find_knee([]) is None


def test_find_knee_thresholds_are_configurable():
    healthy = [_sample(float(i) * 0.1, status=200) for i in range(10)]
    # One error in ten is 10%, above a 5% threshold but below a 50% one.
    mixed = [_sample(1.0 + float(i) * 0.1, status=500 if i == 0 else 200) for i in range(10)]
    buckets = bucketize(healthy + mixed)
    assert find_knee(buckets, error_threshold=0.05) is not None
    assert find_knee(buckets, error_threshold=0.5) is None


# ---------------------------------------------------------------------------
# build_artifact / format_summary
# ---------------------------------------------------------------------------


def test_build_artifact_shape_and_determinism():
    samples = [_sample(float(i) * 0.1) for i in range(20)]
    config = {"target": "local", "rate": 10.0}
    first = build_artifact(7, config, samples)
    second = build_artifact(7, config, samples)
    assert first == second
    assert first["seed"] == 7
    assert first["config"] == config
    assert set(first["summary"]) == {"throughput", "latency_ms", "error_rate", "knee"}
    assert first["buckets"]


def test_build_artifact_is_json_serializable():
    import json

    samples = [_sample(float(i) * 0.1, status=500 if i > 15 else 200) for i in range(20)]
    artifact = build_artifact(1, {"target": "local"}, samples)
    # Round trips cleanly, so the JSON artifact on disk is exactly this.
    assert json.loads(json.dumps(artifact)) == artifact


def test_format_summary_reads_differently_for_a_healthy_and_a_failing_run():
    healthy = build_artifact(1, {}, [_sample(float(i) * 0.1) for i in range(20)])
    failing_samples = [_sample(float(i) * 0.1, status=200) for i in range(10)]
    failing_samples += [_sample(1.0 + float(i) * 0.1, status=500) for i in range(10)]
    failing = build_artifact(1, {}, failing_samples)

    healthy_text = format_summary(healthy)
    failing_text = format_summary(failing)

    assert "the endpoint held for the whole run" in healthy_text
    assert "began shedding" in failing_text
    assert healthy_text != failing_text


def test_format_summary_includes_percentiles_and_throughput():
    samples = [_sample(float(i) * 0.1, latency_ms=float(i)) for i in range(20)]
    text = format_summary(build_artifact(3, {}, samples))
    assert "p50:" in text
    assert "p90:" in text
    assert "p99:" in text
    assert "targeted:" in text
    assert "achieved:" in text
    assert "Error rate over time:" in text


def test_format_summary_handles_an_empty_run_without_crashing():
    text = format_summary(build_artifact(0, {}, []))
    assert "0 request(s)" in text
    assert "n/a" in text
