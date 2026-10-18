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
    """2 x 120 / 4 + 5 = 65s against a given --max-wait of 60s."""
    cli.main([
        "ledger", "fire", "--target", "local", "--config", http_config,
        "--count", "120", "--max-wait", "60",
    ])
    out = capsys.readouterr().out
    assert "readback max-wait: 60s (given)" in out
    assert "120 tag(s) at --readback-rate 4/s take about 30.0s a poll" in out
    assert "Raise --max-wait to at least 65" in out
    cli.main([
        "ledger", "fire", "--target", "local", "--config", http_config,
        "--count", "120", "--readback-rate", "8", "--max-wait", "60",
    ])
    assert WARNING not in capsys.readouterr().out


def test_the_default_max_wait_is_raised_to_fit_the_plan(http_config, capsys):
    """Without --max-wait the same plan gets 65s plus 25%, 81.25, so 82s, and
    the dry run says so instead of warning."""
    cli.main(["ledger", "fire", "--target", "local", "--config", http_config, "--count", "120"])
    out = capsys.readouterr().out
    assert WARNING not in out
    assert (
        "readback max-wait: 82s (the 60s default, raised to fit the plan: 120 "
        "tag(s) at --readback-rate 4/s need about 65.0s"
    ) in out


@pytest.mark.parametrize("tool", sorted(set(DRY_RUNS) - {"steady"}))
def test_a_default_plan_well_inside_60s_keeps_the_60s_default(tool, http_config, capsys):
    cli.main([*DRY_RUNS[tool], "--config", http_config])
    assert "readback max-wait: 60s (default)" in capsys.readouterr().out


def test_steadys_default_plan_gets_its_margin(http_config, capsys):
    """105 tags need 2 x 26.25 + 5 = 57.5s: inside 60, but not with room for a
    store slower than instant, so the default is 57.5 plus 25%, 72s."""
    cli.main([*DRY_RUNS["steady"], "--config", http_config])
    assert (
        "readback max-wait: 72s (the 60s default, raised to fit the plan: 105 tag(s)"
    ) in capsys.readouterr().out


def test_steady_dry_run_counts_the_repeats(http_config, capsys):
    """The default seed with --repeats 3 is 105 variants plus 18 repeats."""
    cli.main([*DRY_RUNS["steady"], "--config", http_config, "--repeats", "3"])
    out = capsys.readouterr().out
    assert WARNING not in out
    assert "readback max-wait: 84s (the 60s default, raised to fit the plan: 123 tag(s)" in out
    cli.main([
        *DRY_RUNS["steady"], "--config", http_config, "--repeats", "3", "--max-wait", "60",
    ])
    out = capsys.readouterr().out
    assert WARNING in out
    assert "123 tag(s) at --readback-rate 4/s" in out


@pytest.mark.parametrize(
    "argv", [*(argv[:2] for argv in DRY_RUNS.values()), ["verify", "check"]],
)
def test_every_readback_tools_help_says_the_default_grows_with_the_plan(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main([*argv, "--help"])
    assert raised.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "seconds to keep polling (default 60, raised to fit a bigger plan" in help_text
    assert "plus 25%;" in help_text
