"""The defects found reviewing the executor (#79), each pinned.

The executor made `--concurrency` real, and the first version of it measured a
saturated pool as a healthy target. Every test here is the specific failure
that shipped in review, so each one names what it used to do.
"""
from __future__ import annotations

import threading
import time

import pytest

from testinghq.barrage import fire
from testinghq.barrage.executor import PoolExecutor, SerialExecutor
from testinghq.barrage.runner import (
    DEFAULT_MAX_CONCURRENCY,
    RateCeilingError,
    _submit,
    check_rate_ceiling,
)
from testinghq.cli import main as cli_main


def _slow(seconds, starts=None):
    def send(index):
        if starts is not None:
            starts.append(index)
        time.sleep(seconds)
        return (f"result-{index}", seconds)

    return send


def test_a_request_that_waited_for_a_worker_records_when_it_really_went_out():
    """The first version stamped each record at hand-off. With one worker and
    three 50ms requests handed over at once, all three were reported as sent at
    the same instant: achieved throughput equal to the target, and a pool that
    was the bottleneck invisible."""
    executor = PoolExecutor(1)
    records = []
    handed_off = time.monotonic()
    try:
        for index in range(3):
            _submit(executor, _slow(0.05), index, handed_off, 20.0, 0.0,
                    records, time.monotonic)
        executor.drain()
    finally:
        executor.shutdown()

    assert [r.index for r in records] == [0, 1, 2], "order is the plan's, not the pool's"
    assert [r.result for r in records] == ["result-0", "result-1", "result-2"]
    times = [r.dispatch_time for r in records]
    assert times == sorted(times)
    assert times[2] - times[0] >= 0.08, "the third went out after the first two, not with them"
    assert records[0].queued < 0.03
    assert records[2].queued >= 0.08


def test_dispatch_summary_names_the_pool_as_the_bottleneck():
    executor = PoolExecutor(1)
    records = []
    now = time.monotonic()
    try:
        for index in range(3):
            _submit(executor, _slow(0.03), index, now, 20.0, 0.0, records, time.monotonic)
        executor.drain()
    finally:
        executor.shutdown()

    summary = fire.dispatch_summary(records, 1)
    assert summary["queued_requests"] == 2
    assert summary["max_queue_wait_ms"] >= 50
    note = fire.format_dispatch_note(summary)
    assert "waited for a free worker" in note and "--concurrency" in note


def test_a_run_that_kept_its_schedule_prints_no_dispatch_note():
    assert fire.format_dispatch_note(
        {"concurrency": 4, "queued_requests": 0, "max_queue_wait_ms": 0.0}
    ) == ""


def test_serial_dispatch_does_not_swallow_ctrl_c():
    """SerialExecutor caught BaseException and deferred it to the next drain,
    so Ctrl-C was ignored until the stage finished sending."""
    def interrupted(_index):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SerialExecutor().submit(interrupted, 0)


def test_serial_dispatch_still_defers_an_ordinary_error_to_drain():
    def broken(_index):
        raise ValueError("transport fell over")

    executor = SerialExecutor()
    executor.submit(broken, 0)
    with pytest.raises(ValueError):
        executor.drain()


def test_aborting_a_run_cancels_what_is_still_queued():
    """On an error or Ctrl-C, `run` used to shut down with wait=True, which
    drained the queue: every request still waiting was sent to the target
    before the process could exit."""
    starts = []
    executor = PoolExecutor(1)
    for index in range(10):
        executor.submit(_slow(0.05, starts), index)
    executor.shutdown(wait=True, cancel_pending=True)
    assert len(starts) <= 2, f"{len(starts)} of 10 were sent after the abort"


def test_result_write_back_does_not_scan_the_run():
    """Each completion used to search the whole record list for its index.
    Quadratic: at the default ceiling, 15,000 requests and over a hundred
    million comparisons on the threads meant to be sending. Writing back by
    position is flat, which this checks by timing 5,000 serial submissions."""
    executor = SerialExecutor()
    records = []
    started = time.monotonic()
    for index in range(5000):
        _submit(executor, lambda i: (i, 0.0), index, 0.0, 50.0, 0.0, records, time.monotonic)
    executor.drain()
    assert time.monotonic() - started < 1.5
    assert all(r.result == r.index for r in records)


def test_concurrency_has_a_ceiling_like_rate_and_duration():
    """The executor's docstring said the CLI capped concurrency. Nothing did:
    --concurrency 100000 built a pool that size."""
    check_rate_ceiling(10.0, 10.0, concurrency=DEFAULT_MAX_CONCURRENCY)
    with pytest.raises(RateCeilingError) as caught:
        check_rate_ceiling(10.0, 10.0, concurrency=DEFAULT_MAX_CONCURRENCY + 1)
    assert "--allow-high-rate" in str(caught.value)
    check_rate_ceiling(10.0, 10.0, allow_high_rate=True,
                       concurrency=DEFAULT_MAX_CONCURRENCY + 1)


@pytest.mark.parametrize("bad", [0, -3])
def test_a_concurrency_below_one_is_refused_even_with_the_opt_in(bad):
    with pytest.raises(RateCeilingError):
        check_rate_ceiling(10.0, 10.0, allow_high_rate=True, concurrency=bad)


def test_the_cli_refuses_an_over_ceiling_concurrency_before_announcing(capsys):
    code = cli_main(["barrage", "fire", "--target", "local", "--rate", "5",
                     "--duration", "10", "--warmup", "2", "--concurrency", "500"])
    captured = capsys.readouterr()
    assert code == 1
    assert "refused" in captured.err and "concurrency" in captured.err
    assert "barrage fire:" not in captured.out


def test_the_cli_allows_it_with_the_opt_in(capsys):
    code = cli_main(["barrage", "fire", "--target", "local", "--rate", "5",
                     "--duration", "10", "--warmup", "2", "--concurrency", "500",
                     "--allow-high-rate"])
    assert code == 2, capsys.readouterr().err
