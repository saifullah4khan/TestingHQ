"""Executor behaviour and the dispatch summary, each pinned to a defect it
once had."""
from __future__ import annotations

import threading
import time

import pytest

from testinghq.barrage import fire
from testinghq.barrage.executor import PoolExecutor, SerialExecutor
from testinghq.barrage.runner import (
    DEFAULT_MAX_CONCURRENCY,
    RateCeilingError,
    RunPlan,
    check_rate_ceiling,
    run,
)
from testinghq.cli import main as cli_main


class FakeClock:
    def __init__(self):
        self.time = 0.0

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def test_serial_dispatch_does_not_swallow_ctrl_c():
    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SerialExecutor().submit(interrupted)


def test_serial_dispatch_parks_an_ordinary_error_on_the_future():
    def broken():
        raise ValueError("transport fell over")

    future = SerialExecutor().submit(broken)
    assert isinstance(future.exception(), ValueError)


def test_shutting_down_the_pool_cancels_what_is_still_queued():
    starts = []
    executor = PoolExecutor(1)

    def slow():
        starts.append(1)
        time.sleep(0.05)

    futures = [executor.submit(slow) for _ in range(10)]
    executor.shutdown(cancel_pending=True)
    time.sleep(0.12)
    assert len(starts) <= 2, f"{len(starts)} of 10 ran after the shutdown"
    assert sum(1 for f in futures if f.cancelled()) >= 8


def test_pool_workers_are_daemon_threads():
    """An abandoned request may still be blocked on a socket. A non-daemon
    worker would hold the interpreter open until its timeout."""
    executor = PoolExecutor(3)
    try:
        workers = [t for t in threading.enumerate() if t.name.startswith("barrage-")]
        assert workers and all(t.daemon for t in workers)
    finally:
        executor.shutdown()


def test_a_shut_down_pool_refuses_new_work():
    executor = PoolExecutor(1)
    executor.shutdown()
    with pytest.raises(RuntimeError):
        executor.submit(lambda: None)


def test_dispatch_summary_names_the_pool_as_the_bottleneck():
    """One worker, three 30ms requests due at once: two of them waited."""
    plan = RunPlan(mode="open", rate=100.0, concurrency=1, warmup_seconds=0.0,
                   hold_seconds=0.03, ramp_step_count=0)

    def slow(index):
        time.sleep(0.03)
        return index

    result = run(plan, slow, allow_high_rate=True, executor=PoolExecutor(1))
    summary = fire.dispatch_summary(result, plan)
    assert summary["queued_requests"] == 2
    assert summary["queue_wait_ms"]["max"] >= 30
    assert summary["queue_wait_ms"]["p50"] is not None
    note = fire.format_dispatch_note(summary)
    assert "waited for a free worker" in note and "--concurrency" in note


def test_a_run_that_kept_its_schedule_prints_no_dispatch_note():
    clock = FakeClock()
    plan = RunPlan(mode="open", rate=10.0, concurrency=1, warmup_seconds=0.0,
                   hold_seconds=1.0, ramp_step_count=0)
    result = run(plan, lambda i: i, clock=clock.now, sleep=clock.sleep)
    assert fire.format_dispatch_note(fire.dispatch_summary(result, plan)) == ""


def test_the_dispatch_note_names_what_the_deadline_and_backlog_cut():
    note = fire.format_dispatch_note({
        "concurrency": 2, "queued_requests": 0, "dropped_backlog": 4,
        "max_queue_seconds": 5.0, "abandoned": 2, "not_dispatched": 3,
    })
    assert "4 request(s) waited longer than 5s" in note
    assert "2 request(s) still in flight (abandoned)" in note
    assert "--grace" in note


def test_records_scale_linearly_with_the_run():
    """Result write-back used to scan the run per completion, which is
    quadratic. 5000 requests through the whole engine stay quick."""
    clock = FakeClock()
    plan = RunPlan(mode="open", rate=1000.0, concurrency=1, warmup_seconds=0.0,
                   hold_seconds=5.0, ramp_step_count=0)
    started = time.monotonic()
    result = run(plan, lambda i: i, clock=clock.now, sleep=clock.sleep, allow_high_rate=True)
    assert time.monotonic() - started < 1.5
    assert len(result.records) == 5000
    assert all(r.result == r.index for r in result.records)


def test_concurrency_has_a_ceiling_like_rate_and_duration():
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
