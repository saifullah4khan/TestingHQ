from testinghq import cli


def test_parser_builds_with_blast_subcommands():
    parser = cli.build_parser()
    args = parser.parse_args(["blast", "generate", "--count", "5", "--seed", "1"])
    assert args.tool == "blast"
    assert args.command == "generate"
    assert args.count == 5
    assert args.seed == 1


def test_fire_defaults_to_dry_run(capsys):
    rc = cli.main(["blast", "fire", "--target", "local"])
    captured = capsys.readouterr()
    assert "dry-run default" in captured.out
    assert rc == 2


def test_fire_with_send_flag_reports_send(capsys, tmp_path):
    """A send that actually proceeds reports the explicit flag.

    Needs a resolvable target, which is why it takes a tmp_path. This test
    previously passed with no target configured at all, because the CLI
    announced itself before refusing. It was green on a command that sent
    nothing.

    It still cannot run here without a real socket, so the send itself is
    stopped at the transport by the suite-wide network block and what is
    asserted is the announcement. The positive case end to end is covered in
    tests/e2e/test_real_sockets.py."""
    config = tmp_path / "target.toml"
    config.write_text(
        '[targets.local]\nname = "local"\nurl = "http://127.0.0.1:9/inbound"\n',
        encoding="utf-8",
    )

    try:
        cli.main([
            "blast", "fire", "--target", "local", "--send",
            "--count", "1", "--config", str(config),
        ])
    except BaseException as exc:  # noqa: BLE001
        # The network block stops the send, which is the point of it.
        assert "network" in str(exc).lower() or "socket" in str(exc).lower(), exc

    captured = capsys.readouterr()
    assert "explicit --send" in captured.out
