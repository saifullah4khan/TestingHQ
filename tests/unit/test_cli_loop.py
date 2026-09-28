"""The CLI surface for `loop`, and the `[loop]` table it reads.

The configuration tests matter more here than for the other tools, because
`core/config.py` is owned elsewhere and this tool reads its own section. That
makes the local reader a second TOML reader in the tree, so the tests are about
its edges: what a missing file does, what a malformed table does, and the fact
that an absent `[loop.outbound]` means SKIPPED rather than a default adapter
that would look like it had checked something.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from testinghq import cli
from testinghq.pipeline import loop as tool
from testinghq.pipeline.adapters import AdapterError
from testinghq.pipeline.common import EXIT_DRY_RUN, EXIT_REFUSED

TARGET = "local"
TARGET_URL = "http://localhost:9/intake"

BASE = f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n'


def _config(tmp_path, body="") -> str:
    path = tmp_path / "target.toml"
    path.write_text(
        BASE
        + '\n[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n'
        + body,
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_loop_is_reachable_and_takes_the_shared_arguments():
    args = cli.build_parser().parse_args(["loop", "fire", "--target", TARGET])
    assert args.tool == "loop" and args.command == "fire"
    for flag in ("readback", "quiet_window", "max_wait", "poll_interval",
                 "tag_prefix", "config", "out", "send", "target", "seed", "count"):
        assert hasattr(args, flag), flag


def test_loop_defaults_to_refusing_machine_mail_ticketing():
    """The brief's default, and the stricter one. Getting it wrong turns a
    pipeline that tickets bulk mail into a passing run."""
    args = cli.build_parser().parse_args(["loop", "fire"])
    assert args.ticket_policy == tool.TICKET_POLICY_NONE


def test_the_ticket_policy_flag_is_constrained():
    parser = cli.build_parser()
    assert parser.parse_args(
        ["loop", "fire", "--ticket-policy", "allowed"]
    ).ticket_policy == "allowed"
    with pytest.raises(SystemExit):
        parser.parse_args(["loop", "fire", "--ticket-policy", "sometimes"])


def test_loop_carries_no_high_rate_escape_hatch():
    args = cli.build_parser().parse_args(["loop", "fire"])
    assert not hasattr(args, "allow_high_rate")
    assert args.rate > 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_loop_fire_does_nothing_without_send(tmp_path, capsys):
    code = cli.main(
        ["loop", "fire", "--target", TARGET, "--config", _config(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert "dry-run default" in out
    assert "no network calls were made" in out
    assert "loop bait" in out


def test_the_dry_run_lists_every_shape_it_would_send(tmp_path, capsys):
    cli.main(
        ["loop", "fire", "--target", TARGET, "--config", _config(tmp_path),
         "--count", "4"]
    )
    out = capsys.readouterr().out
    assert "loop-bait" in out
    assert "precedence-bulk" in out
    assert "auto-submitted-auto-replied" in out


# ---------------------------------------------------------------------------
# The [loop] table
# ---------------------------------------------------------------------------


def test_a_missing_config_is_refused_not_ignored(tmp_path):
    """`core/config.py` is owned elsewhere, so this tool reads the table itself,
    and a file that is not there has to be a refusal. A default that quietly
    stands in would report a run that checked nothing."""
    with pytest.raises(tool.LoopConfigError):
        tool.load_loop_config(str(tmp_path / "nope.toml"))


def test_a_malformed_config_is_refused(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(tool.LoopConfigError):
        tool.load_loop_config(str(path))


def test_a_loop_section_that_is_not_a_table_is_refused(tmp_path):
    # Before the tables, deliberately: in TOML a bare key written after a
    # `[table]` header belongs to that table, so the same mistake lands inside
    # `[targets.local]` and the guard never sees it. That version of this test
    # passed for the wrong reason.
    path = tmp_path / "t.toml"
    path.write_text('loop = "yes"\n\n' + BASE, encoding="utf-8")
    with pytest.raises(tool.LoopConfigError) as caught:
        tool.load_loop_config(str(path))
    assert "must be a table" in str(caught.value)


def test_a_non_string_reply_address_is_a_config_error_not_a_guardrail_one(tmp_path):
    """An integer reply address used to reach `require_synthetic_content`,
    which reported "no addresses could be extracted". That points at the content
    rather than at the key that is wrong."""
    path = _config(tmp_path, "\n[loop]\nreply_address = 7\n")
    with pytest.raises(tool.LoopConfigError) as caught:
        tool.load_loop_config(path)
    assert "reply_address must be a string" in str(caught.value)


def test_an_unknown_ticket_policy_in_config_is_refused(tmp_path):
    path = _config(tmp_path, '\n[loop]\nticket_policy = "sometimes"\n')
    with pytest.raises(tool.LoopConfigError) as caught:
        tool.load_loop_config(path)
    assert "ticket_policy must be one of" in str(caught.value)


def test_no_loop_section_reads_as_empty_not_an_error(tmp_path):
    assert tool.load_loop_config(_config(tmp_path)) == {}


def test_an_absent_outbound_section_is_none(tmp_path):
    """None is the answer that matters: it is what makes the auto-reply check
    report SKIPPED instead of passing."""
    section = tool.load_loop_config(_config(tmp_path))
    assert tool.build_outbound_config(section, _config(tmp_path)) is None


def test_an_outbound_table_becomes_a_readback_config(tmp_path):
    path = _config(
        tmp_path,
        '\n[loop.outbound]\nkind = "mailbox"\npath = "./out.jsonl"\n',
    )
    config = tool.build_outbound_config(tool.load_loop_config(path), path)
    assert config.kind == "mailbox"
    assert config.path.endswith("out.jsonl")


def test_a_relative_outbound_path_resolves_against_the_config_directory(tmp_path):
    """A sink at `out.jsonl` does not mean the same thing relative to the
    current working directory as it does relative to the config naming it, and
    the difference is a silent report of nothing found."""
    path = _config(
        tmp_path,
        '\n[loop.outbound]\nkind = "mailbox"\npath = "out.jsonl"\n',
    )
    config = tool.build_outbound_config(tool.load_loop_config(path), path)
    assert Path(config.path).is_absolute()
    assert Path(config.path).parent == Path(path).resolve().parent


def test_an_absolute_outbound_path_is_left_alone(tmp_path):
    sink = tmp_path / "sink.jsonl"
    path = _config(
        tmp_path, f'\n[loop.outbound]\nkind = "mailbox"\npath = "{sink.as_posix()}"\n'
    )
    config = tool.build_outbound_config(tool.load_loop_config(path), path)
    assert Path(config.path) == sink


def test_an_outbound_section_that_is_not_a_table_is_refused(tmp_path):
    path = _config(tmp_path, '\n[loop]\noutbound = "somewhere"\n')
    with pytest.raises(tool.LoopConfigError) as caught:
        tool.build_outbound_config(tool.load_loop_config(path), path)
    assert "must be a table" in str(caught.value)


def test_the_reply_address_is_read_from_the_table(tmp_path, capsys):
    path = _config(tmp_path, '\n[loop]\nreply_address = "bot@intake.example.com"\n')
    code = cli.main(
        ["loop", "fire", "--target", TARGET, "--config", path, "--count", "1"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert "loop-bait" in out


def test_a_malformed_loop_table_refuses_the_run(tmp_path, capsys):
    path = _config(tmp_path, '\n[loop]\nreply_address = 7\n')
    code = cli.main(
        ["loop", "fire", "--target", TARGET, "--config", path, "--send"]
    )
    captured = capsys.readouterr()
    # A refusal raised inside `execute` goes to the tool's own report channel,
    # which is stdout, the same as `verify` and the rest. Both streams are read
    # because which one carries it is not the property under test.
    assert code == EXIT_REFUSED
    assert "refused" in captured.out + captured.err
    assert "reply_address must be a string" in captured.out + captured.err


def test_a_missing_readback_is_refused_before_the_dry_run(tmp_path, capsys):
    """Same rule as every other tool: no adapter means no evidence, so the run
    does not start, and a dry-run preview is not printed for a run that could
    never happen."""
    path = tmp_path / "no-readback.toml"
    path.write_text(BASE, encoding="utf-8")
    code = cli.main(["loop", "fire", "--target", TARGET, "--config", str(path)])
    captured = capsys.readouterr()
    assert code == EXIT_REFUSED
    assert "no readback adapter configured" in captured.out + captured.err
    assert "dry-run preview" not in captured.out


def test_an_unimportable_readback_spec_is_refused(tmp_path, capsys):
    code = cli.main(
        ["loop", "fire", "--target", TARGET, "--config", _config(tmp_path),
         "--readback", "no.such.module:build", "--send"]
    )
    captured = capsys.readouterr()
    assert code == EXIT_REFUSED
    assert "could not import" in captured.out + captured.err


# ---------------------------------------------------------------------------
# The artifact config
# ---------------------------------------------------------------------------


def test_the_artifact_config_records_the_policy_that_was_used():
    from testinghq.pipeline.adapters import ReadbackConfig

    config = tool.run_config(
        1, 10, "spike", "local", ReadbackConfig(kind="http"), None,
        tool.TICKET_POLICY_ALLOWED, "bot@example.com",
        {"quiet_window": 1.0, "max_wait": 30.0, "poll_interval": 0.5},
    )
    assert config["tool"] == "loop"
    assert config["ticket_policy"] == "allowed"
    assert config["outbound"] is None
    assert config["readback_poll"]["max_wait"] == 30.0
    assert "latency" not in config and "timestamp" not in config


def test_a_summary_never_infers_the_policy_from_the_outcome():
    """An earlier version reported the policy it thought it had used, derived
    from whether tickets were found. A run with no findings would then report
    `none` whatever the operator asked for, which is a lie in an artifact."""
    from testinghq.pipeline.adapters import ReadbackConfig

    results = tool.judge(tool.build_machine_corpus(1, 2, "hq"), {}, None,
                         tool.TICKET_POLICY_ALLOWED)
    summary = tool.summarize(results, [None, None], tool.TICKET_POLICY_ALLOWED)
    assert summary["ticket_policy"] == "allowed"
    assert summary["findings"] == 0
    del ReadbackConfig
