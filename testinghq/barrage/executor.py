"""Dispatch executors for Barrage.

Until this module existed, `barrage/runner.py` called `send_fn` inline. One
request in flight, always, in both modes. That is not a load tester: achieved
throughput is bounded by the target's response time rather than by the rate you
asked for, so a p90 over a 200ms target can never exceed 5 requests/second no
matter what `--rate` says, and no knee is findable because the target is never
asked for enough simultaneous work to fall over.

This module provides the two dispatch strategies, behind one interface, so
`runner.py` can stay the only thing that knows about pacing.

WHY THREADS AND NOT ASYNCIO, since the fix is often reached for with
asyncio.to_thread and it is worth saying why this is not that.

The transport is a blocking `urllib.request.urlopen`. Wrapping it in asyncio
buys nothing without an async HTTP client, and an async client is a new runtime
dependency for a project that currently has exactly one (`tomli`, and only on
Python 3.10). `asyncio.to_thread` is a thread pool with an event loop wrapped
around it, so it would be the same concurrency for a worse dependency story and
a less obvious shutdown path. A `ThreadPoolExecutor` is the honest instrument
for a blocking client.

The cost of threads is real and is why `concurrency` stays a small number: a
thread per in-flight request means one interpreter stack per request, so a
run configured for thousands of concurrent requests would not do what it says.
The CLI cap is on `concurrency`, not on the rate, and the README says so.

INTERFACE, and why it exists at all. `SerialExecutor` is not a testing
convenience bolted on: it is the correct implementation for `concurrency == 1`,
and it is what keeps every existing hermetic test passing unchanged. A test that
injects a fake clock and a fake `send_fn` must not need a real thread pool to
prove what it is proving, and with `concurrency == 1` there is nothing to prove
about concurrency anyway.

The interface is deliberately tiny: `submit`, `drain`, `shutdown`. `drain` is
the one that matters and is easy to get wrong. A stage's requests may still be
in flight when the stage's window closes, and a run that reports before they
land reports a shorter run than it performed.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

#: What every executor can do. `submit` returns a Future so the caller can hold
#: on to a request across a stage boundary; `drain` waits for everything
#: submitted so far; `shutdown` releases the workers.
SendFn = Callable[[int], Tuple[object, float]]


class _Completed(Future):
    """A Future that is already resolved.

    `concurrent.futures.Future` cannot be constructed in a resolved state, so
    serial dispatch resolves one of these instead. Subclassing rather than
    wrapping means callers get the same interface from both executors, and
    `result()` / `exception()` behave identically.
    """

    def __init__(self, value=None, exception: Optional[BaseException] = None):
        super().__init__()
        if exception is not None:
            self.set_exception(exception)
        else:
            self.set_result(value)

    def set_running_or_notify_cancel(self):  # pragma: no cover - Future API
        return None

    def cancel(self):  # pragma: no cover - already done, cannot cancel
        return False


class SerialExecutor:
    """Runs each request inline, immediately, on the calling thread.

    Correct for `concurrency == 1`, where "one in flight" is the specification
    rather than a limitation. Also the executor every hermetic test gets by
    default, because a thread pool would make a fake clock and a fake
    `send_fn` race each other for no benefit: with one worker there is nothing
    to overlap.
    """

    #: Recorded rather than inferred, so a test can assert what actually
    #: happened instead of trusting that nothing was dispatched in parallel.
    max_in_flight = 1

    def __init__(self) -> None:
        self._pending: List[Future] = []

    def submit(self, fn, *args) -> Future:
        try:
            value = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            future = _Completed(exception=exc)
        else:
            future = _Completed(value=value)
        self._pending.append(future)
        return future

    def drain(self) -> None:
        for future in self._pending:
            future.result()
        self._pending.clear()

    def shutdown(self, wait: bool = True) -> None:
        if wait:
            self.drain()


class PoolExecutor:
    """A fixed pool of worker threads, which is what makes `--concurrency` mean
    something.

    `max_workers` is the number of requests in flight at once. The pool's own
    queue provides the backpressure: when every worker is busy, submitted work
    waits rather than being rejected, so the caller keeps offering load on
    schedule and the pool is what limits how much of it is actually in flight.
    That is the closed-loop property, arrived at by construction instead of by
    the `free_at` arithmetic that used to simulate it.

    Threads are created lazily and named, so a hung run is diagnosable from a
    thread dump rather than being a wall of `Thread-N (process)`.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str = "barrage") -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers!r}")
        self.max_in_flight = max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._pending: List[Future] = []
        self._lock = threading.Lock()

    def submit(self, fn, *args) -> Future:
        future = self._pool.submit(fn, *args)
        with self._lock:
            self._pending.append(future)
        return future

    def drain(self) -> None:
        """Wait for every submitted request, in submission order.

        Submission order rather than completion order, deliberately: a future
        that completes first is not the one whose exception the operator wants
        first, and waiting in order means the first failure reported is the
        first request that failed, which is the one whose index is lowest.
        """
        while True:
            with self._lock:
                pending, self._pending = self._pending, []
            if not pending:
                return
            for future in pending:
                future.result()

    def shutdown(self, wait: bool = True) -> None:
        try:
            if wait:
                self.drain()
        finally:
            self._pool.shutdown(wait=wait)


def make_executor(concurrency: int) -> SerialExecutor | PoolExecutor:
    """The executor a run with this `concurrency` should use.

    `concurrency == 1` gets the serial one, not a pool of one. A pool of one
    would add a thread, a lock and a Future to every request for no observable
    difference, and it would break every existing test that injects a fake
    `send_fn` and asserts on a deterministic sequence.
    """
    if concurrency == 1:
        return SerialExecutor()
    return PoolExecutor(concurrency)
