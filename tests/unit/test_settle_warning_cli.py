"""Every pipeline tool's dry run warns when a real run's readback may not
have time to settle: two complete polls at --readback-rate plus
--quiet-window must fit inside --max-wait."""
from __future__ import annotations

import pytest

from testinghq import cli

WARNING = "warning: a real run may end UNSETTLED for lack of time"


@pytest.fixture
def http_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(
        '[targets.local]\nurl = "http://127.0.0.1:9/intake"\n\n'
        '[readback]\nkind = "http"\nurl = "http://127.0.0.1:9/tickets"\n',
        encoding="utf-8",
    )
    return str(path)


DRY_RUNS = {
    "verify": ["verify", "fire", "--target", "local"],
    "ledger": ["ledger", "fire", "--target", "local"],
    "redeliver": ["redeliver", "fire", "--target", "local"],
    "loop": ["loop", "fire", "--target", "local"],
    "steady": ["steady", "fire", "--target", "local", "--label-field", "category"],
}


@pytest.mark.parametrize("tool", sorted(DRY_RUNS))
def test_every_tools_dry_run_warns_when_two_polls_cannot_fit(tool, http_config, capsys):
    rc = cli.main([*DRY_RUNS[tool], "--config", http_config, "--max-wait", "3"])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert WARNING in out, out
    assert "over --max-wait 3s" in out


@pytest.mark.parametrize("tool", sorted(DRY_RUNS))
def test_no_warning_when_the_defaults_fit(tool, http_config, capsys):
    """Every tool's default plan fits two polls and the window in 60s at the
    default 4/s, steady's ~107 tags included."""
    rc = cli.main([*DRY_RUNS[tool], "--config", http_config])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert WARNING not in out, out


def test_the_ledger_dry_run_warns_at_the_size_that_no_longer_fits(http_config, capsys):
    """2 x 120 / 4 + 5 = 65s against the default 60s."""
    cli.main(["ledger", "fire", "--target", "local", "--config", http_config, "--count", "120"])
    out = capsys.readouterr().out
    assert "120 tag(s) at --readback-rate 4/s take about 30.0s a poll" in out
    assert "Raise --max-wait to at least 65" in out
    cli.main([
        "ledger", "fire", "--target", "local", "--config", http_config,
        "--count", "120", "--readback-rate", "8",
    ])
    assert WARNING not in capsys.readouterr().out


def test_steady_dry_run_counts_the_repeats(http_config, capsys):
    cli.main([*DRY_RUNS["steady"], "--config", http_config, "--repeats", "3"])
    assert WARNING in capsys.readouterr().out
