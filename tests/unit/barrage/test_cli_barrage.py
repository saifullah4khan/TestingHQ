"""Tests for the barrage CLI surface: fire and replay.

Named test_cli_barrage.py to avoid colliding with tests/test_cli.py and
tests/unit/test_cli_blast.py; tests/test_repo_invariants.py enforces unique
test module basenames repo-wide.

The zero-network property of a dry run is PROVEN here, not asserted: the
socket module itself is patched to blow up, so any code path that tried to
reach the wire would fail the test rather than quietly succeed.
"""
from __future__ import annotations

import json
import socket

import pytest

from testinghq import cli
from testinghq.barrage import fire as barrage_fire
from testinghq.barrage.payloads import build_payload_pool
from testinghq.barrage.runner import DEFAULT_MAX_DURATION_SEC, DEFAULT_MAX_RATE_PER_SEC
from testinghq.core.exit_codes import EXIT_DRY_RUN, EXIT_OK, EXIT_REFUSED
from testinghq.core.transport import ClientResponse


@pytest.fixture
def forbid_network(monkeypatch):
    """Fails the test if anything opens a real socket. Same shape as
    tests/unit/test_cli_blast.py's fixture."""

    def _blocked(*args, **kwargs):
        raise AssertionError("a real socket was opened during a network-free test")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


class FakeClient:
    """Records every request; never opens a socket."""

    def __init__(self, status=200, body=b'{"ok":true}'):
        self.status = status
        self.body = body
        self.received = []

    def send(self, request):
        self.received.append(request)
        return ClientResponse(status=self.status, body=self.body)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class FakeSleeper:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def _write_target_config(tmp_path, name="local", url="http://127.0.0.1:8000/inbound"):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{name}]\nurl = "{url}"\n', encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parser surface
# ---------------------------------------------------------------------------


def test_barrage_fire_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["barrage", "fire", "--target", "local"])
    assert args.tool == "barrage"
    assert args.command == "fire"
    assert args.rate == barrage_fire.DEFAULT_RATE
    assert args.duration == barrage_fire.DEFAULT_DURATION
    # None means "not given"; build_plan resolves it to DEFAULT_CONCURRENCY.
    assert args.concurrency is None
    assert args.mode == barrage_fire.DEFAULT_MODE
    assert args.send is False  # dry-run is the DEFAULT
    assert args.allow_high_rate is False  # the ceiling is on by DEFAULT
    assert args.config == cli.DEFAULT_TARGET_CONFIG


def test_barrage_fire_parser_accepts_the_documented_invocation():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "barrage", "fire", "--target", "local", "--rate", "20",
            "--duration", "60", "--concurrency", "8", "--send",
        ]
    )
    assert args.target == "local"
    assert args.rate == 20.0
    assert args.duration == 60.0
    assert args.concurrency == 8
    assert args.send is True


def test_barrage_replay_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["barrage", "replay", "run.json"])
    assert args.run == "run.json"
    assert args.send is False
    assert args.out is None


def test_barrage_fire_parser_rejects_an_unknown_mode():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["barrage", "fire", "--target", "local", "--mode", "sideways"])


def test_blast_subcommands_still_work():
    # This lane added barrage; it must not disturb the blast surface.
    parser = cli.build_parser()
    args = parser.parse_args(["blast", "fire", "--target", "local"])
    assert args.tool == "blast"
    assert args.rate == cli.DEFAULT_RATE


# ---------------------------------------------------------------------------
# Dry run makes ZERO network calls. Proven by socket patch, not asserted.
# ---------------------------------------------------------------------------


def test_barrage_fire_dry_run_makes_zero_network_calls(forbid_network, capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--rate", "10", "--duration", "10"]
    )
    assert rc == EXIT_DRY_RUN
    out = capsys.readouterr().out
    assert "dry-run default" in out
    assert "dry-run preview" in out
    assert "no network calls were made" in out


def test_barrage_fire_dry_run_needs_no_target_config_at_all(tmp_path, monkeypatch, forbid_network):
    # A dry run never resolves a target, so it cannot touch the wire even
    # by accident: there is no URL for it to reach.
    monkeypatch.chdir(tmp_path)  # no target.toml here
    rc = cli.main(["barrage", "fire", "--target", "local", "--duration", "10"])
    assert rc == EXIT_DRY_RUN


