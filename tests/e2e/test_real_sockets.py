"""End to end, over real sockets, with the real transport.

Most of this repository injects a fake HTTP client. That is correct for most
things and blind to everything that only appears when a socket is real, which is
what this file exists for. A wrong Content-Length, a chunked-encoding
assumption, a connection that is accepted and then reset, a client that works
in-process and not as a subprocess: none of that is reachable from a fake, and
all of it is what a user meets first.

So this is not the only place a real socket appears.
`tests/integration/test_pipeline_verification.py` binds loopback itself, and
both run in the e2e job. What is unique here is driving
the CLI as a subprocess rather than importing the engine, which is
the one thing an in-process fake cannot substitute for.

The subprocess is `python -m testinghq.cli` run from the repository root, so it
is always the code under test. A `testinghq` console script on PATH can belong
to an older install (a dev venv pointing at another checkout), and running it
here once hid a real failure; one test below only checks that such a script,
when present, starts.

This file is the one place it is reached. The sink is a real `http.server` on a
loopback port. The tool runs as a real subprocess. The transport is
`UrllibHttpClient`. Then the sink's receipts are cross-checked against the run
artifact, which is the assertion that matters: not "it did not crash" but "the
bytes that arrived are the payloads the artifact says were sent".

WHY THIS IS A SEPARATE WORKFLOW. The suite-wide network block in
`tests/conftest.py` is on by default, and it blocks `socket.socket.connect`. A
real end-to-end test has to connect. So this file is marked `allow_network` and
`.github/workflows/e2e.yml` runs it, while `ci.yml` excludes this directory
with `--ignore=tests/e2e` so the hermetic job stays hermetic. The exemption is a
whole module rather than a per-test marker, so a new test here inherits it
deliberately rather than by copy-paste.

`testpaths` is `tests`, so a bare `pytest -q` collects this directory. That is
intended for a local run, where running the socket tests is the point, and it is
why the CI exclusion has to be an explicit `--ignore` rather than something that
also changes local behaviour.

THE EXEMPTION IS NOT TRUSTED. `allow_network` lifts the block, which means a
mistake in this file could reach anything on the internet. So this file
asserts, from inside itself, that every connection it made went to loopback:
the sink records its peer address per request and the test checks the whole
set. That converts an exemption into a bounded, self-policing one.
"""
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from testinghq.core.exit_codes import EXIT_FINDING

REPO_ROOT = Path(__file__).resolve().parents[2]


#: The CLI under test: this checkout's package, run as a real subprocess
#: from the repository root so the checkout shadows any installed copy.
CLI = [sys.executable, "-m", "testinghq.cli"]


#: Every test in this file needs a real socket, deliberately and visibly.
pytestmark = pytest.mark.allow_network

LOOPBACK_PREFIXES = ("127.", "::1", "::ffff:127.")


