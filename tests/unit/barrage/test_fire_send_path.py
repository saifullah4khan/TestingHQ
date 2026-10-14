"""barrage fire and replay through the CLI, with the socket classes faked.

These drive `cli.main` on a real clock, so the runner, the pool and the
per-worker clients are the shipped ones; only PersistentHttpClient and
UrllibHttpClient are replaced, which keeps every test off the network.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from testinghq import cli
from testinghq.barrage import fire as barrage_fire
from testinghq.barrage.executor import SerialExecutor
from testinghq.barrage.runner import arrival_schedule
from testinghq.core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.core.transport import ClientResponse


def _config(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text('[targets.local]\nurl = "http://127.0.0.1:8000/inbound"\n', encoding="utf-8")
    return str(path)


def _fire(tmp_path, *extra, out="run.json"):
    argv = [
        "barrage", "fire", "--target", "local", "--send", "--config", _config(tmp_path),
        "--rate", "20", "--duration", "0.4", "--warmup", "0",
        "--out", str(tmp_path / out), *extra,
    ]
    return cli.main(argv)


def _artifact(tmp_path, name="run.json"):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


class _Recorder:
    """Shared log for the fake clients below."""

    def __init__(self):
        self.lock = threading.Lock()
        self.instances = []
        self.connects = []
        self.requests = []


@pytest.fixture
def persistent(monkeypatch):
    """Replace PersistentHttpClient with a recording fake that answers 200."""
    log = _Recorder()

    class FakePersistent:
        def __init__(self):
            self.connect_seconds = []
            self.reconnects = 0
            self.thread = threading.get_ident()
            with log.lock:
                log.instances.append(self)

        def connect(self, url, timeout=10.0):
            with log.lock:
                log.connects.append((self, url, timeout))
            self.connect_seconds.append(0.001)
            return 0.001

        def send(self, request):
            assert threading.get_ident() == self.thread, "a client was shared between workers"
            with log.lock:
                log.requests.append((self, request))
            return ClientResponse(status=200, body=b"{}")

        def close(self):
            pass

    monkeypatch.setattr(barrage_fire, "PersistentHttpClient", FakePersistent)
    return log


@pytest.fixture
def urllib_client(monkeypatch):
    """Replace UrllibHttpClient with a recording fake that answers 200."""
    log = _Recorder()

    class FakeUrllib:
        def __init__(self):
            with log.lock:
                log.instances.append(self)

        def send(self, request):
            with log.lock:
                log.requests.append((self, request))
            return ClientResponse(status=200, body=b"{}")

    monkeypatch.setattr(barrage_fire, "UrllibHttpClient", FakeUrllib)
    return log


# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------


def test_each_worker_connects_once_and_reuses_its_connection(tmp_path, persistent, capsys):
    rc = _fire(tmp_path, "--mode", "closed", "--concurrency", "3")
    assert rc == EXIT_OK, capsys.readouterr().err

    data = _artifact(tmp_path)
    assert 1 <= len(persistent.instances) <= 3
    assert len(persistent.connects) == len(persistent.instances)
    assert all(timeout == 10.0 for _c, _u, timeout in persistent.connects)
    assert len(persistent.requests) == data["summary"]["requests"]["sent"] > len(persistent.instances)
    assert data["config"]["keepalive"] is True
    connections = data["connections"]
    assert connections["keepalive"] is True
    assert connections["workers_connected"] == len(persistent.instances)
    assert connections["setup_ms"]["p50"] == 1.0


def test_no_keepalive_uses_a_new_client_per_request(tmp_path, persistent, urllib_client, capsys):
    rc = _fire(tmp_path, "--no-keepalive", "--concurrency", "2")
    assert rc == EXIT_OK, capsys.readouterr().err

    data = _artifact(tmp_path)
    assert persistent.instances == []
    assert len(urllib_client.instances) == len(urllib_client.requests) == 8
    assert data["config"]["keepalive"] is False
    assert data["connections"]["keepalive"] is False


def test_a_failed_connect_costs_one_attempt_not_two(tmp_path, monkeypatch, capsys):
    attempts = []

    class Refusing:
        def __init__(self):
            self.connect_seconds = []
            self.reconnects = 0

        def connect(self, url, timeout=10.0):
            attempts.append("connect")
            raise ConnectionRefusedError("refused")

        def send(self, request):
            attempts.append("send")
            raise ConnectionRefusedError("refused")

        def close(self):
            pass

    monkeypatch.setattr(barrage_fire, "PersistentHttpClient", Refusing)
    rc = _fire(tmp_path, "--concurrency", "1")
    assert rc == EXIT_OK, capsys.readouterr().err

    data = _artifact(tmp_path)
    assert attempts.count("connect") == 1
    assert attempts.count("send") == 7, "the request whose connect failed was tried again"
    assert data["summary"]["status_classes"]["no_response"] == 8
    assert data["connections"]["connect_failures"] == 1


# ---------------------------------------------------------------------------
# --timeout
# ---------------------------------------------------------------------------


def test_timeout_reaches_every_request_and_the_artifact(tmp_path, persistent, capsys):
    assert _fire(tmp_path, "--timeout", "2.5") == EXIT_OK, capsys.readouterr().err
    assert {request.timeout for _c, request in persistent.requests} == {2.5}
    assert {timeout for _c, _u, timeout in persistent.connects} == {2.5}
    assert _artifact(tmp_path)["config"]["timeout"] == 2.5


def test_timeout_defaults_to_ten_seconds(tmp_path, persistent, capsys):
    assert _fire(tmp_path) == EXIT_OK, capsys.readouterr().err
    assert {request.timeout for _c, request in persistent.requests} == {10.0}


def test_a_non_positive_timeout_is_refused(tmp_path, persistent, capsys):
    assert _fire(tmp_path, "--timeout", "0") == EXIT_REFUSED
    assert persistent.requests == []


# ---------------------------------------------------------------------------
# The deadline holds against a target that never answers
# ---------------------------------------------------------------------------


def test_a_dead_target_cannot_hold_the_run_past_its_deadline(tmp_path, monkeypatch, capsys):
    """One worker, a target that accepts and never answers, a 5s timeout. The
    run still ends at duration + grace, because no send runs on the main
    thread, and the stuck request is recorded as abandoned."""
    release = threading.Event()

    class Stuck:
        def __init__(self):
            self.connect_seconds = []
            self.reconnects = 0

        def connect(self, url, timeout=10.0):
            self.connect_seconds.append(0.0)
            return 0.0

        def send(self, request):
            release.wait(request.timeout)
            raise TimeoutError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(barrage_fire, "PersistentHttpClient", Stuck)
    started = time.monotonic()
    try:
        rc = _fire(tmp_path, "--concurrency", "1", "--timeout", "5",
                   "--duration", "0.5", "--grace", "0.2")
        wall = time.monotonic() - started
    finally:
        release.set()
    assert rc == EXIT_OK, capsys.readouterr().err
    assert wall < 0.5 + 0.2 + 0.6, f"the run took {wall:.2f}s"

    requests = _artifact(tmp_path)["summary"]["requests"]
    assert requests["scheduled"] == 10
    assert requests["abandoned"] == 1
    assert requests["completed"] == 0
    assert requests["not_dispatched"] + requests["dropped_backlog"] == 9


# ---------------------------------------------------------------------------
# Ctrl-C
# ---------------------------------------------------------------------------


class _InterruptingExecutor(SerialExecutor):
    """Runs inline, and raises KeyboardInterrupt on submission `after`."""

    def __init__(self, after):
        self.after = after
        self.submitted = 0

    def submit(self, fn):
        if self.submitted == self.after:
            raise KeyboardInterrupt
        self.submitted += 1
        return super().submit(fn)


def test_ctrl_c_writes_a_partial_artifact_and_exits_3(tmp_path, persistent, monkeypatch, capsys):
    monkeypatch.setattr(barrage_fire, "PoolExecutor", lambda n: _InterruptingExecutor(after=3))
    rc = _fire(tmp_path)
    out = capsys.readouterr().out

    assert rc == EXIT_FINDING
    data = _artifact(tmp_path)
    assert data["aborted"] is True
    assert [r["outcome"] for r in data["records"]] == ["completed"] * 3 + ["not_dispatched"]
    assert len(persistent.requests) == 3
    assert "ABORTED" in out and "aborted: true" in out


def test_ctrl_c_without_out_still_exits_3(tmp_path, persistent, monkeypatch, capsys):
    monkeypatch.setattr(barrage_fire, "PoolExecutor", lambda n: _InterruptingExecutor(after=1))
    argv = ["barrage", "fire", "--target", "local", "--send", "--config", _config(tmp_path),
            "--rate", "20", "--duration", "0.4", "--warmup", "0"]
    assert cli.main(argv) == EXIT_FINDING
    assert "pass --out to keep a partial run" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_rebuilds_the_same_schedule_and_send_settings(tmp_path, urllib_client, capsys):
    rc = _fire(tmp_path, "--warmup", "0.2", "--duration", "0.5", "--grace", "2.5",
               "--max-queue-seconds", "1.5", "--timeout", "3", "--no-keepalive",
               "--concurrency", "3")
    assert rc == EXIT_OK, capsys.readouterr().err
    original = _artifact(tmp_path)

    rc = cli.main(["barrage", "replay", str(tmp_path / "run.json"), "--send",
                   "--config", _config(tmp_path), "--out", str(tmp_path / "again.json")])
    assert rc == EXIT_OK, capsys.readouterr().err
    replayed = _artifact(tmp_path, "again.json")

    assert json.dumps(replayed["config"]) == json.dumps(original["config"])
    assert [r["scheduled_at"] for r in replayed["records"]] == \
        [r["scheduled_at"] for r in original["records"]]
    plan = barrage_fire.plan_from_artifact(original)[0]
    assert [r["scheduled_at"] for r in original["records"]] == \
        [round(a.offset, 6) for a in arrival_schedule(plan.stages)]
    assert {request.timeout for _c, request in urllib_client.requests} == {3.0}


def test_replay_refuses_an_artifact_from_a_newer_schema(tmp_path, capsys):
    run = tmp_path / "run.json"
    run.write_text(json.dumps({
        "schema_version": 99, "seed": 1,
        "config": {"mode": "open", "rate": 10.0, "duration": 2.0, "warmup": 0.0,
                   "concurrency": 1, "pool_size": 5},
    }), encoding="utf-8")
    assert cli.main(["barrage", "replay", str(run)]) == EXIT_REFUSED
    assert "schema_version 99" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Dry-run preview and artifact config
# ---------------------------------------------------------------------------


def test_dry_run_preview_states_the_deadline_and_connection_model(capsys):
    rc = cli.main(["barrage", "fire", "--target", "local", "--duration", "10",
                   "--grace", "0.5", "--timeout", "4", "--no-keepalive"])
    out = capsys.readouterr().out
    assert rc == EXIT_DRY_RUN
    assert "run deadline: 10.5s" in out
    assert "per-request timeout: 4s" in out
    assert "a new one per request" in out


def test_a_private_target_records_allow_public_target_false(tmp_path):
    class Client:
        def send(self, request):
            return ClientResponse(status=200, body=b"{}")

    out = tmp_path / "run.json"
    clock = [0.0]
    barrage_fire.execute(
        barrage_fire.build_plan("open", 5.0, 1.0, 1, 0.0), 1, 3, "local",
        _config(tmp_path), str(out), client=Client(),
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        printer=lambda _line: None, allow_public_target=True,
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["config"]["allow_public_target"] is False
    assert data["schema_version"] == 1 and data["tool"] == "barrage"
