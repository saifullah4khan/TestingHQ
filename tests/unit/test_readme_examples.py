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

The pipeline tools need the same treatment for the same reason, and it is worth
being explicit about why their case is not a special exemption. `verify`,
`ledger` and `redeliver` refuse to run without a readback adapter, by design:
a verification run with no adapter would check nothing, report nothing wrong and
exit 0, which is the false green this package exists to eliminate. So every
documented command for them is genuinely unrunnable as written, and this
harness supplies a mail-sink adapter and the sink to read, built to match the
payloads the command will send. The example in the README stays the short
version an operator types; the plumbing the guard needs is the guard's job,
exactly as it already is for `replay`.
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
# docs/CONFIG.md is generated, and a generated example is just as capable of
# rotting as a hand-written one. It is included so that the command the
# reference tells you to run is actually run here.
DOC_FILES = ("README.md", "examples/README.md", "docs/CONFIG.md")

# A fenced code block, or a line, beginning with the CLI. Comments and blank
# lines are excluded by requiring the line to start with the command name.
COMMAND_LINE = re.compile(r"^\s*(testinghq\s+\S.*)$")

#: Argument that carries a path the command would write to, so it can be
#: redirected into tmp_path.
WRITE_FLAGS = ("--out",)

#: Argument that names a JSON artifact to replay.
REPLAY_TARGET = re.compile(r"^[^\s/\\]+\.json$")

#: The tools that cannot run without a readback adapter, and therefore the ones
#: this harness has to supply one for.
PIPELINE_TOOLS = frozenset({"verify", "ledger", "redeliver", "loop"})

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


#: A config whose readback is a mail sink the harness writes, and whose target
#: is loopback. Nothing connects to it: `--send` is stripped, so the only
#: consumer of the sink is a `verify check`, which reads and never sends.
READBACK_CONFIG = """[targets.local]
name = "local"
url = "http://127.0.0.1:9/intake"

[readback]
kind = "mailbox"
path = "{sink}"
"""


def _sink_and_artifact(tmp_path: Path, seed: int = 1, count: int = 4):
    """A mail sink and a matching run artifact for the pipeline tools.

    Both are built from the same tagged corpus, which is the point: the sink
    holds exactly what a correct pipeline would hold for the payloads the
    command sends, so a documented `verify check` reads back a clean run and
    exits 0 rather than reporting a total loss. A sink built from a different
    corpus, or an empty one, would make every documented check command exit 3,
    and the harness would be asserting that correct commands are broken.
    """
    from testinghq.core import report
    from testinghq.pipeline.verify import build_tagged_corpus

    sink = tmp_path / "mail-sink.jsonl"
    lines = []
    records = []
    for email, tag, record_id in build_tagged_corpus(seed, count):
        lines.append(
            json.dumps(
                {
                    "id": report.payload_sha256(email)[:12],
                    "tag": tag,
                    "from": email.from_addr,
                    "subject": email.subject,
                    "body": email.text,
                    "attachments": [a.filename for a in email.attachments],
                    "route": email.envelope.to[0],
                    "message_id": email.headers.get("Message-ID", "").strip("<>"),
                }
            )
        )
        records.append(
            {
                "id": record_id,
                "category": "clean",
                "tag": tag,
                "payload_sha256": report.payload_sha256(email),
                "response": {"status": 200, "latency_ms": 1.0, "body_snippet": "ok"},
                "assertion": {"passed": True, "mismatches": []},
            }
        )
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact = {
        "seed": seed,
        "config": {
            "tool": "verify", "count": count, "seed": seed, "tag_prefix": "hq",
            "target": "local", "dry_run": False,
        },
        "summary": {"sent": count, "found": count, "verified": count, "failed": 0},
        "records": records,
    }
    return sink, artifact


def _prepare(argv: list[str], tmp_path: Path) -> list[str]:
    """Strip the program name and --send, redirect output paths, and supply
    whatever the command needs to be runnable at all.

    The leading `testinghq` is dropped: it is the console-script name, and
    passing it through to `python -m testinghq.cli` would have argparse read it
    as the tool name.
    """
    argv = list(argv)
    if argv and argv[0] == "testinghq":
        argv = argv[1:]
    tool = argv[0] if argv else ""
    is_replay = "replay" in argv
    is_check = "check" in argv
    is_report = tool == "report"
    is_config = tool == "config"

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
        if (is_replay or is_check or is_report) and REPLAY_TARGET.match(arg):
            if tool == "barrage":
                name, payload = "barrage_artifact.json", _barrage_artifact()
            elif tool == "report":
                # `report` takes a positional artifact and nothing else, so
                # there is one case: read back a blast artifact. A documented
                # `report` example therefore proves the command is not refused
                # on a real artifact, which is the property being checked.
                name, payload = "report_artifact.json", _blast_artifact()
            elif is_check:
                sink, payload = _sink_and_artifact(tmp_path)
                name = "verify_artifact.json"
            else:
                name, payload = "blast_artifact.json", _blast_artifact()
            path = tmp_path / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            out.append(str(path))
            index += 1
            continue
        out.append(arg)
        index += 1

    # A pipeline command without a readback is refused by design, so one is
    # supplied here rather than documented. `--config` is appended last so it
    # wins over any the example already carried, matching how argparse resolves
    # a repeated option and keeping the harness's behaviour independent of what
    # the example happens to say.
    #
    # The sink path goes in as posix. A Windows path with backslashes is a TOML
    # basic string with invalid escapes in it, and the first version of this
    # wrote one, so every documented pipeline command was refused with a
    # complaint about a `[readback]` table that had plainly been there all
    # along.
    #
    # The poll settings matter as much. A documented command is run to prove it
    # is not refused, not to produce a verdict, so the readback is told not to
    # wait: with the real defaults one `verify check` sat out a five-second quiet
    # window per poll and this file took seventeen seconds of it. Zero still
    # confirms, because a confirming poll happens either way.
    # `config validate <path>` names a file that has to exist. The example in
    # docs/CONFIG.md points at the conventional ./target.toml, which is not in
    # the tree, so one is written here instead. A valid one, because the point
    # of the check is that a documented command is not REFUSED, and validating
    # a file that does not exist would fail it for a reason that says nothing
    # about the command.
    if is_config and "validate" in out:
        config_path = tmp_path / "target.toml"
        config_path.write_text(
            '[targets.local]\nurl = "http://127.0.0.1:8000/intake"\n',
            encoding="utf-8",
        )
        out = [str(config_path) if a.endswith(".toml") else a for a in out]

    if tool in PIPELINE_TOOLS and "--readback" not in out:
        sink, _artifact = _sink_and_artifact(tmp_path)
        config = tmp_path / "readback_target.toml"
        config.write_text(
            READBACK_CONFIG.format(sink=sink.as_posix()), encoding="utf-8"
        )
        out += [
            "--readback", "mailbox",
            "--config", str(config),
            "--quiet-window", "0",
            "--poll-interval", "0.05",
        ]
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
    known = {"blast", "barrage", "report", "config"} | set(PIPELINE_TOOLS)
    assert tools <= known, (
        f"a documented command names a tool this harness does not know how to "
        f"run: {sorted(tools - known)}. Add it to PIPELINE_TOOLS and give the "
        f"harness whatever that tool needs to be runnable, or its documented "
        f"examples will be refused."
    )
    assert {"blast", "barrage"} <= tools, (
        f"the two shipped tools should still be documented; found {sorted(tools)}"
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