class _Sink:
    """A real HTTP server on loopback, recording what actually arrived.

    Records the exact request body, not a hash and not a count, so the test can
    assert the payload is byte-identical to what the artifact claims was sent.
    """

    def __init__(self, status: int = 200, delay_seconds: float = 0.0):
        self.status = status
        # A target that takes measurable time to answer. Zero for the
        # byte-fidelity tests, which do not want to wait; non-zero for the
        # concurrency tests, which cannot see anything without it.
        self.delay_seconds = delay_seconds
        self.bodies: list[bytes] = []
        self.paths: list[str] = []
        self.peers: list[str] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def __enter__(self):
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                peer = self.client_address[0]
                if sink.delay_seconds:
                    time.sleep(sink.delay_seconds)
                with sink._lock:
                    sink.bodies.append(body)
                    sink.paths.append(self.path)
                    sink.peers.append(peer)
                payload = b'{"ok":true}'
                self.send_response(sink.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/intake"

    def write_config(self, path: Path, name: str = "e2e") -> str:
        path.write_text(
            f'[targets.{name}]\nname = "{name}"\nurl = "{self.url}"\n',
            encoding="utf-8",
        )
        return str(path)

    def only_talked_to_loopback(self) -> bool:
        return all(
            any(p.startswith(prefix) for prefix in LOOPBACK_PREFIXES)
            for p in self.peers
        )


def _run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run the CLI under test as a subprocess, as a user would."""
    return subprocess.run(
        [*CLI, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Blast
# ---------------------------------------------------------------------------


def test_blast_send_delivers_exactly_what_the_artifact_says(tmp_path):
    """The load-bearing assertion of this file.

    Blast's determinism guarantee is that a seed plus a config reproduces the
    same payload bytes. That is only worth anything if the bytes that reach the
    far end are the bytes the artifact says were sent, so the sink's receipts
    are compared against a locally rebuilt corpus from the same seed.

    Compared as bodies, not counts. A count of 40 receipts would pass even if
    every one were the wrong payload, and a wrong payload is the failure this
    test exists to catch.
    """
    from testinghq.blast.corpus import CorpusPlan
    from testinghq.core.formats import get_format
    from testinghq.core.transport import DEFAULT_BOUNDARY, build_request

    seed, count = 4242, 12

    with _Sink() as sink:
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "run.json"
        result = _run(
            "blast", "fire", "--target", "e2e", "--send",
            "--seed", str(seed), "--count", str(count),
            "--out", str(artifact), "--config", config,
        )
        assert result.returncode == 0, (
            f"blast fire --send exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        assert sink.bodies, "the sink received nothing at all"
        assert sink.only_talked_to_loopback(), (
            f"connections went somewhere other than loopback: {set(sink.peers)}"
        )

        data = json.loads(artifact.read_text(encoding="utf-8"))
        assert len(data["records"]) == count

        # Rebuilt from what the artifact records (the corpus plan and the
        # target's wire format) through the transport's own request builder,
        # because that is what builds the bytes the tool puts on the wire.
        # Comparing against a hand-rolled reconstruction would test the
        # reconstruction.
        plan = CorpusPlan.from_config(data["seed"], data["config"])
        wire_format = get_format(data["config"]["format"], DEFAULT_BOUNDARY)
        encoded = [
            build_request(email, "http://127.0.0.1/", wire_format=wire_format).body
            for email, _label in plan.build()
        ]

        assert len(sink.bodies) == count, (
            f"the artifact records {count} payloads but the sink received "
            f"{len(sink.bodies)}"
        )
        for index, (arrived, want) in enumerate(zip(sink.bodies, encoded)):
            assert arrived == want, (
                f"payload {index} differs from what the seed and config "
                f"rebuild. The sink received {len(arrived)} bytes and the "
                f"rebuild is {len(want)}."
            )


def test_every_recorded_request_was_answered_and_reported(tmp_path):
    """Each sent payload has a 2xx in the artifact, and the counts add up.
    A payload that was sent but never answered, or answered but never
    recorded, is exactly the kind of quiet loss an end-to-end test can see and
    a fake client cannot."""
    with _Sink() as sink:
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "run.json"
        result = _run(
            "blast", "fire", "--target", "e2e", "--send",
            "--seed", "7", "--count", "8", "--out", str(artifact),
            "--config", config,
        )
        assert result.returncode == 0, result.stderr

        data = json.loads(artifact.read_text(encoding="utf-8"))
        statuses = [r["response"]["status"] for r in data["records"]]

        assert statuses == [200] * 8, f"unexpected statuses: {statuses}"
        assert len(sink.bodies) == 8
        assert data["summary"]["by_status_class"]["2xx"] == 8
        assert sum(data["summary"]["by_category"].values()) == 8


def test_a_failing_endpoint_is_reported_as_a_status_not_a_crash(tmp_path):
    """The other direction: a real 500 over a real socket has to land in the
    artifact as a 5xx and flag the clean payloads it broke."""
    with _Sink(status=500) as sink:
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "run.json"
        result = _run(
            "blast", "fire", "--target", "e2e", "--send",
            "--seed", "7", "--count", "10", "--out", str(artifact),
            "--config", config,
        )
        assert result.returncode == EXIT_FINDING, (
            "a run that raised flags exits with the finding code\n" + result.stderr
        )

        data = json.loads(artifact.read_text(encoding="utf-8"))
        assert data["summary"]["by_status_class"]["5xx"] == 10
        assert len(data["summary"]["flags"]) == 10, "a 500 is a flag in every category"
        assert len(sink.bodies) == 10
        assert sink.only_talked_to_loopback()


# ---------------------------------------------------------------------------
# Barrage
# ---------------------------------------------------------------------------


def test_barrage_send_reaches_the_sink_over_real_sockets(tmp_path):
    """Barrage's send path goes through the same transport and had its own
    untested seam, the `client` parameter on `_cmd_replay` that was missing and
    cost 81 seconds of real connection attempts before it was found. That was
    discovered by reading a test, not by running one."""
    with _Sink() as sink:
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "load.json"
        result = _run(
            "barrage", "fire", "--target", "e2e", "--send",
            "--rate", "20", "--duration", "2", "--warmup", "0",
            "--seed", "3", "--out", str(artifact), "--config", config,
        )
        assert result.returncode == 0, (
            f"barrage fire --send exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        assert sink.bodies, "the sink received nothing at all"
        assert sink.only_talked_to_loopback(), (
            f"connections went somewhere other than loopback: {set(sink.peers)}"
        )

        data = json.loads(artifact.read_text(encoding="utf-8"))
        total = data["summary"]["throughput"]["total_requests"]
        assert total == len(sink.bodies), (
            f"the run artifact reports {total} requests and the sink received "
            f"{len(sink.bodies)}"
        )
        assert data["summary"]["error_rate"] == 0.0
        # A 2-second run at 20/s is about 40 requests. Assert a floor rather
        # than an exact number, because the last dispatch of a stage can land
        # either side of the window boundary.
        assert total >= 20, f"only {total} requests in a 2s run at 20/s"


def test_barrage_replay_sends_the_same_payloads_again(tmp_path):
    """Replay is a reproducibility claim, and a real socket is where byte
    equality can actually be checked rather than inferred from hashes."""
    with _Sink() as sink:
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "load.json"
        first = _run(
            "barrage", "fire", "--target", "e2e", "--send",
            "--rate", "20", "--duration", "1", "--warmup", "0",
            "--seed", "3", "--out", str(artifact), "--config", config,
        )
        assert first.returncode == 0, first.stderr
        original = list(sink.bodies)
        assert original, "the first run sent nothing"

        sink.bodies.clear()
        second = _run("barrage", "replay", str(artifact), "--send", "--config", config)
        assert second.returncode == 0, second.stderr
        replayed = list(sink.bodies)

        assert replayed, "the replay sent nothing"
        assert len(replayed) == len(original)
        for index, (before, after) in enumerate(zip(original, replayed)):
            assert before == after, f"replayed payload {index} differs"


# ---------------------------------------------------------------------------
# The exemption is self-policing
# ---------------------------------------------------------------------------


def test_this_file_is_the_only_end_to_end_module():
    """The e2e surface is one file on purpose. Two files would mean two places
    to look for "what does this actually send over the wire", and the second
    one would not get the loopback assertion."""
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    modules = sorted(p.name for p in e2e_dir.glob("test_*.py"))
    assert modules == ["test_real_sockets.py"], (
        f"tests/e2e contains {modules}; keep the end-to-end surface in one file"
    )


def test_the_subprocess_runs_the_code_under_test():
    """The CLI these tests spawn is this checkout, not an installed copy: the
    package it imports lives here and reports this checkout's version."""
    import testinghq

    assert Path(testinghq.__file__).resolve().parent.parent == REPO_ROOT
    result = _run("--version", timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"testinghq {testinghq.__version__}"
    located = subprocess.run(
        [sys.executable, "-c", "import testinghq; print(testinghq.__file__)"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert Path(located.stdout.strip()).resolve() == Path(testinghq.__file__).resolve()


def test_an_installed_console_script_starts():
    """The `testinghq` entry point, when one is installed, resolves and runs.
    It may be an older install than this checkout, so only that is checked."""
    script = shutil.which("testinghq")
    if script is None:
        pytest.skip("no testinghq console script on PATH")
    result = subprocess.run(
        [script, "--version"], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("testinghq "), result.stdout


def test_the_sink_actually_binds_loopback_and_nothing_else():
    """Cheap, and it means a sink that somehow bound a routable interface would
    be noticed here rather than by something else."""
    with _Sink() as sink:
        assert sink.url.startswith("http://127.0.0.1:"), sink.url


# ---------------------------------------------------------------------------
# Barrage concurrency, keep-alive and the run deadline, against real sockets
#
# These live here rather than in their own module because `allow_network` is
# one file. A fake clock cannot see concurrency, and only a real server can
# say how many TCP connections a run opened.
# ---------------------------------------------------------------------------

_BARRAGE_DURATION = 2.0
_BARRAGE_RATE = 20.0
#: 200ms of service time puts the one-request-in-flight ceiling at 5/s, a
#: quarter of the rate asked for, wide enough that a scheduler hiccup cannot
#: close the gap.
_BARRAGE_SERVICE_SECONDS = 0.2


def _barrage_against(url, concurrency):
    """One real open-loop run over a real socket, with the shipped send hook.
    Returns the records and the reported throughput."""
    from testinghq.barrage import fire as barrage_fire
    from testinghq.barrage import runner as barrage_runner
    from testinghq.barrage.report import throughput
    from testinghq.blast.generate import generate_corpus

    plan = barrage_runner.RunPlan(
        mode="open", rate=_BARRAGE_RATE, concurrency=concurrency,
        warmup_seconds=0.0, hold_seconds=_BARRAGE_DURATION, ramp_step_count=0,
    )
    sender = barrage_fire.make_send_fn(generate_corpus(seed=7, count=4), url)
    try:
        result = barrage_runner.run(plan, sender, allow_high_rate=True)
    finally:
        sender.close()
    samples = barrage_fire.samples_from_records(result, plan.warmup_seconds)
    return result.records, throughput(samples)


def test_barrage_concurrency_changes_the_throughput_a_real_target_sees():
    """A target taking 200ms per request, offered 20 requests/second. One
    worker cannot exceed 1 / 0.2 = 5/s; eight can hold the schedule. Asserted
    as a ratio, because the absolutes depend on a loaded runner."""
    with _Sink(delay_seconds=_BARRAGE_SERVICE_SECONDS) as sink:
        serial_records, serial = _barrage_against(sink.url, 1)
        parallel_records, parallel = _barrage_against(sink.url, 8)

    assert len(serial_records) == len(parallel_records), (
        "both runs were asked for the same rate and duration and should have "
        "scheduled the same requests"
    )
    assert serial["achieved_rps"] < _BARRAGE_RATE * 0.5, serial
    assert parallel["achieved_rps"] > serial["achieved_rps"] * 2, (serial, parallel)
    assert parallel["achieved_rps"] >= _BARRAGE_RATE * 0.85, parallel
    assert all(getattr(r.result, "status", None) == 200 for r in parallel_records)


def test_barrage_accounts_for_every_request_it_dispatched():
    """Every scheduled request comes back, in index order, and with eight
    workers against a 200ms target every one of them completed."""
    with _Sink(delay_seconds=_BARRAGE_SERVICE_SECONDS) as sink:
        records, _ = _barrage_against(sink.url, 8)

    assert records, "the run dispatched nothing"
    assert [r.index for r in records] == list(range(len(records)))
    unfilled = [r for r in records if r.outcome != "completed" or r.result is None]
    assert not unfilled, f"{len(unfilled)} of {len(records)} records have no result"


def _count_connections(sink):
    """Count the TCP connections the sink accepts. `verify_request` runs once
    per accepted connection, before any request on it is read."""
    accepted = []
    original = sink._server.verify_request

    def verify(request, client_address):
        accepted.append(client_address)
        return original(request, client_address)

    sink._server.verify_request = verify
    return accepted


@pytest.mark.parametrize("keepalive", [True, False])
def test_barrage_keeps_one_connection_per_worker(tmp_path, keepalive):
    with _Sink() as sink:
        accepted = _count_connections(sink)
        config = sink.write_config(tmp_path / "target.toml")
        artifact = tmp_path / "load.json"
        result = _run(
            "barrage", "fire", "--target", "e2e", "--send",
            "--rate", "20", "--duration", "1", "--warmup", "0", "--concurrency", "2",
            "--out", str(artifact), "--config", config,
            *([] if keepalive else ["--no-keepalive"]),
        )
        assert result.returncode == 0, result.stderr
        assert sink.only_talked_to_loopback()

    data = json.loads(artifact.read_text(encoding="utf-8"))
    sent = data["summary"]["requests"]["sent"]
    assert sent == len(sink.bodies) == 20
    assert data["config"]["keepalive"] is keepalive
    if keepalive:
        assert len(accepted) <= 2, f"{len(accepted)} connections for 2 workers"
        assert data["connections"]["workers_connected"] == len(accepted)
        assert data["connections"]["setup_ms"]["max"] is not None
    else:
        assert len(accepted) == sent


def test_barrage_ends_at_its_deadline_against_a_target_that_never_answers(tmp_path):
    """A listener that accepts connections and never reads them. With a 20s
    timeout the run must still end at duration + grace, recording the stuck
    requests as abandoned instead of waiting them out."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    config = tmp_path / "target.toml"
    config.write_text(
        f'[targets.e2e]\nurl = "http://127.0.0.1:{port}/intake"\n', encoding="utf-8"
    )
    artifact = tmp_path / "load.json"
    try:
        started = time.monotonic()
        result = _run(
            "barrage", "fire", "--target", "e2e", "--send",
            "--rate", "10", "--duration", "1", "--warmup", "0", "--concurrency", "2",
            "--grace", "0.5", "--timeout", "20",
            "--out", str(artifact), "--config", str(config), timeout=60,
        )
        wall = time.monotonic() - started
    finally:
        listener.close()

    assert result.returncode == 0, result.stderr
    # 1.5s of run plus interpreter start-up, far below the 20s timeout.
    assert wall < 1.5 + 6.0, f"barrage took {wall:.1f}s against a 1.5s deadline"
    requests = json.loads(artifact.read_text(encoding="utf-8"))["summary"]["requests"]
    assert requests["scheduled"] == 10
    assert requests["abandoned"] == 2
    assert requests["completed"] == 0
    assert requests["not_dispatched"] + requests["dropped_backlog"] == 8
