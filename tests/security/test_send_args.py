"""`--allow-public-target` on the command line, and `blast generate --target`."""
from __future__ import annotations

import json
import socket

import pytest

from testinghq import cli
from testinghq.cli.common import resolve_for_send
from testinghq.core import guardrails

PUBLIC_HOST = "intake.mycompany.com"

#: Every command that can put a request on the wire.
SEND_COMMANDS = [
    ["blast", "fire"],
    ["blast", "replay", "run.json"],
    ["barrage", "fire"],
    ["barrage", "replay", "run.json"],
    ["verify", "fire"],
    ["ledger", "fire"],
    ["loop", "fire"],
    ["redeliver", "fire"],
    ["steady", "fire"],
]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        if host == PUBLIC_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guardrails.clear_dns_cache()
    yield
    guardrails.clear_dns_cache()


def _config(tmp_path, url, fmt="sendgrid"):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.t]\nurl = "{url}"\nformat = "{fmt}"\n', encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("command", SEND_COMMANDS, ids=" ".join)
def test_every_send_command_accepts_the_flag_and_defaults_it_off(command):
    parser = cli.build_parser()
    assert parser.parse_args(command).allow_public_target is False
    assert parser.parse_args(command + ["--allow-public-target"]).allow_public_target is True


@pytest.mark.parametrize(
    "command", [["report", "x.json"], ["config", "validate"], ["blast", "generate"]],
    ids=" ".join,
)
def test_commands_that_cannot_send_do_not_offer_it(command):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(command + ["--allow-public-target"])


def test_blast_fire_at_a_public_target_is_refused_before_any_announcement(tmp_path, capsys):
    config = _config(tmp_path, f"https://{PUBLIC_HOST}/in")
    code = cli.main(["blast", "fire", "--target", "t", "--send", "--config", config])
    captured = capsys.readouterr()
    assert code == 1
    assert "--allow-public-target" in captured.err
    assert "explicit --send flag set" not in captured.out


def test_resolve_for_send_reads_the_flag_and_the_target_from_args(tmp_path):
    config = _config(tmp_path, f"https://{PUBLIC_HOST}/in")
    args = cli.build_parser().parse_args(
        ["blast", "fire", "--target", "t", "--config", config, "--allow-public-target"]
    )
    assert resolve_for_send(args, "blast").public is True

    args.allow_public_target = False
    with pytest.raises(guardrails.GuardrailError):
        resolve_for_send(args, "blast")


def test_resolve_for_send_takes_an_explicit_target_name(tmp_path):
    config = _config(tmp_path, "http://127.0.0.1:9/in")
    args = cli.build_parser().parse_args(["blast", "replay", "run.json", "--config", config])
    assert resolve_for_send(args, "blast", target_name="t").name == "t"
    with pytest.raises(guardrails.GuardrailError, match="requires --target"):
        resolve_for_send(args, "blast", target_name=None)


def test_blast_generate_writes_the_named_targets_format(tmp_path):
    config = _config(tmp_path, "http://127.0.0.1:9/in", fmt="postmark")
    out = tmp_path / "corpus"
    code = cli.main(["blast", "generate", "--count", "3", "--out", str(out),
                     "--target", "t", "--config", config])
    assert code == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "postmark"
    files = sorted(out.glob("*.json"))
    files = [f for f in files if f.name != "manifest.json"]
    assert len(files) == 3
    assert "TextBody" in json.loads(files[0].read_text(encoding="utf-8"))


def test_blast_generate_without_a_target_writes_sendgrid(tmp_path):
    out = tmp_path / "corpus"
    assert cli.main(["blast", "generate", "--count", "2", "--out", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "sendgrid"
    body = next(out.glob("*.multipart")).read_bytes()
    assert b'name="dkim"' in body


def test_blast_generate_with_an_unknown_target_is_refused(tmp_path):
    config = _config(tmp_path, "http://127.0.0.1:9/in")
    code = cli.main(["blast", "generate", "--out", str(tmp_path / "c"),
                     "--target", "nope", "--config", config])
    assert code == 1
