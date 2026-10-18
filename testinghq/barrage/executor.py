"""Dispatch executors for Barrage.

Two strategies behind one small interface, so `runner.py` stays the only thing
that knows about pacing:

- `SerialExecutor` runs each task inline on the calling thread. It is the
  correct implementation for `concurrency == 1` and what hermetic tests with a
  fake clock get, because one worker has nothing to overlap.
- `PoolExecutor` is a fixed set of daemon worker threads fed from a queue.

Threads rather than asyncio because the transport is a blocking `http.client`
call and the project takes no runtime dependencies. One thread per in-flight
request is why `concurrency` is capped (see `runner.check_rate_ceiling`).

Interface: `submit(fn) -> Future`, `shutdown(cancel_pending=...)`, and
`max_in_flight`. The runner waits on the futures itself, with a deadline, so
an executor never decides how long a run lasts.

The pool's threads are daemon threads on purpose. A request the run has
abandoned at its deadline may still be blocked in a socket read, bounded only
by the transport timeout; a non-daemon thread would hold the interpreter open
until it returned.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Callable, Set


class _Completed(Future):
    """A Future that is already resolved, for inline execution."""

    def __init__(self, value=None, exception=None):
        super().__init__()
        if exception is not None:
            self.set_exception(exception)
        else:
            self.set_result(value)


class SerialExecutor:
    """Runs each task inline, immediately, on the calling thread."""

    max_in_flight = 1

    def submit(self, fn: Callable[[], object]) -> Future:
        # Exception, not BaseException: a KeyboardInterrupt must stop the run
        # where it happened instead of being parked on a Future.
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced by the runner
            return _Completed(exception=exc)
        return _Completed(value=value)

    def shutdown(self, cancel_pending: bool = True) -> None:
        return None


class PoolExecutor:
    """A fixed pool of daemon worker threads.

    `max_workers` is the number of tasks running at once. Work submitted while
    every worker is busy waits in the queue; the task itself decides what to do
    about having waited (Barrage drops a request that waited too long).
    """

    def __init__(self, max_workers: int, thread_name_prefix: str = "barrage") -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers!r}")
        self.max_in_flight = max_workers
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._pending: Set[Future] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._threads = [
            threading.Thread(
                target=self._work, name=f"{thread_name_prefix}-{i}", daemon=True
            )
            for i in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn = item
            with self._lock:
                self._pending.discard(future)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - delivered via the Future
                future.set_exception(exc)
            else:
                future.set_result(result)

    def submit(self, fn: Callable[[], object]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot submit to an executor that was shut down")
            self._pending.add(future)
        self._queue.put((future, fn))
        return future

    def shutdown(self, cancel_pending: bool = True) -> None:
        """Stop accepting work and let the workers exit once idle.

        With `cancel_pending`, work still in the queue is cancelled and never
        runs. A task already running finishes on its own thread; it is not
        waited for, because a request on the wire cannot be recalled and the
        run has already decided what to call it.
        """
        with self._lock:
            self._closed = True
            pending, self._pending = self._pending, set()
        if cancel_pending:
            for future in pending:
                future.cancel()
        for _ in self._threads:
            self._queue.put(None)


def make_executor(concurrency: int) -> "SerialExecutor | PoolExecutor":
    """Serial for one worker, a pool otherwise."""
    if concurrency == 1:
        return SerialExecutor()
    return PoolExecutor(concurrency)
