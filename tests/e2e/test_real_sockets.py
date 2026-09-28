"""End to end, over real sockets, with the real transport.

Most of this repository injects a fake HTTP client. That is correct for most
things and blind to everything that only appears when a socket is real, which is
what this file exists for. A wrong Content-Length, a chunked-encoding
assumption, a connection that is accepted and then reset, a client that works
in-process and not as a subprocess: none of that is reachable from a fake, and
all of it is what a user meets first.

So this is not the only place a real socket appears. `tests/web/test_server.py`
and `tests/integration/test_pipeline_verification.py` both bind loopback
themselves, and all three run in the e2e job. What is unique here is driving
the installed binary as a subprocess rather than importing the engine, which is
the one thing an in-process fake cannot substitute for.

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
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_executable() -> str:
    """The installed `testinghq` console script, wherever it landed.

    Resolved rather than hardcoded. These tests spawn the real binary, and a
    path like `.venv/Scripts/testinghq.exe` is Windows-only, so a hardcoded one
    would have passed locally and failed on every Linux runner, which is the
    exact class of bug this job exists to catch.
    """
    found = shutil.which("testinghq")
    if found:
        return found
    for candidate in (
        REPO_ROOT / ".venv" / "Scripts" / "testinghq.exe",
        REPO_ROOT / ".venv" / "bin" / "testinghq",
    ):
        if candidate.exists():
            return str(candidate)
    # Last resort: the module, which is the same entry point.
    return f"{sys.executable} -m testinghq.cli"


EXE = _find_executable()

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
    """Run the real binary, as a user would."""
    argv = EXE.split() if " -m " in EXE else [EXE]
    return subprocess.run(
        [*argv, *args],
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
    from testinghq.blast.corrupt import DEFAULT_MIX, corrupt_corpus
    from testinghq.blast.generate import generate_corpus
    from testinghq.blast.serialize import to_multipart_parts
    from testinghq.core.transport import encode_multipart

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

        expected = corrupt_corpus(generate_corpus(seed, count), seed, DEFAULT_MIX)
        # Rebuilt through the transport's own encoder, because that is what
        # builds the bytes the tool puts on the wire. Comparing against a
        # hand-rolled reconstruction would test the reconstruction.
        encoded = [
            encode_multipart(to_multipart_parts(email))
            for email, _recipe in expected
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
        assert result.returncode == 0, result.stderr

        data = json.loads(artifact.read_text(encoding="utf-8"))
        assert data["summary"]["by_status_class"]["5xx"] == 10
        assert data["summary"]["flags"], "a 500 should have flagged something"
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


def test_this_file_is_the_only_one_allowed_to_exempt_itself():
    """`allow_network` is the one power in the suite that can leave the
    machine. Keeping it to exactly one file, and asserting that, is what makes
    the exemption reviewable rather than a trend."""
    import ast

    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == Path(__file__).relative_to(REPO_ROOT).as_posix():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            marked = False
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "pytestmark":
                        marked = "allow_network" in ast.dump(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if "allow_network" in ast.dump(decorator):
                        marked = True
            if marked:
                offenders.append(relative)

    # The two known cases, and only these. `tests/web/test_server.py` starts a
    # real stdlib server to exercise the HTTP layer; this file is the end-to-end
    # suite. Anything else needing a socket is a new exemption and should be
    # argued for rather than added.
    assert sorted(set(offenders)) == ["tests/web/test_server.py"], (
        f"unexpected modules exempting themselves from the network block: "
        f"{sorted(set(offenders))}. Only the real-server tests may."
    )


def test_this_file_is_the_only_end_to_end_module():
    """The e2e surface is one file on purpose. Two files would mean two places
    to look for "what does this actually send over the wire", and the second
    one would not get the loopback assertion."""
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    modules = sorted(p.name for p in e2e_dir.glob("test_*.py"))
    assert modules == ["test_real_sockets.py"], (
        f"tests/e2e contains {modules}; keep the end-to-end surface in one file"
    )


def test_the_sink_actually_binds_loopback_and_nothing_else():
    """Cheap, and it means a sink that somehow bound a routable interface would
    be noticed here rather than by something else."""
    with _Sink() as sink:
        assert sink.url.startswith("http://127.0.0.1:"), sink.url


# ---------------------------------------------------------------------------
# Barrage concurrency, measured against a real socket
#
# This lives here rather than in its own module because of the exemption rule
# above: `allow_network` is one file, and adding a second is supposed to be
# argued for. This is the argument. A load test cannot be faked, because a fake
# clock cannot see concurrency: a fake `sleep` returns instantly, so a serial
# loop and a parallel one are indistinguishable to virtual time. That is
# precisely how a serial dispatcher carried a green suite for as long as it did
# (issue #38).
# ---------------------------------------------------------------------------

#: A WHOLE number of one-second buckets, and that is not arbitrary.
#: `barrage.report.throughput` divides the request count by
#: `len(buckets) * bucket_seconds`, and buckets are whole units. A 1.5s run spans
#: two buckets, so 30 requests are reported as 30 / 2.0 = 15/s even though every
#: one was dispatched on the 20/s schedule. At 2.0s the arithmetic is
#: 40 / 2.0 = 20/s and the reported number is the scheduled one. Getting this
#: wrong makes a correct run look like it under-delivered, which is the sort of
#: thing that gets a real bug dismissed.
_BARRAGE_DURATION = 2.0
_BARRAGE_RATE = 20.0
#: 200ms of service time puts the one-request-in-flight ceiling at 5/s, a
#: quarter of the rate asked for. The gap has to be wide enough that a
#: scheduler hiccup cannot close it.
_BARRAGE_SERVICE_SECONDS = 0.2


def _barrage_against(url, concurrency):
    """One real barrage run over a real socket. Returns the reported numbers."""
    from testinghq.blast.generate import generate_corpus
    from testinghq.barrage import runner as barrage_runner
    from testinghq.barrage.report import Sample, throughput
    from testinghq.core.transport import post

    plan = barrage_runner.RunPlan(
        mode="open", rate=_BARRAGE_RATE, concurrency=concurrency,
        warmup_seconds=0.0, hold_seconds=_BARRAGE_DURATION, ramp_step_count=0,
    )
    pool = generate_corpus(seed=7, count=4)

    def send_fn(index):
        result = post(pool[index % len(pool)], url)
        return result, result.latency_ms / 1000.0

    records = barrage_runner.run(plan, send_fn, allow_high_rate=True)
    samples = [
        Sample(
            dispatch_time=r.dispatch_time,
            target_rate=r.target_rate,
            status=getattr(r.result, "status", None),
            latency_ms=getattr(r.result, "latency_ms", None) or 0.0,
        )
        for r in records
    ]
    return records, throughput(samples)


def test_barrage_concurrency_changes_the_throughput_a_real_target_sees():
    """The load-bearing claim of issue #38, measured end to end.

    A target taking 200ms per request, offered 20 requests/second. With one
    request in flight the ceiling is 1 / 0.2 = 5/s whatever is asked for, and
    the run reports an achieved rate near 5 while looking like it honoured a
    20/s schedule. That number is a measurement of the dispatcher, not of the
    target, and it is what the tool reported for as long as there was no
    executor.

    Asserted as a ratio rather than as exact figures, because the absolutes
    depend on a real scheduler and a loaded CI runner. The claim worth making
    is that concurrency moved the number by several times over.
    """
    with _Sink(delay_seconds=_BARRAGE_SERVICE_SECONDS) as sink:
        _serial_records, serial = _barrage_against(sink.url, 1)
        parallel_records, parallel = _barrage_against(sink.url, 8)

    assert serial["total_requests"] == parallel["total_requests"], (
        "both runs were asked for the same rate and duration and should have "
        f"dispatched the same count: {serial['total_requests']} vs "
        f"{parallel['total_requests']}"
    )

    assert serial["achieved_rps"] < _BARRAGE_RATE, (
        f"one request in flight achieved {serial['achieved_rps']:.2f}/s against "
        f"a requested {_BARRAGE_RATE}/s, which is what a 0.2s service time "
        "allows. If this ever passes, the target is no longer slow enough for "
        "the comparison to mean anything."
    )

    assert parallel["achieved_rps"] > serial["achieved_rps"] * 2, (
        f"concurrency 8 achieved {parallel['achieved_rps']:.2f}/s against "
        f"{serial['achieved_rps']:.2f}/s at 1. --concurrency still does nothing."
    )

    assert parallel["achieved_rps"] >= _BARRAGE_RATE * 0.85, (
        f"concurrency 8 achieved {parallel['achieved_rps']:.2f}/s, short of the "
        f"requested {_BARRAGE_RATE}/s. With 8 workers against 0.2s of service "
        "time the ceiling is 40/s, so the run should have held its schedule. "
        "The tolerance is for a loaded runner, not for a missing pool."
    )

    assert all(getattr(r.result, "status", None) == 200 for r in parallel_records), (
        "every request under concurrency should have been answered 200"
    )


def test_barrage_accounts_for_every_request_it_dispatched():
    """A pool can drop work in a way a serial loop cannot, and the symptom is a
    record whose result was never filled in.

    Checked on the records rather than through the artifact, because a None
    result reaching the report is the failure, and reading the records is where
    it is visible.
    """
    with _Sink(delay_seconds=_BARRAGE_SERVICE_SECONDS) as sink:
        records, _ = _barrage_against(sink.url, 8)

    assert records, "the run dispatched nothing"
    unfilled = [r for r in records if r.result is None]
    assert not unfilled, (
        f"{len(unfilled)} of {len(records)} records have no result. A request "
        "was dispatched and never accounted for, which is the failure mode a "
        "concurrent dispatcher adds."
    )
    assert [r.index for r in records] == list(range(len(records))), (
        "records must come back in index order. A run artifact is aligned by "
        "index, so an artifact that reorders itself between executions of the "
        "same plan cannot be compared."
    )