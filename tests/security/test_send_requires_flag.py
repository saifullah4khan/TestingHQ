"""Any send path requires --send.

This is a read-only check against the existing CLI (owned by the engine
lane; not modified here). It exists in the security suite because it is
the one property the whole guardrail design hinges on: the parser default
for --send must be False, and the only place that flips will_send to True
is the explicit flag.

A note on what used to be here. There was a test asserting that
`blast fire --target local --send` prints "explicit --send flag set". It
passed, but it passed for the wrong reason: no `target.toml` existed, so
the command was going to be refused for an unresolvable target, and it used
to print its announcement *before* the refusal. The test was green on a
command that never sent anything and never reached the branch it claimed to
cover.

Reordering the CLI to validate before announcing fixed that, and the test
correctly went red. A refused send must now announce nothing, and that is
what is asserted below.

The positive case, that a send which *does* proceed reports the explicit
flag, cannot be asserted here: the send itself needs a real socket, and this
suite is hermetic by design. It is asserted in `tests/test_cli.py`, which
reaches the announcement and lets the suite-wide network block stop the send
before it leaves the machine.
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


def test_a_send_that_is_refused_announces_nothing(capsys):
    """A command that will not send must not say it is sending.

    This is the security-relevant form of the ordering rule. The previous
    version of this file asserted the opposite, by accident: the announcement
    was visible precisely because it was printed before the refusal."""
    rc = cli.main(["blast", "fire", "--target", "no-such-target", "--send"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "refused" in captured.err
    assert "explicit --send flag set" not in captured.out + captured.err, (
        "a refused send announced itself as a send"
    )


def test_evaluate_send_requires_a_truthy_flag_to_enable_sending():
    for falsy in (False, 0, None, "", []):
        assert guardrails.evaluate_send(falsy).will_send is False
    assert guardrails.evaluate_send(True).will_send is True