def test_barrage_fire_dry_run_previews_the_plan(forbid_network, capsys):
    cli.main(
        [
            "barrage", "fire", "--target", "local", "--rate", "10",
            "--duration", "20", "--mode", "closed",
        ]
    )
    out = capsys.readouterr().out
    assert "closed-loop" in out
    assert "10/s" in out
    # No explicit --concurrency, so the default resolves and is reported.
    assert f"concurrency: {barrage_fire.DEFAULT_CONCURRENCY}" in out


def test_barrage_replay_dry_run_makes_zero_network_calls(tmp_path, forbid_network, capsys):
    artifact = {
        "seed": 5,
        "config": {
            "mode": "open", "rate": 10.0, "duration": 20.0, "warmup": 5.0,
            "concurrency": 2, "pool_size": 10, "target": "local", "dry_run": False,
        },
    }
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(artifact), encoding="utf-8")

    rc = cli.main(["barrage", "replay", str(run_path)])
    assert rc == EXIT_DRY_RUN
    assert "no network calls were made" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The ceiling, through the CLI
# ---------------------------------------------------------------------------


def test_barrage_fire_refuses_over_ceiling_rate_without_the_flag(forbid_network, capsys):
    rc = cli.main(
        [
            "barrage", "fire", "--target", "local",
            "--rate", str(DEFAULT_MAX_RATE_PER_SEC + 1), "--duration", "10",
        ]
    )
    assert rc == EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_refuses_over_ceiling_duration_without_the_flag(forbid_network, capsys):
    rc = cli.main(
        [
            "barrage", "fire", "--target", "local", "--rate", "10",
            "--duration", str(DEFAULT_MAX_DURATION_SEC + 1),
        ]
    )
    assert rc == EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_allows_over_ceiling_rate_with_the_explicit_flag(forbid_network):
    rc = cli.main(
        [
            "barrage", "fire", "--target", "local",
            "--rate", str(DEFAULT_MAX_RATE_PER_SEC + 1), "--duration", "10",
            "--allow-high-rate",
        ]
    )
    assert rc == EXIT_DRY_RUN  # still a dry run, but not refused


def test_barrage_fire_refuses_duration_shorter_than_warmup(forbid_network, capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--duration", "3", "--warmup", "5"]
    )
    assert rc == EXIT_REFUSED
    assert "warmup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Guardrail refusals on the send path
# ---------------------------------------------------------------------------


def test_barrage_fire_send_without_configured_target_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no target.toml here
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--send", "--duration", "10"]
    )
    assert rc == EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_send_without_target_flag_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["barrage", "fire", "--send", "--duration", "10"])
    assert rc == EXIT_REFUSED


def test_execute_refuses_public_host_even_when_configured(tmp_path, forbid_network):
    # A single-label target name has no dot, so the host check has to run on
    # the resolved URL, not on the name.
    _write_target_config(tmp_path, name="prod", url="https://ingest.mycompany.com/inbound")
    plan = barrage_fire.build_plan("open", 10.0, 10.0, 1, 0.0)
    client = FakeClient()
    clock = FakeClock()

    with pytest.raises(Exception) as excinfo:
        barrage_fire.execute(
            plan, 1, 5, "prod", str(tmp_path / "target.toml"), None,
            client=client, clock=clock.now, sleep=FakeSleeper(clock),
        )
    assert "public host" in str(excinfo.value)
    assert client.received == []


def test_execute_accepts_a_reserved_test_domain_target(tmp_path, forbid_network):
    _write_target_config(tmp_path, name="staging", url="https://staging.example.test/inbound")
    plan = barrage_fire.build_plan("open", 5.0, 2.0, 1, 0.0)
    client = FakeClient(status=200)
    clock = FakeClock()

    rc = barrage_fire.execute(
        plan, 1, 5, "staging", str(tmp_path / "target.toml"), None,
        client=client, clock=clock.now, sleep=FakeSleeper(clock),
        printer=lambda text: None,
    )
    assert rc == EXIT_OK
    assert client.received


# ---------------------------------------------------------------------------
# The send path, hermetically. main() exposes no flag to inject a client,
# by design: real usage always uses a real socket.
# ---------------------------------------------------------------------------


