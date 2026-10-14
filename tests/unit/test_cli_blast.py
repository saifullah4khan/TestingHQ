"""Tests for the M3 CLI wiring: blast generate/fire/replay.

tests/test_cli.py (root) and tests/security/test_send_requires_flag.py pin
the frozen dry-run/--send output contract already; this file covers the
rest of the surface those two do not touch: --rate/--out/--config, the
generate command's on-disk output, zero-network dry runs, the guardrail
refusal paths, and replay's byte-identical reproducibility check.

Named test_cli_blast.py (not test_cli.py) to avoid colliding with the
existing tests/test_cli.py basename; tests/test_repo_invariants.py enforces
unique basenames repo-wide.
"""
from __future__ import annotations

import json
import socket

import pytest

from testinghq.cli import blast as blast_cli
from testinghq.core import report as core_report
from testinghq import cli
from testinghq.core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.core.transport import ClientResponse


class _NoNetwork:
    """Fails the test if anything tries to open a real socket."""


@pytest.fixture
def forbid_network(monkeypatch):
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


def _write_target_config(tmp_path, name="local", url="http://127.0.0.1:8000/inbound"):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{name}]\nurl = "{url}"\n', encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parser surface: --rate, --out, --config on fire and replay
# ---------------------------------------------------------------------------


def test_fire_parser_has_rate_out_config_with_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["blast", "fire", "--target", "local"])
    assert args.rate == cli.DEFAULT_RATE
    assert args.out is None
    assert args.config == cli.DEFAULT_TARGET_CONFIG


def test_replay_parser_has_rate_out_config_with_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["blast", "replay", "run.json"])
    assert args.run == "run.json"
    assert args.rate == cli.DEFAULT_RATE
    assert args.out is None
    assert args.send is False


# ---------------------------------------------------------------------------
# blast generate: corpus to disk, no network, deterministic
# ---------------------------------------------------------------------------


def test_generate_writes_manifest_and_payload_files(tmp_path, forbid_network):
    out_dir = tmp_path / "corpus"
    rc = cli.main(["blast", "generate", "--count", "5", "--seed", "3", "--out", str(out_dir)])
    assert rc == 0
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 3
    assert manifest["count"] == 5
    assert len(manifest["items"]) == 5
    for item in manifest["items"]:
        assert (out_dir / f"{item['id']}.multipart").is_file()


