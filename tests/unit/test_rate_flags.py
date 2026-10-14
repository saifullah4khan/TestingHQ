"""Every `--rate` flag refuses a rate that is not a positive number.

A zero or negative `--rate` used to reach the token bucket after the command
had announced it was sending, and `blast fire` and `blast replay` died there
with a traceback. The flags now share one argparse type, so the command line is
refused (exit 1) before any command code runs, which also means nothing can
have been sent.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from testinghq import cli
from testinghq.cli.common import positive_number
from testinghq.core.exit_codes import EXIT_REFUSED

REPO_ROOT = Path(__file__).resolve().parents[2]

#: One command line per `--rate` flag, each otherwise a valid send.
SENDING_COMMANDS = [
    ["blast", "fire", "--target", "local", "--send"],
    ["blast", "replay", "run.json", "--send"],
    ["barrage", "fire", "--target", "local", "--send"],
    ["verify", "fire", "--target", "local", "--send"],
    ["ledger", "fire", "--target", "local", "--send"],
    ["redeliver", "fire", "--target", "local", "--send"],
    ["loop", "fire", "--target", "local", "--send"],
    ["steady", "fire", "--target", "local", "--send"],
]


@pytest.mark.parametrize("argv", SENDING_COMMANDS, ids=lambda a: " ".join(a[:2]))
@pytest.mark.parametrize("rate", ["0", "-5", "nan", "inf", "fast"])
def test_a_rate_that_is_not_a_positive_number_is_refused_by_the_parser(
    argv, rate, monkeypatch, capsys
):
    def ran(*_args, **_kwargs):
        raise AssertionError("a command ran with an invalid --rate")

    for module in (cli.blast, cli.barrage, cli.pipeline):
        for name in dir(module):
            if name.startswith("_cmd_"):
                monkeypatch.setattr(module, name, ran)

    with pytest.raises(SystemExit) as raised:
        cli.main([*argv, "--rate", rate])
    err = capsys.readouterr().err
    assert raised.value.code == EXIT_REFUSED
    assert "--rate" in err
    assert "Traceback" not in err


def test_readback_rate_uses_the_same_type(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["ledger", "fire", "--target", "local", "--readback-rate", "0"])
    assert raised.value.code == EXIT_REFUSED
    assert "--readback-rate" in capsys.readouterr().err


def test_a_positive_rate_is_accepted():
    assert positive_number("0.5") == 0.5
    args = cli.build_parser().parse_args(["blast", "fire", "--rate", "12"])
    assert args.rate == 12.0


@pytest.mark.parametrize(
    "argv",
    [
        ["blast", "fire", "--rate", "0", "--send", "--target", "local", "--count", "1"],
        ["blast", "replay", "missing.json", "--rate", "-1", "--send"],
    ],
    ids=["fire", "replay"],
)
def test_the_process_exits_1_with_no_traceback(tmp_path, argv):
    """The defect as reported: a real process, exit 1, a usage line, no
    traceback and no "explicit --send" announcement."""
    config = tmp_path / "target.toml"
    config.write_text('[targets.local]\nurl = "http://127.0.0.1:9/inbound"\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *argv, "--config", str(config)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == EXIT_REFUSED
    assert "Traceback" not in result.stderr
    assert "--rate" in result.stderr
    assert "explicit --send" not in result.stdout
