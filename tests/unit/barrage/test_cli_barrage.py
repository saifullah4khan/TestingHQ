"""Tests for the barrage CLI surface: fire and replay.

Named test_cli_barrage.py to avoid colliding with tests/test_cli.py and
tests/unit/test_cli_blast.py; tests/test_lane_hygiene.py enforces unique
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
from testinghq.barrage.runner import DEFAULT_MAX_DURATION_SEC, DEFAULT_MAX_RATE_PER_SEC
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
    # None, not DEFAULT_CONCURRENCY: the flag means "not specified", which
    # build_plan resolves per mode. Defaulting it here would make an explicit
    # --concurrency indistinguishable from an omitted one, and open mode has
    # to refuse the explicit case.
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
    assert rc == barrage_fire.EXIT_DRY_RUN
    out = capsys.readouterr().out
    assert "dry-run default" in out
    assert "dry-run preview" in out
    assert "no network calls were made" in out


def test_barrage_fire_dry_run_needs_no_target_config_at_all(tmp_path, monkeypatch, forbid_network):
    # A dry run never resolves a target, so it cannot touch the wire even
    # by accident: there is no URL for it to reach.
    monkeypatch.chdir(tmp_path)  # no target.toml here
    rc = cli.main(["barrage", "fire", "--target", "local", "--duration", "10"])
    assert rc == barrage_fire.EXIT_DRY_RUN


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
    assert rc == barrage_fire.EXIT_DRY_RUN
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
    assert rc == barrage_fire.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_refuses_over_ceiling_duration_without_the_flag(forbid_network, capsys):
    rc = cli.main(
        [
            "barrage", "fire", "--target", "local", "--rate", "10",
            "--duration", str(DEFAULT_MAX_DURATION_SEC + 1),
        ]
    )
    assert rc == barrage_fire.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_allows_over_ceiling_rate_with_the_explicit_flag(forbid_network):
    rc = cli.main(
        [
            "barrage", "fire", "--target", "local",
            "--rate", str(DEFAULT_MAX_RATE_PER_SEC + 1), "--duration", "10",
            "--allow-high-rate",
        ]
    )
    assert rc == barrage_fire.EXIT_DRY_RUN  # still a dry run, but not refused


def test_barrage_fire_refuses_duration_shorter_than_warmup(forbid_network, capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--duration", "3", "--warmup", "5"]
    )
    assert rc == barrage_fire.EXIT_REFUSED
    assert "warmup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Guardrail refusals on the send path
# ---------------------------------------------------------------------------


def test_barrage_fire_send_without_configured_target_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no target.toml here
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--send", "--duration", "10"]
    )
    assert rc == barrage_fire.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_barrage_fire_send_without_target_flag_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["barrage", "fire", "--send", "--duration", "10"])
    assert rc == barrage_fire.EXIT_REFUSED


def test_execute_refuses_public_host_even_when_configured(tmp_path, forbid_network):
    # The trap this pins: a bare single-label target name has no dot, so
    # the public-host hardening would pass it unconditionally if only the
    # name were checked. Resolving the URL and checking that too is what
    # makes the hardening bite.
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
    assert rc == barrage_fire.EXIT_OK
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
    assert rc == barrage_fire.EXIT_OK
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
    assert rc == barrage_fire.EXIT_REFUSED
    assert "barrage replay" in capsys.readouterr().err


def test_barrage_replay_invalid_json_is_reported_not_crashed(tmp_path, capsys):
    run_path = tmp_path / "run.json"
    run_path.write_text("{not json", encoding="utf-8")
    rc = cli.main(["barrage", "replay", str(run_path)])
    assert rc == barrage_fire.EXIT_REFUSED
    assert "not valid JSON" in capsys.readouterr().err


def test_barrage_replay_incomplete_config_is_refused(tmp_path, capsys):
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"seed": 1, "config": {"rate": 10.0}}), encoding="utf-8")
    rc = cli.main(["barrage", "replay", str(run_path)])
    assert rc == barrage_fire.EXIT_REFUSED
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
    assert rc == barrage_fire.EXIT_DRY_RUN
    out = capsys.readouterr().out
    assert "seed=12" in out
    assert "closed-loop" in out
    assert "concurrency: 3" in out


# ---------------------------------------------------------------------------
# Synthetic-content guardrail runs before any network call
# ---------------------------------------------------------------------------


def test_require_synthetic_pool_passes_for_the_generated_pool():
    pool = barrage_fire.build_payload_pool(seed=4, pool_size=10)
    barrage_fire.require_synthetic_pool(pool)  # must not raise


def test_require_synthetic_pool_refuses_a_non_reserved_address():
    from testinghq.core.guardrails import GuardrailError

    pool = barrage_fire.build_payload_pool(seed=4, pool_size=2)
    tainted = pool[0].__class__(
        **{**pool[0].__dict__, "to": "real.person@mycompany.com"}
    )
    with pytest.raises(GuardrailError):
        barrage_fire.require_synthetic_pool([tainted])


# ---------------------------------------------------------------------------
# --concurrency in open mode.
#
# Barrage has no executor. testinghq does not import threading anywhere, and
# there is no concurrent.futures or asyncio in testinghq or core/transport. A
# request is issued, its response read, and only then is the next dispatched,
# so exactly one is ever in flight and --concurrency cannot mean anything.
#
# Measured against a real local server sleeping 300ms, driving the shipped
# binary at 6 req/s: open mode achieved 3.14 req/s at --concurrency 1, 3.13 at
# 4 and 3.16 at 64. Closed mode was the same story. The flag was parsed,
# validated, stored in the plan, written into the run artifact and printed by
# the dry-run preview, and did nothing.
#
# These pin the two things that make the tool honest about it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_concurrency_is_accepted_in_both_modes(mode, capsys):
    """Both modes, not just open.

    This used to be `test_concurrency_is_refused_with_a_reason`, and it was
    right at the time: the dispatcher was serial, so `--concurrency` could not
    mean anything and refusing it was the honest thing to do. Its own docstring
    carried the measurement: "concurrency 1, 4 and 64 all produced the same
    throughput in both modes, because `testinghq` has no executor and a send is
    never more than one request in flight."

    The executor exists now, so the flag is the control that decides how much
    load is in flight, and refusing it would be refusing the tool's one job.
    """
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", mode, "--concurrency", "8"]
    )
    capsys.readouterr()

    assert rc == barrage_fire.EXIT_DRY_RUN, (
        f"--concurrency 8 exited {rc} in {mode} mode. It is the one control that "
        "decides how much load is in flight; refusing it is refusing the tool."
    )
    assert "no effect" not in capsys.readouterr().err


def test_the_dry_run_preview_reports_the_requested_concurrency(capsys):
    """The preview has to describe what this build can actually do, which was
    the note above the preview tests. With the executor, the number it prints is
    the number that will be used, and that is the property a preview exists for.
    """
    cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "closed",
         "--concurrency", "8"]
    )
    out = capsys.readouterr().out

    assert "concurrency: 8" in out
    assert "SERIAL" not in out, (
        "the preview no longer dispatches one at a time, so it must not say it "
        "does"
    )

def test_concurrency_of_one_is_accepted_in_both_modes(capsys):
    """1 is the truth, so passing it explicitly is not asking for something
    inert. Refusing it would be pedantry that teaches users to distrust the
    flag."""
    for mode in ("open", "closed"):
        rc = cli.main(
            ["barrage", "fire", "--target", "local", "--mode", mode,
             "--concurrency", "1"]
        )
        assert rc == barrage_fire.EXIT_DRY_RUN
        assert "concurrency: 1" in capsys.readouterr().out


def test_refusing_concurrency_sends_nothing(capsys, forbid_network):
    """The refusal has to happen before any request, not after. A dry run is
    the default here, so this is the easy half to get wrong: a plan that
    previews happily and only then refuses would already have told the
    operator the run was fine."""
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "open",
         "--concurrency", "8", "--send"]
    )
    out = capsys.readouterr()

    assert rc == barrage_fire.EXIT_REFUSED
    assert "dry-run preview" not in out.out
    assert "refused" in out.err


def test_closed_mode_still_records_the_concurrency_it_was_given():
    """Closed mode threads the number into its slot allocation, so the value is
    meaningful the moment an executor exists. Coercing it in `build_plan` would
    throw that away and make the eventual fix harder to see.

    The CLI refuses an explicit value above 1 meanwhile; this is about what the
    plan records, which is the layer `barrage replay` depends on."""
    assert barrage_fire.build_plan("closed", 5.0, 20.0, 8, 5.0).concurrency == 8


def test_omitting_concurrency_resolves_to_the_default_in_both_modes():
    """None must not leak into the plan, and both modes now agree.

    Open mode used to resolve to 1, which was the truth at the time: the
    dispatcher was serial, so one was what it had. With an executor, 1 would be
    a choice to measure almost nothing, and the same default in both modes is
    the one thing that does not need explaining per mode.
    """
    closed = barrage_fire.build_plan("closed", 5.0, 20.0, None, 5.0)
    opened = barrage_fire.build_plan("open", 5.0, 20.0, None, 5.0)

    assert closed.concurrency == barrage_fire.DEFAULT_CONCURRENCY
    assert opened.concurrency == barrage_fire.DEFAULT_CONCURRENCY
    assert opened.concurrency == closed.concurrency


def test_open_mode_records_the_concurrency_it_will_actually_use():
    """A run artifact is a record of what ran.

    This test used to assert the opposite, and its docstring explained why:
    "Recording a number that had no effect makes every future replay of it a
    reproduction of a fiction." That reasoning was right and it is now obsolete
    for a new run, because the number does have an effect. Recording 1 for a
    64-worker run would once again be the fiction.
    """
    plan = barrage_fire.build_plan("open", 5.0, 20.0, 64, 5.0)
    assert plan.concurrency == 64


def test_replay_of_a_legacy_artifact_with_a_meaningless_concurrency_still_works(tmp_path):
    """Artifacts written before this was true carry an inert number, and
    refusing them would break replay of every open-mode run this tool has
    produced. The record is historical; the rebuild has to tolerate it."""
    artifact = {
        "seed": 5,
        "config": {
            "mode": "open", "rate": 10.0, "duration": 20.0, "warmup": 5.0,
            "concurrency": 2, "pool_size": 10, "target": "local", "dry_run": False,
        },
    }
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(artifact), encoding="utf-8")

    assert cli.main(["barrage", "replay", str(run_path)]) == barrage_fire.EXIT_DRY_RUN


# ---------------------------------------------------------------------------
# The dry-run preview has to describe what this build can actually do.
#
# Every test in this section used to assert that the preview said SERIAL, in
# both modes, and warned that a slow target would cap the rate. All three were
# correct and all three described a tool that could not do its job. The
# executor exists now, so the preview describes a pool instead, and these tests
# assert the pool. The shape of each is unchanged, because the property they
# protect is unchanged: the preview must not describe a different build from
# the one that will run.
# ---------------------------------------------------------------------------


def test_dry_run_preview_states_how_many_requests_can_be_in_flight(capsys):
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "open",
         "--concurrency", "16"]
    )
    out = capsys.readouterr().out

    assert rc == barrage_fire.EXIT_DRY_RUN
    assert "pool of 16 workers" in out
    assert "16 request(s) in flight" in out
    assert "SERIAL" not in out


def test_dry_run_preview_says_so_honestly_at_concurrency_one(capsys):
    """The `concurrency == 1` branch is not decoration. One worker really is one
    request in flight, and the operator who asked for it needs the same warning
    the preview gave everyone before, because a single worker against a slow
    target still cannot hold the schedule."""
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "open",
         "--concurrency", "1"]
    )
    out = capsys.readouterr().out

    assert rc == barrage_fire.EXIT_DRY_RUN
    assert "one worker" in out
    assert "slower than the arrival interval" in out
    assert "raise --concurrency" in out, (
        "the warning has to say what to do about it, not only what will happen"
    )


def test_dry_run_preview_states_the_pool_in_closed_mode_too(capsys):
    """Closed mode's default is above 1, so its preview describes a pool. The
    default is still reported alongside it, because a number with no
    description next to it is the thing this section exists to prevent."""
    rc = cli.main(["barrage", "fire", "--target", "local", "--mode", "closed"])
    out = capsys.readouterr().out

    assert rc == barrage_fire.EXIT_DRY_RUN
    assert f"concurrency: {barrage_fire.DEFAULT_CONCURRENCY}" in out
    assert f"pool of {barrage_fire.DEFAULT_CONCURRENCY} workers" in out
    assert "SERIAL" not in out


def test_dry_run_preview_reports_the_memory_cost_of_the_pool(capsys):
    """The pool is one interpreter thread per in-flight request, so a high
    concurrency is a memory cost and not a free setting. A preview is the place
    an operator finds that out, before they set it to 4000."""
    rc = cli.main(
        ["barrage", "fire", "--target", "local", "--mode", "closed",
         "--concurrency", "64"]
    )
    out = capsys.readouterr().out

    assert rc == barrage_fire.EXIT_DRY_RUN
    assert "costs memory" in out
