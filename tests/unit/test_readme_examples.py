"""Run every `testinghq` command in the docs, as a dry run.

A documented command that exits 1 tells the first person who copies it that
they are being refused. So every `testinghq ...` command in the docs, whether
on its own line in a code block or inline in backticks, is run here with
`--send` removed, output paths pointed at a temporary directory, real artifacts
supplied for the commands that read one, and a mail-sink readback supplied for
the pipeline tools, which refuse to run without one by design. Nothing is
skipped: every command found is either run or the test fails.

`--send` is removed by filtering the argument list, and the suite-wide network
block in tests/conftest.py is the second layer.
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
DOC_FILES = (
    "README.md",
    "docs/README.md",
    "docs/DOGFOOD.md",
    "docs/CONFIG.md",
    "examples/README.md",
)

#: A line in a code block that starts with the CLI, and an inline code span
#: that does.
COMMAND_LINE = re.compile(r"^\s*(testinghq\s+[^`]*\S)\s*$")
INLINE_COMMAND = re.compile(r"`(testinghq\s+[^`]+)`")

#: Arguments that name a file the command writes.
WRITE_FLAGS = ("--out", "--html")

#: A bare file name of a JSON artifact.
ARTIFACT_ARG = re.compile(r"^[^\s/\\]+\.json$")

#: Tools that cannot run without a readback adapter.
PIPELINE_TOOLS = frozenset({"verify", "ledger", "redeliver", "loop", "steady"})
KNOWN_TOOLS = PIPELINE_TOOLS | {"blast", "barrage", "sigcheck", "compare", "report", "config"}

#: Declared here rather than imported, so a change to the convention shows up
#: as a failure here rather than being tracked silently.
EXIT_OK = 0
EXIT_DRY_RUN = 2


def _doc_commands() -> list:
    """(file, line number, command) for every `testinghq ...` in the docs."""
    found = []
    for name in DOC_FILES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            inline = INLINE_COMMAND.findall(line)
            if inline:
                found += [(name, line_no, command.strip()) for command in inline]
                continue
            match = COMMAND_LINE.match(line)
            if match:
                found.append((name, line_no, match.group(1)))
    return found


DOC_COMMANDS = _doc_commands()


def _blast_artifact(seed: int = 1, count: int = 4) -> dict:
    from testinghq.blast.corrupt import DEFAULT_MIX, corrupt_corpus
    from testinghq.blast.generate import generate_corpus
    from testinghq.core import report

    corrupted = corrupt_corpus(generate_corpus(seed, count), seed, DEFAULT_MIX)
    records = [
        report.build_record(
            email, report.category_label(recipe), seed, index,
            {"status": 200, "latency_ms": 1.0, "body_snippet": "ok"},
        )
        for index, (email, recipe) in enumerate(corrupted)
    ]
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


#: A loopback target and a mail-sink readback. Nothing connects to the target:
#: `--send` is removed, so the sink is only ever read.
READBACK_CONFIG = """[targets.local]
url = "http://127.0.0.1:9/intake"

[readback]
kind = "mailbox"
path = "{sink}"
"""


def _sink_and_artifact(tmp_path: Path, seed: int = 1, count: int = 4):
    """A mail sink holding exactly what a correct pipeline would hold for a
    verify run, and that run's artifact, so `verify check` reads a clean run."""
    from testinghq.core import report
    from testinghq.pipeline.verify import build_tagged_corpus

    sink = tmp_path / "mail-sink.jsonl"
    lines, records = [], []
    for email, tag, record_id in build_tagged_corpus(seed, count):
        lines.append(json.dumps({
            "id": report.payload_sha256(email)[:12],
            "tag": tag,
            "from": email.from_addr,
            "subject": email.subject,
            "body": email.text,
            "attachments": [a.filename for a in email.attachments],
            "route": email.envelope.to[0],
            "message_id": email.headers.get("Message-ID", "").strip("<>"),
        }))
        records.append({
            "id": record_id,
            "category": "clean",
            "tag": tag,
            "payload_sha256": report.payload_sha256(email),
            "response": {"status": 200, "latency_ms": 1.0, "body_snippet": "ok"},
            "assertion": {"passed": True, "mismatches": []},
        })
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


