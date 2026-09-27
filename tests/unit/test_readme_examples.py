"""Run every `testinghq` command in the docs, as a dry run.

A README example that exits non-zero is worse than no example, because it is
the first thing a new user runs. The review that prompted this found one:

    testinghq barrage fire --target local --rate 20 --duration 60 --concurrency 8 --send

which exits 1, because open mode is the default and `--concurrency` is refused
there. The command had been in the README since before that refusal existed, and
nothing noticed, because nothing ran it.

So: extract every `testinghq ...` command line from the docs, strip `--send` so
nothing can be transmitted, point any output path at a temporary directory,
supply a real artifact for the `replay` commands, run it, and require exit 0.

What this deliberately does not do is skip anything. A quarantine list of
"commands we cannot check" is how a docs guard quietly stops being one, and this
repo has a non-negotiable against those. Every command found is either run or
the test fails.

Two details that matter for it to actually work:

- `--send` is stripped by filtering the argument list, not by string
  replacement, so a command that spells it `--send=true` or repeats it is still
  caught. The suite-wide network block in tests/conftest.py is the second layer,
  and since it is un-swallowable it would fail the test rather than record a
  timeout.
- `replay` needs a real artifact on disk, so the test builds one of the right
  shape rather than skipping the command.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = ("README.md", "examples/README.md")

# A fenced code block, or a line, beginning with the CLI. Comments and blank
# lines are excluded by requiring the line to start with the command name.
COMMAND_LINE = re.compile(r"^\s*(testinghq\s+\S.*)$")

#: Argument that carries a path the command would write to, so it can be
#: redirected into tmp_path.
WRITE_FLAGS = ("--out",)

#: Argument that names a JSON artifact to replay.
REPLAY_TARGET = re.compile(r"^[^\s/\\]+\.json$")

#: The CLI's own convention, identical across both tools: 0 ran, 1 refused,
#: 2 dry-run with nothing sent. Declared here rather than imported so this
#: test does not silently track a change to the convention.
EXIT_OK = 0
EXIT_DRY_RUN = 2
ACCEPTED_EXITS = (EXIT_OK, EXIT_DRY_RUN)


def _doc_commands() -> list[tuple[str, int, str]]:
    """(file, line number, command) for every `testinghq ...` line in the docs."""
    found = []
    for name in DOC_FILES:
        path = REPO_ROOT / name
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = COMMAND_LINE.match(line)
            if match:
                found.append((name, line_no, match.group(1).strip()))
    return found


DOC_COMMANDS = _doc_commands()


def _blast_artifact(seed: int = 1, count: int = 4) -> dict:
    from testinghq.blast.corrupt import DEFAULT_MIX, corrupt_corpus
    from testinghq.blast.generate import generate_corpus
    from testinghq.core import report

    corrupted = corrupt_corpus(generate_corpus(seed, count), seed, DEFAULT_MIX)
    records = []
    for index, (email, recipe) in enumerate(corrupted):
        record = report.build_record(
            email,
            report.category_label(recipe),
            seed,
            index,
            {"status": 200, "latency_ms": 1.0, "body_snippet": "ok"},
        )
        records.append(record)
    config = {
        "mix": list(report.CATEGORIES), "count": count, "seed": seed,
        "dry_run": False, "target": "local",
    }
    return {"seed": seed, "config": config, "records": records,
            "summary": report.compute_summary(records, seed, config)}


def _barrage_artifact() -> dict:
    return {
        "seed": 1,
        "config": {
            "mode": "open", "rate": 10.0, "duration": 20.0, "warmup": 5.0,
            "concurrency": 1, "pool_size": 5, "target": "local", "dry_run": False,
        },
        "summary": {
            "throughput": {"targeted_rps": 10.0, "achieved_rps": 10.0,
                           "total_requests": 100},
            "latency_ms": {"p50": 1.0, "p90": 1.0, "p99": 1.0},
            "error_rate": 0.0,
            "knee": None,
        },
        "buckets": [],
    }


def _prepare(argv: list[str], tmp_path: Path) -> list[str]:
    """Strip the program name and --send, redirect output paths, and supply a
    replay artifact.

    The leading `testinghq` is dropped: it is the console-script name, and
    passing it through to `python -m testinghq.cli` would have argparse read it
    as the tool name.
    """
    argv = list(argv)
    if argv and argv[0] == "testinghq":
        argv = argv[1:]
    tool = argv[0] if argv else ""
    is_replay = "replay" in argv

    out: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--send":
            index += 1
            continue
        if arg in WRITE_FLAGS:
            out += [arg, str(tmp_path / (arg.lstrip("-") or "out.json"))]
            index += 2
            continue
        if is_replay and REPLAY_TARGET.match(arg):
            name = "barrage_artifact.json" if tool == "barrage" else "blast_artifact.json"
            payload = _barrage_artifact() if tool == "barrage" else _blast_artifact()
            path = tmp_path / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            out.append(str(path))
            index += 1
            continue
        out.append(arg)
        index += 1
    return out


def test_the_docs_actually_contain_commands_to_check():
    """A guard that finds nothing because the pattern stopped matching is
    indistinguishable from one that is passing. If a doc is restructured and the
    extractor no longer sees anything, this fails rather than the checks below
    passing vacuously."""
    assert len(DOC_COMMANDS) >= 12, (
        f"only found {len(DOC_COMMANDS)} `testinghq` commands in "
        f"{list(DOC_FILES)}; the extractor has probably stopped matching"
    )
    tools = {command.split()[1] for _f, _l, command in DOC_COMMANDS}
    assert tools == {"blast", "barrage"}, (
        f"expected commands for both tools, found {sorted(tools)}"
    )


@pytest.mark.parametrize(
    "name,line_no,command",
    DOC_COMMANDS,
    ids=[f"{f}:{n}" for f, n, _c in DOC_COMMANDS],
)
def test_every_documented_command_runs_as_a_dry_run(name, line_no, command, tmp_path):
    """The command must not be REFUSED.

    On the accepted exit codes: the review that prompted this asked for exit 0,
    and asserting that would have failed on commands that are working
    perfectly. Both tools use the same documented convention, 0 ran, 1 refused,
    2 dry-run with nothing sent, and `--send` is stripped here, so every
    send-shaped command correctly exits 2. Asserting 0 would have meant either
    failing correct commands or leaving `--send` on and actually transmitting,
    so the assertion is against the refused code instead. That is the actual
    requirement: a documented example must not tell a user they are being
    denied.

    `generate` writes a corpus and is not send-shaped, so it exits 0, and both
    codes are accepted.
    """
    argv = _prepare(shlex.split(command), tmp_path)
    assert "--send" not in argv, (
        f"{name}:{line_no}: the dry-run harness failed to strip --send from "
        f"{command!r}; this test would risk transmitting"
    )

    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )

    assert result.returncode in ACCEPTED_EXITS, (
        f"{name}:{line_no}: the documented command exits {result.returncode}, "
        f"so anyone copying it gets a refusal.\n"
        f"  command: {command}\n"
        f"  run as:  {' '.join(argv)}\n"
        f"  stdout:  {result.stdout.strip()[:500]}\n"
        f"  stderr:  {result.stderr.strip()[:500]}"
    )
    # Being specific about which code, so a future change to the convention
    # shows up here rather than as a silently loosened assertion.
    if "fire" in argv or "replay" in argv:
        assert result.returncode == EXIT_DRY_RUN, (
            f"{name}:{line_no}: with --send stripped this command should be a "
            f"dry run and exit {EXIT_DRY_RUN}, got {result.returncode}"
        )


def test_the_examples_never_transmit(tmp_path):
    """Belt and braces. `_prepare` strips `--send` and the suite-wide network
    block is the second layer, but this asserts the harness's own claim rather
    than trusting that it kept doing its job."""
    for _f, _l, command in DOC_COMMANDS:
        argv = _prepare(shlex.split(command), tmp_path)
        assert "--send" not in argv
        assert not any(
            a.startswith(("http://", "https://")) for a in argv
        ), f"{command!r} still names a network destination after preparation"