def test_execute_fires_the_expected_number_of_requests(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    plan = barrage_fire.build_plan("open", 10.0, 2.0, 1, 0.0)
    client = FakeClient(status=200)
    clock = FakeClock()

    rc = barrage_fire.execute(
        plan, 3, 5, "local", str(tmp_path / "target.toml"), None,
        client=client, clock=clock.now, sleep=FakeSleeper(clock),
        printer=lambda text: None,
    )
    assert rc == EXIT_OK
    assert len(client.received) == 20  # 10/s for 2s


def test_execute_writes_a_json_artifact(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    out_path = tmp_path / "run.json"
    plan = barrage_fire.build_plan("open", 10.0, 2.0, 1, 0.0)
    clock = FakeClock()

    barrage_fire.execute(
        plan, 3, 5, "local", str(tmp_path / "target.toml"), str(out_path),
        client=FakeClient(status=200), clock=clock.now, sleep=FakeSleeper(clock),
        printer=lambda text: None,
    )

    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["seed"] == 3
    assert artifact["config"]["target"] == "local"
    assert artifact["config"]["dry_run"] is False
    assert artifact["config"]["rate"] == 10.0
    assert artifact["summary"]["throughput"]["total_requests"] == 20
    assert "knee" in artifact["summary"]


def test_execute_reports_a_shedding_endpoint(tmp_path, forbid_network, capsys):
    _write_target_config(tmp_path)
    plan = barrage_fire.build_plan("open", 10.0, 2.0, 1, 0.0)
    clock = FakeClock()

    printed = []
    barrage_fire.execute(
        plan, 3, 5, "local", str(tmp_path / "target.toml"), None,
        client=FakeClient(status=500), clock=clock.now, sleep=FakeSleeper(clock),
        printer=printed.append,
    )
    text = "\n".join(printed)
    assert "began shedding" in text


def test_execute_is_reproducible_for_the_same_seed(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    bodies = []
    for _ in range(2):
        client = FakeClient(status=200)
        clock = FakeClock()
        barrage_fire.execute(
            barrage_fire.build_plan("open", 10.0, 2.0, 1, 0.0),
            8, 5, "local", str(tmp_path / "target.toml"), None,
            client=client, clock=clock.now, sleep=FakeSleeper(clock),
            printer=lambda text: None,
        )
        bodies.append([r.body for r in client.received])
    assert bodies[0] == bodies[1]


def test_execute_refuses_over_ceiling_before_sending_anything(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    plan = barrage_fire.build_plan("open", DEFAULT_MAX_RATE_PER_SEC + 10, 2.0, 1, 0.0)
    client = FakeClient()
    clock = FakeClock()

    with pytest.raises(Exception):
        barrage_fire.execute(
            plan, 1, 5, "local", str(tmp_path / "target.toml"), None,
            client=client, clock=clock.now, sleep=FakeSleeper(clock),
        )
    assert client.received == []


# ---------------------------------------------------------------------------
# replay reproduces the run
# ---------------------------------------------------------------------------


def test_barrage_replay_missing_file_is_reported_not_crashed(tmp_path, capsys):
    rc = cli.main(["barrage", "replay", str(tmp_path / "nope.json")])
    assert rc == EXIT_REFUSED
    assert "barrage replay" in capsys.readouterr().err


def test_barrage_replay_invalid_json_is_reported_not_crashed(tmp_path, capsys):
    run_path = tmp_path / "run.json"
    run_path.write_text("{not json", encoding="utf-8")
    rc = cli.main(["barrage", "replay", str(run_path)])
    assert rc == EXIT_REFUSED
    assert "not valid JSON" in capsys.readouterr().err


def test_barrage_replay_incomplete_config_is_refused(tmp_path, capsys):
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"seed": 1, "config": {"rate": 10.0}}), encoding="utf-8")
    rc = cli.main(["barrage", "replay", str(run_path)])
    assert rc == EXIT_REFUSED
    assert "cannot reproduce" in capsys.readouterr().err


def test_barrage_replay_reuses_the_saved_plan(tmp_path, forbid_network, capsys):
    _write_target_config(tmp_path)
    out_path = tmp_path / "run.json"
    clock = FakeClock()
    barrage_fire.execute(
        barrage_fire.build_plan("closed", 10.0, 2.0, 3, 0.0),
        12, 7, "local", str(tmp_path / "target.toml"), str(out_path),
        client=FakeClient(status=200), clock=clock.now, sleep=FakeSleeper(clock),
        printer=lambda text: None,
    )

    rc = cli.main(["barrage", "replay", str(out_path)])
    assert rc == EXIT_DRY_RUN
    out = capsys.readouterr().out
    assert "seed=12" in out
    assert "closed-loop" in out
    assert "concurrency: 3" in out


# ---------------------------------------------------------------------------
# Synthetic-content guardrail runs before any network call
# ---------------------------------------------------------------------------


def test_require_synthetic_pool_passes_for_the_generated_pool():
    pool = build_payload_pool(seed=4, pool_size=10)
    barrage_fire.require_synthetic_pool(pool)  # must not raise


def test_require_synthetic_pool_refuses_a_non_reserved_address():
    from testinghq.core.guardrails import GuardrailError

    pool = build_payload_pool(seed=4, pool_size=2)
    tainted = pool[0].__class__(
        **{**pool[0].__dict__, "to": "real.person@mycompany.com"}
    )
    with pytest.raises(GuardrailError):
        barrage_fire.require_synthetic_pool([tainted])


# ---------------------------------------------------------------------------
# --concurrency is the worker count in both modes. The concurrency itself is
# measured in test_runner.py and tests/e2e/test_real_sockets.py, because a
# fake clock cannot see overlap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_concurrency_is_accepted_in_both_modes(mode, capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", mode, "--concurrency", "8"]
    )
    assert rc == EXIT_DRY_RUN, capsys.readouterr().err


