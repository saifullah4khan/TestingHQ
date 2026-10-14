"""The readback polling flags are checked by the parser, so a value that
could never work is a usage refusal (exit 1) before anything is sent."""
from __future__ import annotations

import pytest

from testinghq import cli


@pytest.fixture
def http_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(
        '[targets.local]\nurl = "http://127.0.0.1:9/intake"\n\n'
        '[readback]\nkind = "http"\nurl = "http://127.0.0.1:9/tickets"\n',
        encoding="utf-8",
    )
    return str(path)


LEDGER = ["ledger", "fire", "--target", "local"]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--quiet-window", "-1"),
        ("--quiet-window", "abc"),
        ("--max-wait", "0"),
        ("--max-wait", "-5"),
        ("--max-wait", "nan"),
        ("--poll-interval", "0"),
        ("--poll-interval", "-0.5"),
        ("--poll-interval", "inf"),
    ],
)
def test_a_bad_polling_flag_is_a_usage_refusal(flag, value, http_config, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([*LEDGER, "--config", http_config, flag, value, "--send"])
    assert exc.value.code == 1
    assert f"argument {flag}" in capsys.readouterr().err


def test_a_zero_quiet_window_is_still_accepted(http_config, capsys):
    """Zero is a legal window: settle on the first two identical polls."""
    rc = cli.main([*LEDGER, "--config", http_config, "--quiet-window", "0"])
    assert rc == 2
