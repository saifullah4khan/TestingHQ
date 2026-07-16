"""Tests for the M3 CLI wiring: blast generate/fire/replay.

tests/test_cli.py (root) and tests/security/test_send_requires_flag.py pin
the frozen dry-run/--send output contract already; this file covers the
rest of the surface those two do not touch: --rate/--out/--config, the
generate command's on-disk output, zero-network dry runs, the guardrail
refusal paths, and replay's byte-identical reproducibility check.

Named test_cli_blast.py (not test_cli.py) to avoid colliding with the
existing tests/test_cli.py basename; tests/test_lane_hygiene.py enforces
unique basenames repo-wide.
"""
from __future__ import annotations

import json
import socket

import pytest

from testinghq import cli
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
    pairs = cli._build_corpus(seed=2, count=3)
    client = FakeClient(status=200)
    rc = cli._run_fire(
        pairs, seed=2, count=3, target_name="local", rate=50.0, out=None,
        config_path=str(tmp_path / "target.toml"), client=client,
    )
    assert rc == 0
    assert len(client.received) == 3


def test_run_fire_writes_artifact_matching_records_sent(tmp_path, forbid_network):
    _write_target_config(tmp_path)
    pairs = cli._build_corpus(seed=4, count=2)
    client = FakeClient(status=500)
    out_path = tmp_path / "run.json"
    rc = cli._run_fire(
        pairs, seed=4, count=2, target_name="local", rate=50.0, out=str(out_path),
        config_path=str(tmp_path / "target.toml"), client=client,
    )
    assert rc == 0
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["config"]["target"] == "local"
    assert artifact["config"]["dry_run"] is False
    assert len(artifact["records"]) == 2
    for record in artifact["records"]:
        assert record["response"]["status"] == 500


def test_run_fire_refuses_public_host_even_if_configured(tmp_path, forbid_network):
    _write_target_config(tmp_path, name="prod", url="https://ingest.mycompany.com/inbound")
    pairs = cli._build_corpus(seed=1, count=1)
    client = FakeClient()
    rc = cli._run_fire(
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
    original_pairs = cli._build_corpus(seed=6, count=3)
    client_a = FakeClient(status=200)
    run_path = tmp_path / "run.json"
    cli._run_fire(
        original_pairs, seed=6, count=3, target_name="local", rate=50.0,
        out=str(run_path), config_path=str(tmp_path / "target.toml"), client=client_a,
    )

    saved = json.loads(run_path.read_text(encoding="utf-8"))
    replay_pairs = cli._build_corpus(seed=saved["seed"], count=saved["config"]["count"])
    original_hashes = [r["payload_sha256"] for r in saved["records"]]
    replay_hashes = [cli.report.payload_sha256(email) for email, _label in replay_pairs]
    assert replay_hashes == original_hashes

    client_b = FakeClient(status=200)
    rc = cli._run_fire(
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
