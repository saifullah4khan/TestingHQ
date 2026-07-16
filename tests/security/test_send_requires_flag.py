"""Any send path requires --send.

This is a read-only check against the existing CLI (owned by the engine
lane; not modified here). It exists in the security suite because it is
the one property the whole guardrail design hinges on: the parser default
for --send must be False, and the only place that flips will_send to True
is the explicit flag.
"""
from testinghq import cli
from testinghq.core import guardrails


def test_send_flag_defaults_to_false_in_the_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["blast", "fire", "--target", "local"])
    assert args.send is False


def test_fire_without_send_flag_is_a_dry_run(capsys):
    cli.main(["blast", "fire", "--target", "local"])
    out = capsys.readouterr().out
    assert "dry-run default" in out


def test_fire_with_send_flag_reports_explicit_send(capsys):
    cli.main(["blast", "fire", "--target", "local", "--send"])
    out = capsys.readouterr().out
    assert "explicit --send flag set" in out


def test_evaluate_send_requires_a_truthy_flag_to_enable_sending():
    for falsy in (False, 0, None, "", []):
        assert guardrails.evaluate_send(falsy).will_send is False
    assert guardrails.evaluate_send(True).will_send is True
