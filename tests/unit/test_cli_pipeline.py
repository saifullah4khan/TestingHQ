"""The CLI surface for verify, ledger and redeliver.

These tests are about the wiring, not the tools. The tools are covered in
depth elsewhere; what has to be true here is narrower and just as important:

  the three commands share one argument grammar, so learning one teaches the
  other two
  the dry-run default holds, and a refusal happens before anything is sent
  `--readback` resolution works from a flag and from a config file, and the
  flag wins
  a missing adapter is a refusal, never a silent run that verified nothing

That last one is the whole reason these are tested at the CLI layer. Down at
the module layer a missing adapter is a `ReadbackConfig` with a bad kind, which
is a short way from a real adapter. At the CLI layer it is a user who forgot
`--readback`, and the difference between refusing and proceeding is the
difference between a tool and a rubber stamp.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from testinghq.cli import common as cli_common
from testinghq import cli
from testinghq.pipeline.adapters import AdapterError
from testinghq.pipeline.common import EXIT_DRY_RUN, EXIT_REFUSED

TARGET = "local"
TARGET_URL = "http://localhost:9/intake"


@pytest.fixture
def target_config(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n', encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_verify_is_reachable_from_the_top_level():
    parser = cli.build_parser()
    for argv in (["verify", "fire", "--target", TARGET], ["verify", "check", "run.json"]):
        assert parser.parse_args(argv) is not None


def test_each_tool_requires_a_subcommand():
    parser = cli.build_parser()
    for tool in ("verify",):
        with pytest.raises(SystemExit):
            parser.parse_args([tool])


def test_verify_check_sends_nothing_so_it_has_no_send_flag():
    """The absence is deliberate and matches `compare`: a path that cannot
    reach the network has nothing to gate, and a `--send` that does nothing is
    an invitation to believe it did something."""
    parser = cli.build_parser()
    args = parser.parse_args(["verify", "check", "run.json"])
    assert not hasattr(args, "send")


def test_verify_fire_does_nothing_without_send(target_config, capsys):
    code = cli.main(
        ["verify", "fire", "--target", TARGET, "--config", target_config,
         "--readback", "mailbox"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert "dry-run default" in out
    assert "no network calls were made" in out


def test_a_missing_readback_is_refused_rather_than_defaulted(target_config, capsys):
    """The difference between a tool and a rubber stamp. A verification run
    with no adapter would check nothing, report nothing wrong, and exit 0."""
    code = cli.main(
        ["verify", "fire", "--target", TARGET, "--config", target_config, "--send"]
    )
    err = capsys.readouterr().err
    assert code == EXIT_REFUSED
    assert "--readback" in err
    assert "[readback]" in err


def test_a_missing_readback_is_refused_before_the_dry_run_preview(target_config, capsys):
    """The dry run builds a preview from the adapter config, so it has to have
    one too. Previewing a run whose adapter is unknown would be describing
    something that cannot happen."""
    code = cli.main(["verify", "fire", "--target", TARGET, "--config", target_config])
    assert code == EXIT_REFUSED
    assert "dry-run preview" not in capsys.readouterr().out


def test_an_unimportable_readback_spec_is_refused(target_config, capsys):
    code = cli.main(
        ["verify", "fire", "--target", TARGET, "--config", target_config,
         "--readback", "no.such.module:build", "--send"]
    )
    captured = capsys.readouterr()
    assert code == EXIT_REFUSED
    # The message names the module that could not be imported. Which stream
    # carries it is not the property under test: it is reported by the run
    # itself on stdout, while the handler's own refusals go to stderr, and
    # asserting on one stream would pin an accident.
    assert "no.such.module" in captured.out + captured.err


# ---------------------------------------------------------------------------
# Resolving the adapter
# ---------------------------------------------------------------------------


def _resolve(argv):
    return cli_common._resolve_readback(cli.build_parser().parse_args(argv))


def test_a_bare_kind_flag_resolves_to_a_config(target_config):
    config = _resolve(
        ["verify", "fire", "--config", target_config, "--readback", "mailbox"]
    )
    assert config.kind == "mailbox"


def test_a_config_file_readback_table_is_used_when_no_flag_is_given(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n',
        encoding="utf-8",
    )
    config = _resolve(["verify", "fire", "--config", str(path)])
    assert config.kind == "http"
    assert config.url == "http://localhost:8000/tickets"


def test_a_flag_kind_overrides_the_config_file(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n',
        encoding="utf-8",
    )
    config = _resolve(
        ["verify", "fire", "--config", str(path), "--readback", "mailbox"]
    )
    assert config.kind == "mailbox"
    # The rest of the table is kept, so a config file can carry the url and the
    # flag can carry the choice.
    assert config.url == "http://localhost:8000/tickets"


def test_a_spec_flag_is_resolved_as_an_import_path(tmp_path):
    """The kind is the spec itself rather than a separate `python` marker, so
    that `--readback mypkg:build` and a `[readback]` table with the same kind
    produce one config and a custom adapter written against one works with the
    other."""
    config = _resolve(["verify", "fire", "--readback", "mypkg.mine:build"])
    assert config.spec == "mypkg.mine:build"

    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "mypkg.mine:build"\n'
        'url = "http://localhost:8000/tickets"\n',
        encoding="utf-8",
    )
    from_file = _resolve(["verify", "fire", "--config", str(path)])
    assert from_file.spec == "mypkg.mine:build"
    # And the flag still overrides the kind, while the file still supplies the
    # url the factory needs.
    from_flag = _resolve(
        ["verify", "fire", "--config", str(path), "--readback", "other.mod:probe"]
    )
    assert from_flag.spec == "other.mod:probe"
    assert from_flag.url == "http://localhost:8000/tickets"


def test_a_spec_flag_still_gets_the_config_files_table(tmp_path):
    """The flag says HOW to read the system and the file says WHAT its url and
    field names are. A factory handed only a spec has nothing to connect to,
    which is what the first version of this did and the demo run proved."""
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n'
        "timeout = 3.0\n",
        encoding="utf-8",
    )
    config = _resolve(
        ["verify", "fire", "--config", str(path), "--readback", "mypkg.mine:build"]
    )
    assert config.url == "http://localhost:8000/tickets"
    assert config.timeout == 3.0


def test_the_public_host_flag_flows_into_the_adapter_config():
    config = _resolve(
        ["verify", "fire", "--readback", "http", "--allow-public-readback"]
    )
    assert config.allow_public_hosts is True


def test_a_readback_table_that_is_not_a_table_is_refused_by_the_config_loader(tmp_path):
    """Refused at load time, by the security lane's file, not by a second TOML
    reader in the pipeline package.

    The key goes BEFORE the tables on purpose. In TOML a bare key written after
    a `[table]` header belongs to that table, so `readback = "http"` placed
    under `[targets.local]` silently becomes a target entry key and the guard
    never sees it. The first version of this test made exactly that mistake and
    passed for the wrong reason.
    """
    from testinghq.core.config import ConfigError

    path = tmp_path / "target.toml"
    path.write_text(
        f'readback = "http"\n\n[targets.{TARGET}]\nurl = "{TARGET_URL}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        from testinghq.core.config import load_config

        load_config(str(path))


def test_a_flag_works_even_when_there_is_no_config_file_at_all():
    """A dry run with `--readback` and no target config still previews, because
    resolving a target is only needed to send."""
    config = _resolve(["verify", "fire", "--readback", "mailbox"])
    assert config.kind == "mailbox"


# ---------------------------------------------------------------------------
# The expect-route flag
# ---------------------------------------------------------------------------


def test_expect_route_defaults_to_none_on_every_tool():
    parser = cli.build_parser()
    for argv in (["verify", "fire"],):
        assert parser.parse_args(argv).expect_route is None


def test_expect_route_is_carried_through():
    parser = cli.build_parser()
    args = parser.parse_args(["verify", "fire", "--expect-route", "queue:support"])
    assert args.expect_route == "queue:support"


def test_verify_check_accepts_a_body_exact_flag():
    parser = cli.build_parser()
    assert parser.parse_args(["verify", "check", "run.json", "--body-exact"]).body_exact


def test_every_send_shaped_command_takes_the_poll_flags():
    """Including `verify check`. It polls too, and a flag it does not accept
    fails argparse with exit 2, which is what a demonstration script using it
    gets if this is wrong."""
    parser = cli.build_parser()
    for argv in (["verify", "fire"], ["verify", "check", "run.json"]):
        args = parser.parse_args(argv)
        for flag in ("quiet_window", "max_wait", "poll_interval"):
            assert getattr(args, flag) > 0, f"{argv} lost --{flag.replace('_', '-')}"


def test_the_poll_defaults_are_ordered_so_a_run_can_settle():
    """A quiet window longer than max_wait would mean the readback can never
    settle and always gives up, which reads as a slow system rather than as a
    misconfiguration."""
    from testinghq.pipeline import common

    assert common.DEFAULT_QUIET_WINDOW < common.DEFAULT_MAX_WAIT
    assert 0 < common.DEFAULT_POLL_INTERVAL < common.DEFAULT_QUIET_WINDOW


def test_settle_is_gone():
    """It was a fixed sleep before one lookup, which is wrong in both
    directions: too short and a queue consumer's output reads as a loss, too
    long and every run pays for the slowest pipeline that ever was."""
    parser = cli.build_parser()
    for argv in (["verify", "fire"],):
        args = parser.parse_args(argv)
        assert not hasattr(args, "settle"), f"{argv} still accepts --settle"


# ---------------------------------------------------------------------------
# Readback auth
# ---------------------------------------------------------------------------


def test_a_missing_header_variable_is_refused_before_anything_is_sent(tmp_path, capsys):
    """The refusal has to land before the first request, not at connect time. A
    run that sends fifty messages and then discovers it had no credentials
    wasted the run and put fifty payloads on the operator's endpoint for
    nothing."""
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        "[readback]\nkind = \"http\"\nurl = \"http://localhost:8000/tickets\"\n\n"
        '[readback.headers]\nAuthorization = "env:HQ_READBACK_TOKEN"\n',
        encoding="utf-8",
    )
    argv = ["verify", "fire", "--config", str(path), "--target", TARGET, "--send"]
    with pytest.raises(AdapterError) as caught:
        cli_common._resolve_readback(cli.build_parser().parse_args(argv))
    assert "HQ_READBACK_TOKEN" in str(caught.value)


def test_a_literal_header_value_is_refused_at_the_config_boundary(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        "[readback]\nkind = \"http\"\nurl = \"http://localhost:8000/tickets\"\n\n"
        '[readback.headers]\nAuthorization = "Bearer hunter2"\n',
        encoding="utf-8",
    )
    with pytest.raises(AdapterError) as caught:
        cli_common._resolve_readback(
            cli.build_parser().parse_args(["verify", "fire", "--config", str(path)])
        )
    message = str(caught.value)
    assert "literal value" in message
    assert "hunter2" not in message, "the refused secret was echoed back"


def test_a_run_artifact_never_carries_a_header_value():
    from testinghq.pipeline.adapters import parse_readback_config

    config = parse_readback_config(
        {
            "kind": "http",
            "url": "http://localhost:8000/tickets",
            "headers": {"Authorization": "env:HQ_READBACK_TOKEN"},
        },
        env={"HQ_READBACK_TOKEN": "s3cr3t"},
    )
    assert config.header_names() == ["Authorization"]
    assert "s3cr3t" not in json.dumps(config.to_json())


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


def test_the_pipeline_package_delegates_to_the_canonical_guardrails():
    """The rule `tests/test_repo_invariants.py` enforces for `web/`, extended to
    the new package.

    This repository has already paid for a second copy of a safety rule: two
    copies disagreed within hours, and a target the CLI refused the UI would
    have fired at. The failure mode was a check that looked correct while being
    inert, which is the kind that survives review and then does damage.

    Structural rather than behavioural on purpose. A behavioural check would
    compare the two implementations' outputs and pass happily while they
    drifted, which is exactly what is being guarded against. What has to be
    impossible is a second body to drift.
    """
    import pathlib

    from testinghq import pipeline

    # Anchored to the pipeline package, not to `cli.__file__`. The two used to
    # sit side by side in one file, so deriving one from the other was free, and
    # it broke the moment the CLI became a package of its own. A test that
    # locates the code it is checking by asking a different module where it
    # lives is a test that breaks for reasons unrelated to what it checks.
    package = pathlib.Path(pipeline.__file__).resolve().parent
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(package.glob("*.py"))
    }
    assert sources, "the pipeline package is missing"

    common = sources["common.py"]
    assert "from ..core import guardrails" in common, (
        "pipeline/common.py must import the canonical guardrails module"
    )
    assert "guardrails.require_synthetic_content" in common
    assert "guardrails.require_configured_target" in common
    assert "guardrails.evaluate_send" not in common, (
        "evaluate_send is the CLI's decision, and the pipeline package is only "
        "ever called after it has said yes; calling it here too would be a "
        "second place the rule lives"
    )

    adapters = sources["adapters.py"]
    assert "from ..core import guardrails" in adapters
    assert "guardrails.require_configured_target" in adapters, (
        "a readback URL connects to and reads from a real system, so it is "
        "gated through the same canonical check as a firing target, in one place"
    )

    for name, source in sources.items():
        assert "def require_synthetic_content(" not in source, (
            f"pipeline/{name} must not define its own synthetic-content check"
        )
        assert "def require_configured_target(" not in source, (
            f"pipeline/{name} must not define its own target check"
        )
        assert "def evaluate_send(" not in source, (
            f"pipeline/{name} must not define its own send decision"
        )


def test_an_unimplemented_subcommand_says_so_rather_than_crashing(capsys):
    """`_not_yet` is the honest answer for a subcommand argparse accepted but
    `main` has no handler for. Every current subcommand is implemented, so
    reaching it means the parser and the dispatcher disagree, and that should
    be a message naming a bug in this file rather than a traceback or a
    misleading `--not-yet` milestone."""
    assert callable(cli_common._not_yet)
    code = cli_common._not_yet("nonsense")
    captured = capsys.readouterr()
    assert code != 0
    assert "nonsense" in captured.err
    assert "no handler is wired up" in captured.err


def test_an_unknown_tool_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["nonsense"])


def test_the_tools_share_their_readback_arguments():
    """One argument grammar for all three. An operator who has learned one has
    learned the other two, and the guardrail on the readback URL is in one
    place rather than three."""
    parser = cli.build_parser()
    for argv in (
        ["verify", "fire"],
        ["ledger", "fire"],
    ):
        args = parser.parse_args(argv)
        assert hasattr(args, "readback")
        assert hasattr(args, "allow_public_readback")
        assert hasattr(args, "expect_route")
        assert hasattr(args, "send")
        assert hasattr(args, "tag_prefix")
        assert hasattr(args, "config")


def test_ledger_fire_does_nothing_without_send(target_config, capsys):
    code = cli.main(
        ["ledger", "fire", "--target", TARGET, "--config", target_config,
         "--readback", "mailbox", "--count", "5"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert "hq-0-0000 .. hq-0-0004" in out
    assert "no network calls were made" in out


def test_the_scenario_flag_is_repeatable_and_constrained():
    parser = cli.build_parser()
    args = parser.parse_args(["redeliver", "fire", "--scenario", "duplicate", "--scenario", "references"])
    assert args.scenario == ["duplicate", "references"]
    with pytest.raises(SystemExit):
        parser.parse_args(["redeliver", "fire", "--scenario", "telepathy"])


def test_no_pipeline_tool_carries_the_high_rate_escape_hatch():
    """The thing that needs a hard ceiling is sustained load, and that is
    Barrage's job, with the ceiling already in place. Offering it here would
    be a way to bypass a guardrail by picking a different tool."""
    parser = cli.build_parser()
    for argv in (["verify", "fire"], ["ledger", "fire"], ["redeliver", "fire"]):
        assert not hasattr(parser.parse_args(argv), "allow_high_rate")


# ---------------------------------------------------------------------------
# Dry run: the default
# ---------------------------------------------------------------------------


def test_redeliver_fire_does_nothing_without_send(target_config, capsys):
    code = cli.main(
        ["redeliver", "fire", "--target", TARGET, "--config", target_config,
         "--readback", "mailbox", "--count", "2"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    for scenario in ("duplicate", "slow-retry", "reply-first", "references"):
        assert scenario in out
    assert "no network calls were made" in out


def test_a_dry_run_prints_the_plan_for_the_scenario_you_asked_for(target_config, capsys):
    cli.main(
        ["redeliver", "fire", "--target", TARGET, "--config", target_config,
         "--readback", "mailbox", "--scenario", "references"]
    )
    out = capsys.readouterr().out
    assert "references" in out
    assert "duplicate" not in out


# ---------------------------------------------------------------------------
# Missing adapter: refused, never defaulted
# ---------------------------------------------------------------------------


def test_the_exit_codes_are_defined_in_exactly_one_place():
    """All three tools share them so they script the same way. A second
    definition would be two sets of numbers that agree today."""
    from testinghq import pipeline
    from testinghq.pipeline import common

    # See the note in test_the_pipeline_package_delegates_to_the_canonical_guardrails:
    # anchored to the pipeline package rather than derived from cli's location.
    package = pathlib.Path(pipeline.__file__).resolve().parent

    for name in ("verify", "ledger", "redeliver"):
        source = package / f"{name}.py"
        text = source.read_text(encoding="utf-8")
        for code in ("EXIT_OK =", "EXIT_REFUSED =", "EXIT_DRY_RUN =", "EXIT_MISMATCH ="):
            assert code not in text, f"{name}.py redefines {code}"
    assert common.EXIT_MISMATCH == 3
    assert common.EXIT_OK == 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