def test_generate_is_deterministic(tmp_path, forbid_network):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    cli.main(["blast", "generate", "--count", "4", "--seed", "9", "--out", str(out_a)])
    cli.main(["blast", "generate", "--count", "4", "--seed", "9", "--out", str(out_b)])
    manifest_a = json.loads((out_a / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((out_b / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["items"] == manifest_b["items"]
    for item in manifest_a["items"]:
        body_a = (out_a / f"{item['id']}.multipart").read_bytes()
        body_b = (out_b / f"{item['id']}.multipart").read_bytes()
        assert body_a == body_b


# ---------------------------------------------------------------------------
# blast fire: dry run makes zero network calls
# ---------------------------------------------------------------------------


def test_fire_dry_run_makes_zero_network_calls(forbid_network, capsys):
    rc = cli.main(["blast", "fire", "--target", "local", "--count", "3", "--seed", "1"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "dry-run default" in out
    assert "dry-run preview" in out


def test_fire_dry_run_writes_artifact_with_null_status(tmp_path, forbid_network):
    out_path = tmp_path / "run.json"
    rc = cli.main(
        [
            "blast", "fire", "--target", "local", "--count", "3", "--seed", "1",
            "--out", str(out_path),
        ]
    )
    assert rc == 2
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["seed"] == 1
    assert artifact["config"]["dry_run"] is True
    assert artifact["config"]["target"] is None
    assert len(artifact["records"]) == 3
    for record in artifact["records"]:
        assert record["response"]["status"] is None
        assert record["assertion"] == {"passed": True, "mismatches": []}


def test_fire_send_without_configured_target_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no target.toml here
    rc = cli.main(["blast", "fire", "--target", "local", "--send", "--count", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refused" in err


def test_fire_send_missing_target_flag_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["blast", "fire", "--send", "--count", "1"])
    assert rc == 1


# ---------------------------------------------------------------------------
# blast fire send path, hermetically, via _run_fire directly (main() exposes
# no CLI flag to inject a fake client, by design: real usage always uses a
# real socket).
# ---------------------------------------------------------------------------


def test_run_fire_send_path_with_fake_client(tmp_path, forbid_network):
    _write_target_config(tmp_path, name="local", url="http://127.0.0.1:8000/inbound")
    pairs = blast_cli._build_corpus(seed=2, count=3)
    client = FakeClient(status=200)
    rc = blast_cli._run_fire(
        pairs, seed=2, count=3, target_name="local", rate=50.0, out=None,
        config_path=str(tmp_path / "target.toml"), client=client,
    )
    assert rc == 0
    assert len(client.received) == 3


def test_run_fire_writes_artifact_matching_records_sent(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    pairs = blast_cli._build_corpus(seed=4, count=2)
    client = FakeClient(status=500)
    out_path = tmp_path / "run.json"
    rc = blast_cli._run_fire(
        pairs, seed=4, count=2, target_name="local", rate=50.0, out=str(out_path),
        config_path=str(tmp_path / "target.toml"), client=client,
    )
    assert rc == EXIT_FINDING
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["config"]["target"] == "local"
    assert artifact["config"]["dry_run"] is False
    assert len(artifact["records"]) == 2
    for record in artifact["records"]:
        assert record["response"]["status"] == 500


def test_run_fire_refuses_public_host_even_if_configured(tmp_path, forbid_network):
    _write_target_config(tmp_path, name="prod", url="https://ingest.mycompany.com/inbound")
    pairs = blast_cli._build_corpus(seed=1, count=1)
    client = FakeClient()
    rc = blast_cli._run_fire(
        pairs, seed=1, count=1, target_name="prod", rate=50.0, out=None,
        config_path=str(tmp_path / "target.toml"), client=client,
    )
    assert rc == 1
    assert client.received == []


# ---------------------------------------------------------------------------
# blast replay: byte-identical regeneration from seed + config
# ---------------------------------------------------------------------------


def test_replay_dry_run_reproduces_same_payload_hashes(tmp_path, forbid_network):
    run_path = tmp_path / "run.json"
    cli.main(
        [
            "blast", "fire", "--target", "local", "--count", "4", "--seed", "11",
            "--out", str(run_path),
        ]
    )
    rc = cli.main(["blast", "replay", str(run_path)])
    assert rc == 2  # dry-run replay, same convention as dry-run fire


def test_replay_send_path_reproduces_same_corpus_and_fires_it(tmp_path, forbid_network):
    _write_target_config(tmp_path, name="local", url="http://127.0.0.1:8000/inbound")
    original_pairs = blast_cli._build_corpus(seed=6, count=3)
    client_a = FakeClient(status=200)
    run_path = tmp_path / "run.json"
    blast_cli._run_fire(
        original_pairs, seed=6, count=3, target_name="local", rate=50.0,
        out=str(run_path), config_path=str(tmp_path / "target.toml"), client=client_a,
    )

    saved = json.loads(run_path.read_text(encoding="utf-8"))
    replay_pairs = blast_cli._build_corpus(seed=saved["seed"], count=saved["config"]["count"])
    original_hashes = [r["payload_sha256"] for r in saved["records"]]
    replay_hashes = [core_report.payload_sha256(email) for email, _label in replay_pairs]
    assert replay_hashes == original_hashes

    client_b = FakeClient(status=200)
    rc = blast_cli._run_fire(
        replay_pairs, seed=saved["seed"], count=saved["config"]["count"],
        target_name=saved["config"]["target"], rate=50.0, out=None,
        config_path=str(tmp_path / "target.toml"), client=client_b,
    )
    assert rc == 0
    assert len(client_b.received) == 3
    assert [r.body for r in client_a.received] == [r.body for r in client_b.received]


def test_replay_detects_a_tampered_run_file(tmp_path, forbid_network):
    run_path = tmp_path / "run.json"
    cli.main(
        [
            "blast", "fire", "--target", "local", "--count", "2", "--seed", "20",
            "--out", str(run_path),
        ]
    )
    data = json.loads(run_path.read_text(encoding="utf-8"))
    data["records"][0]["payload_sha256"] = "0" * 64
    run_path.write_text(json.dumps(data), encoding="utf-8")

    rc = cli.main(["blast", "replay", str(run_path)])
    assert rc == 1


def test_replay_missing_file_is_reported_not_crashed(tmp_path, capsys):
    rc = cli.main(["blast", "replay", str(tmp_path / "does-not-exist.json")])
    assert rc == 1
    assert "replay" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# blast fire --send: the exit code is the verdict
# ---------------------------------------------------------------------------


def _fire_with(tmp_path, client, pairs=None, out=None, **kwargs):
    _write_target_config(tmp_path)
    if pairs is None:
        pairs = blast_cli._build_corpus(seed=2, count=3)
    return blast_cli._run_fire(
        pairs, seed=2, count=len(pairs), target_name="local", rate=50.0, out=out,
        config_path=str(tmp_path / "target.toml"), client=client, **kwargs,
    )


def test_a_clean_send_exits_ok(tmp_path, forbid_network):
    assert _fire_with(tmp_path, FakeClient(status=200)) == EXIT_OK


def test_a_5xx_on_a_messy_payload_exits_with_a_finding(tmp_path, forbid_network):
    from testinghq.blast.corpus import CorpusPlan, parse_mix

    plan = CorpusPlan(seed=2, count=5, mix=parse_mix("messy=100"))
    rc = _fire_with(tmp_path, FakeClient(status=500), pairs=plan.build(), plan=plan)
    assert rc == EXIT_FINDING


class RefusingClient:
    def send(self, request):
        raise ConnectionRefusedError("[Errno 111] Connection refused")


def test_no_response_is_recorded_with_its_reason(tmp_path, forbid_network, capsys):
    out = tmp_path / "run.json"
    rc = _fire_with(tmp_path, RefusingClient(), out=str(out))
    assert rc == EXIT_FINDING
    artifact = json.loads(out.read_text(encoding="utf-8"))
    for record in artifact["records"]:
        assert record["response"]["error_kind"] == "refused"
        assert "Connection refused" in record["response"]["error"]
    assert artifact["summary"]["transport_errors"]["refused"] == 3
    printed = capsys.readouterr().out
    assert "(refused 3, dns 0, timeout 0, tls 0, other 0)" in printed
    assert "timed out" not in printed


def test_fail_on_body_flags_a_2xx_that_says_error(tmp_path, forbid_network):
    out = tmp_path / "run.json"
    client = FakeClient(status=200, body=b'{"status":"error"}')
    rc = _fire_with(tmp_path, client, out=str(out), fail_on_body='"status":"error"')
    assert rc == EXIT_FINDING
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["config"]["fail_on_body"] == '"status":"error"'
    assert all("failure body" in flag for flag in artifact["summary"]["flags"])
    assert _fire_with(tmp_path, client) == EXIT_OK, "no pattern, no body inspection"


def test_an_invalid_fail_on_body_pattern_is_refused(forbid_network, capsys):
    rc = cli.main(["blast", "fire", "--count", "1", "--fail-on-body", "("])
    assert rc == EXIT_REFUSED
    assert "--fail-on-body" in capsys.readouterr().err


class InterruptingClient:
    """Answers once, then the operator presses Ctrl-C."""

    def __init__(self):
        self.calls = 0

    def send(self, request):
        self.calls += 1
        if self.calls > 1:
            raise KeyboardInterrupt
        return ClientResponse(status=200, body=b"ok")


def test_ctrl_c_writes_what_was_sent_and_exits_with_a_finding(tmp_path, forbid_network):
    out = tmp_path / "run.json"
    rc = _fire_with(tmp_path, InterruptingClient(), out=str(out))
    assert rc == EXIT_FINDING
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["aborted"] is True
    assert len(artifact["records"]) == 1


def test_the_send_artifact_carries_schema_version_tool_and_the_plan(tmp_path, forbid_network):
    out = tmp_path / "run.json"
    _fire_with(tmp_path, FakeClient(), out=str(out))
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["tool"] == "blast"
    assert artifact["config"]["attachments"] == "seeded"
    assert sum(artifact["config"]["mix_percent"].values()) == pytest.approx(100)


# ---------------------------------------------------------------------------
# --mix, --catalog, --attachments, --count from the command line
# ---------------------------------------------------------------------------


def _generate(tmp_path, *flags):
    out_dir = tmp_path / "corpus"
    rc = cli.main(["blast", "generate", "--out", str(out_dir), *flags])
    manifest = None
    if (out_dir / "manifest.json").exists():
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    return rc, manifest


def test_generate_honours_mix(tmp_path, forbid_network):
    rc, manifest = _generate(tmp_path, "--count", "20", "--mix", "degenerate=100")
    assert rc == EXIT_OK
    assert {item["category"] for item in manifest["items"]} == {"degenerate"}
    assert manifest["schema_version"] == 1


def test_generate_refuses_a_mix_that_does_not_add_up(tmp_path, forbid_network, capsys):
    rc, manifest = _generate(tmp_path, "--mix", "clean=30,degenerate=30")
    assert rc == EXIT_REFUSED
    assert manifest is None
    assert "add up to 100" in capsys.readouterr().err


def test_generate_and_fire_reach_the_catalog(tmp_path, forbid_network):
    from testinghq.blast.catalog import CATALOG

    rc, manifest = _generate(tmp_path, "--catalog")
    assert rc == EXIT_OK
    assert sorted(item["case"] for item in manifest["items"]) == sorted(CATALOG)

    out = tmp_path / "run.json"
    rc = cli.main(["blast", "fire", "--catalog", "empty_subject,many_recipients", "--out", str(out)])
    assert rc == EXIT_DRY_RUN
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert [r["case"] for r in artifact["records"]] == ["empty_subject", "many_recipients"]
    assert artifact["config"]["catalog"] == ["empty_subject", "many_recipients"]


def test_catalog_with_count_is_refused(tmp_path, forbid_network):
    rc, _manifest = _generate(tmp_path, "--catalog", "--count", "5")
    assert rc == EXIT_REFUSED


def test_attachments_mode_reaches_the_corpus(tmp_path, forbid_network, capsys):
    rc = cli.main(["blast", "fire", "--count", "60", "--attachments", "none"])
    assert rc == EXIT_DRY_RUN
    assert "with attachments: 0" in capsys.readouterr().out
    rc = cli.main(["blast", "fire", "--count", "60"])
    assert "with attachments: 0" not in capsys.readouterr().out, "seeded is the default"


@pytest.mark.parametrize("command", ["generate", "fire"])
def test_a_negative_count_is_a_refusal_not_a_traceback(tmp_path, forbid_network, capsys, command):
    argv = ["blast", command, "--count", "-3"]
    if command == "generate":
        argv += ["--out", str(tmp_path / "corpus")]
    rc = cli.main(argv)
    assert rc == EXIT_REFUSED
    assert "--count must be 0 or more" in capsys.readouterr().err


def test_replay_rebuilds_a_catalog_and_mix_run(tmp_path, forbid_network):
    for flags in (
        ["--catalog", "zero_byte_attachment,oversized_body_1mb"],
        ["--count", "6", "--mix", "clean=50,malformed=50", "--attachments", "adversarial"],
    ):
        run_path = tmp_path / "run.json"
        assert cli.main(["blast", "fire", *flags, "--out", str(run_path)]) == EXIT_DRY_RUN
        assert cli.main(["blast", "replay", str(run_path)]) == EXIT_DRY_RUN


def test_replay_refuses_a_newer_schema_version(tmp_path, forbid_network, capsys):
    run_path = tmp_path / "run.json"
    cli.main(["blast", "fire", "--count", "2", "--out", str(run_path)])
    data = json.loads(run_path.read_text(encoding="utf-8"))
    data["schema_version"] = 2
    run_path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["blast", "replay", str(run_path)]) == EXIT_REFUSED
    assert "schema_version" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --catalog through the send guard
# ---------------------------------------------------------------------------


def test_the_full_catalog_passes_the_send_guard(tmp_path, forbid_network):
    """Every catalog case, including the zero-length envelope with its empty
    To, is synthetic enough to send."""
    from testinghq.blast.corpus import CorpusPlan, parse_catalog

    config = _write_target_config(tmp_path)
    plan = CorpusPlan(seed=0, catalog=parse_catalog("all"))
    pairs = plan.build()
    assert any(email.to == "" for email, _label in pairs), "the empty-To case is in the catalog"
    args = cli.build_parser().parse_args(
        ["blast", "fire", "--target", "local", "--config", str(config), "--send", "--catalog"]
    )
    assert blast_cli._validate_send_plan(pairs, args, "local") is None


def test_the_catalog_with_a_real_address_spliced_in_is_still_refused(tmp_path, forbid_network):
    from testinghq.blast.corpus import CorpusPlan, parse_catalog

    config = _write_target_config(tmp_path)
    pairs = CorpusPlan(seed=0, catalog=parse_catalog("all")).build()
    email, label = pairs[0]
    pairs[0] = (email.with_changes(to="Someone <someone@gmail.com>"), label)
    args = cli.build_parser().parse_args(
        ["blast", "fire", "--target", "local", "--config", str(config), "--send", "--catalog"]
    )
    refusal = blast_cli._validate_send_plan(pairs, args, "local")
    assert refusal is not None and "someone@gmail.com" in refusal


def test_blast_fire_send_catalog_sends_every_case(tmp_path, forbid_network, monkeypatch, capsys):
    """The README command, end to end in process: nothing refused, one request
    per catalog case."""
    from testinghq.blast.corpus import CorpusPlan, parse_catalog

    fake = FakeClient(status=200)
    real_post = blast_cli.post
    monkeypatch.setattr(
        blast_cli, "post",
        lambda email, url, client=None, wire_format=None: real_post(
            email, url, client=fake, wire_format=wire_format
        ),
    )
    config = _write_target_config(tmp_path)
    out = tmp_path / "blast.json"
    rc = cli.main(["blast", "fire", "--target", "local", "--config", str(config),
                   "--send", "--catalog", "--rate", "1000", "--out", str(out)])
    captured = capsys.readouterr()
    assert rc in (EXIT_OK, EXIT_FINDING), captured.err
    assert "refused" not in captured.err
    expected = len(CorpusPlan(seed=0, catalog=parse_catalog("all")).build())
    assert len(fake.received) == expected
    assert len(json.loads(out.read_text(encoding="utf-8"))["records"]) == expected