def _artifact_for(argv: list, tmp_path: Path) -> Path:
    """A real artifact of the kind the command reads."""
    if argv[0] == "barrage":
        name, payload = "barrage_artifact.json", _barrage_artifact()
    elif argv[0] == "verify":
        name, payload = "verify_artifact.json", _sink_and_artifact(tmp_path)[1]
    else:
        name, payload = "blast_artifact.json", _blast_artifact()
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _prepare(argv: list, tmp_path: Path) -> list:
    """Drop the program name and --send, redirect written files, and supply
    the artifacts, config and readback the command needs to be runnable."""
    argv = list(argv[1:] if argv and argv[0] == "testinghq" else argv)
    reads_artifact = (
        argv[0] in ("report", "compare") or "replay" in argv or "check" in argv
    )
    out: list = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--send" or arg.startswith("--send="):
            index += 1
            continue
        if arg in WRITE_FLAGS:
            out += [arg, str(tmp_path / f"written-{arg.lstrip('-')}")]
            index += 2
            continue
        if reads_artifact and ARTIFACT_ARG.match(arg):
            out.append(str(_artifact_for(argv, tmp_path)))
            index += 1
            continue
        out.append(arg)
        index += 1

    if argv[0] == "config" and "validate" in argv:
        config_path = tmp_path / "target.toml"
        config_path.write_text(
            '[targets.local]\nurl = "http://127.0.0.1:8000/intake"\n', encoding="utf-8"
        )
        out = [a for a in out if not a.endswith(".toml")] + [str(config_path)]

    # `--config` is appended last so it wins over any the example carried. The
    # readback is told not to wait: a documented command is run to prove it is
    # not refused, not to produce a verdict.
    if argv[0] in PIPELINE_TOOLS and "--readback" not in out:
        sink, _artifact = _sink_and_artifact(tmp_path)
        config = tmp_path / "readback_target.toml"
        config.write_text(READBACK_CONFIG.format(sink=sink.as_posix()), encoding="utf-8")
        out += [
            "--readback", "mailbox",
            "--config", str(config),
            "--quiet-window", "0",
            "--poll-interval", "0.05",
        ]
    return out


def test_the_docs_contain_commands_this_harness_can_run():
    """If the extractor stops matching, this fails rather than the checks
    below passing vacuously; a tool the harness cannot supply fails too."""
    assert len(DOC_COMMANDS) >= 12, (
        f"only found {len(DOC_COMMANDS)} `testinghq` commands in {list(DOC_FILES)}; "
        "the extractor has probably stopped matching"
    )
    tools = {command.split()[1] for _f, _l, command in DOC_COMMANDS}
    assert tools <= KNOWN_TOOLS, (
        f"a documented command names a tool this harness cannot run: "
        f"{sorted(tools - KNOWN_TOOLS)}. Teach _prepare what it needs."
    )


@pytest.mark.parametrize(
    "name,line_no,command",
    DOC_COMMANDS,
    ids=[f"{f}:{n}:{c.split()[1]}" for f, n, c in DOC_COMMANDS],
)
def test_every_documented_command_runs_as_a_dry_run(name, line_no, command, tmp_path):
    """Not refused, and with --send removed, a sending command is a dry run."""
    argv = _prepare(shlex.split(command), tmp_path)
    assert "--send" not in argv

    result = subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )

    expected = EXIT_DRY_RUN if ("fire" in argv or "replay" in argv) else EXIT_OK
    assert result.returncode == expected, (
        f"{name}:{line_no}: the documented command exits {result.returncode}, "
        f"expected {expected}.\n"
        f"  command: {command}\n"
        f"  run as:  {' '.join(argv)}\n"
        f"  stdout:  {result.stdout.strip()[-800:]}\n"
        f"  stderr:  {result.stderr.strip()[-800:]}"
    )


def test_the_examples_never_transmit(tmp_path):
    """No prepared command keeps --send or names a network destination."""
    for _f, _l, command in DOC_COMMANDS:
        argv = _prepare(shlex.split(command), tmp_path)
        assert "--send" not in argv
        assert not any(a.startswith(("http://", "https://")) for a in argv), command
