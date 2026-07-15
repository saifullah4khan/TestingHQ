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


def test_fire_with_send_flag_reports_send(capsys):
    cli.main(["blast", "fire", "--target", "local", "--send"])
    captured = capsys.readouterr()
    assert "explicit --send" in captured.out