def test_the_dry_run_preview_reports_the_requested_concurrency(capsys):
    cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "closed",
         "--concurrency", "8"]
    )
    out = capsys.readouterr().out
    assert "concurrency: 8" in out
    assert "each of the 8 worker(s) sends its next request only after" in out


def test_concurrency_of_one_is_accepted_in_both_modes(capsys):
    for mode in ("open", "closed"):
        rc = cli.main(
            ["barrage", "fire", "--target", "local", "--mode", mode,
             "--concurrency", "1"]
        )
        assert rc == EXIT_DRY_RUN
        assert "concurrency: 1" in capsys.readouterr().out


def test_build_plan_keeps_the_concurrency_it_was_given_in_both_modes():
    """`barrage replay` rebuilds the plan from this value."""
    assert barrage_fire.build_plan("closed", 5.0, 20.0, 8, 5.0).concurrency == 8
    assert barrage_fire.build_plan("open", 5.0, 20.0, 64, 5.0).concurrency == 64


def test_omitting_concurrency_resolves_to_the_default_in_both_modes():
    closed = barrage_fire.build_plan("closed", 5.0, 20.0, None, 5.0)
    opened = barrage_fire.build_plan("open", 5.0, 20.0, None, 5.0)
    assert closed.concurrency == opened.concurrency == barrage_fire.DEFAULT_CONCURRENCY


def test_replay_of_an_artifact_without_the_newer_keys_uses_the_defaults(tmp_path, capsys):
    """An artifact written before grace, timeout and keep-alive were recorded
    still replays, with today's defaults for what it lacks."""
    artifact = {
        "seed": 5,
        "config": {
            "mode": "open", "rate": 10.0, "duration": 20.0, "warmup": 5.0,
            "concurrency": 2, "pool_size": 10, "target": "local", "dry_run": False,
        },
    }
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(artifact), encoding="utf-8")

    assert cli.main(["barrage", "replay", str(run_path)]) == EXIT_DRY_RUN
    out = capsys.readouterr().out
    assert f"(duration + {barrage_fire.DEFAULT_GRACE_SECONDS:g}s grace)" in out
    assert f"per-request timeout: {barrage_fire.DEFAULT_TIMEOUT:g}s" in out
    assert "kept alive, one per worker" in out


# ---------------------------------------------------------------------------
# The dry-run preview describes the pool that will run.
# ---------------------------------------------------------------------------


def test_dry_run_preview_states_how_many_requests_can_be_in_flight(capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "open",
         "--concurrency", "16"]
    )
    out = capsys.readouterr().out

    assert rc == EXIT_DRY_RUN
    assert "pool of 16 workers" in out
    assert "16 request(s) in flight" in out


def test_dry_run_preview_warns_that_one_worker_caps_the_rate(capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "open",
         "--concurrency", "1"]
    )
    out = capsys.readouterr().out

    assert rc == EXIT_DRY_RUN
    assert "one worker" in out
    assert "slower than the arrival interval" in out
    assert "raise --concurrency" in out


def test_dry_run_preview_states_the_pool_in_closed_mode_too(capsys):
    rc = cli.main(["barrage", "fire", "--target", "local", "--mode", "closed"])
    out = capsys.readouterr().out

    assert rc == EXIT_DRY_RUN
    assert f"concurrency: {barrage_fire.DEFAULT_CONCURRENCY}" in out
    assert f"pool of {barrage_fire.DEFAULT_CONCURRENCY} workers" in out


def test_dry_run_preview_reports_the_memory_cost_of_the_pool(capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "closed",
         "--concurrency", "64"]
    )
    out = capsys.readouterr().out

    assert rc == EXIT_DRY_RUN
    assert "costs memory" in out
