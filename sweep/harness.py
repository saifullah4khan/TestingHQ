"""Helpers for sweep/run.sh: the parts that are awkward or unportable in bash.

    python sweep/harness.py summary STEPS.tsv --out summary.json ...
    python sweep/harness.py relativize RUN_DIR
    python sweep/harness.py wait-port 127.0.0.1 8025 --timeout 30
    python sweep/harness.py http DELETE http://127.0.0.1:8025/api/v1/messages
    python sweep/harness.py sha256 FILE
    python sweep/harness.py extract ARCHIVE DEST

Standard library only, and the same on Linux, macOS and Git Bash on Windows,
which is why run.sh hashes, unpacks, waits and writes JSON through here rather
than through sha256sum, unzip, nc or a printf of JSON.

The run's verdict is decided in `classify`. Every testinghq tool exits with
the shared convention (testinghq/core/exit_codes.py): 0 ran and the answer was
yes, 3 ran and the answer was no, 1 declined, 2 ran without sending. A sweep
step that exits 0 or 3 completed, and a 3 is a finding to read in its report.
Anything else is an error: 1 means the step was refused and sent nothing, and 2
cannot be a completed sweep step because every step that sends passes --send,
so it would mean the step sent nothing it was asked to send. An exit code
outside the convention (a crash, a missing command) is an error too.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

#: Exit codes of a step that ran to completion. Mirrors EXIT_OK and
#: EXIT_FINDING in testinghq/core/exit_codes.py.
COMPLETED_CODES = (0, 3)
FINDING_CODE = 3

#: What each convention code means, for the summary. Mirrors MEANINGS in
#: testinghq/core/exit_codes.py, with the sweep's reading of 2 added.
MEANINGS = {
    0: "ran, answer was yes",
    1: "declined: guardrail, bad config, or bad usage",
    2: "ran, sent nothing although --send was passed",
    3: "ran, answer was no",
}

#: The steps that run a testinghq command. Every other step (docker compose,
#: the setup script, a clone or a download) completed only when it exited 0.
TESTINGHQ_TOOLS = frozenset({
    "barrage", "blast", "compare", "config", "ledger", "loop", "redeliver",
    "report", "sigcheck", "steady", "verify",
})

RESULT_PASSED = "passed"
RESULT_FINDINGS = "findings"
RESULT_ERROR = "error"


# ---------------------------------------------------------------------------
# Step records and the summary
# ---------------------------------------------------------------------------


#: The columns of the step log run.sh appends to, one tab-separated line per
#: step. Written by bash, so no step costs a Python start-up; the command is
#: last because it is the one value that may hold anything but a tab.
STEP_COLUMNS = ("phase", "step", "tool", "exit_code", "artifact", "html", "log",
                "skipped", "command")


def _optional_int(value: str) -> Optional[int]:
    return int(value) if value.strip() else None


def read_steps(path: Path) -> List[Dict[str, object]]:
    """The step records in run order, with each report record folded into the
    step whose artifact it rendered."""
    steps: List[Dict[str, object]] = []
    by_name: Dict[str, Dict[str, object]] = {}
    if not path.exists():
        return steps
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        values = line.split("\t", len(STEP_COLUMNS) - 1)
        values += [""] * (len(STEP_COLUMNS) - len(values))
        entry = dict(zip(STEP_COLUMNS, values))
        code = _optional_int(entry["exit_code"])
        if entry["phase"] == "report":
            target = by_name.get(entry["step"])
            if target is not None:
                target["html"] = entry["html"] or None
                target["report_exit_code"] = code
            continue
        step: Dict[str, object] = {
            "step": entry["step"],
            "tool": entry["tool"] or None,
            "command": entry["command"] or None,
            "exit_code": code,
            "meaning": None,
            "artifact": entry["artifact"] or None,
            "html": None,
            "report_exit_code": None,
            "log": entry["log"] or None,
            "skipped": entry["skipped"] or None,
        }
        if code is not None and step["tool"] in TESTINGHQ_TOOLS:
            step["meaning"] = MEANINGS.get(code, "outside the exit code convention")
        elif code is not None:
            step["meaning"] = "completed" if code == 0 else "failed"
        steps.append(step)
        by_name[entry["step"]] = step
    return steps


def step_failed(step: Dict[str, object]) -> bool:
    """True when the step, or the report rendering its artifact, errored."""
    code = step.get("exit_code")
    if code is None:
        # Skipped. A step skipped because an earlier one failed is an error;
        # one skipped by a profile switch (SWEEP_CORPUS=0) is not.
        return bool(step.get("skipped")) and str(step["skipped"]).startswith("blocked:")
    completed = COMPLETED_CODES if step.get("tool") in TESTINGHQ_TOOLS else (0,)
    if code not in completed:
        return True
    report = step.get("report_exit_code")
    return report is not None and report != 0


def classify(steps: Iterable[Dict[str, object]]) -> str:
    """passed, findings or error, from every step's exit code."""
    steps = list(steps)
    if any(step_failed(step) for step in steps):
        return RESULT_ERROR
    if any(step.get("exit_code") == FINDING_CODE for step in steps):
        return RESULT_FINDINGS
    return RESULT_PASSED


def build_summary(
    steps: List[Dict[str, object]],
    *,
    helpdesk: str,
    started: str,
    finished: str,
    version: str,
    config: Optional[str],
    target: Optional[str],
    settings: Dict[str, str],
    notes: Optional[List[str]] = None,
) -> Dict[str, object]:
    result = classify(steps)
    return {
        "schema_version": SCHEMA_VERSION,
        "helpdesk": helpdesk,
        "started": started,
        "finished": finished,
        "testinghq_version": version,
        "config": config,
        "target": target,
        "settings": dict(sorted(settings.items())),
        "completed_exit_codes": list(COMPLETED_CODES),
        "notes": list(notes or []),
        "steps": steps,
        "result": result,
        "exit_code": 1 if result == RESULT_ERROR else 0,
    }


def print_table(summary: Dict[str, object], out=None) -> None:
    out = out or sys.stdout
    print(f"\n{summary['helpdesk']}: {summary['result']}", file=out)
    for step in summary["steps"]:  # type: ignore[union-attr]
        code = step["exit_code"]
        shown = "skip" if code is None else str(code)
        note = step["skipped"] or step["meaning"] or ""
        report = step.get("report_exit_code")
        if report not in (None, 0):
            note = f"{note}; report exited {report}"
        print(f"  {step['step']:<16} {shown:>4}  {note}", file=out)


# ---------------------------------------------------------------------------
# Artifacts without machine-specific paths
# ---------------------------------------------------------------------------


def _relative(value, prefixes):
    if isinstance(value, str):
        for prefix in prefixes:
            if value.startswith(prefix):
                return value[len(prefix):].replace("\\", "/")
        return value
    if isinstance(value, list):
        return [_relative(item, prefixes) for item in value]
    if isinstance(value, dict):
        return {key: _relative(item, prefixes) for key, item in value.items()}
    return value


def relativize_artifacts(run_dir: Path, root: Path) -> List[str]:
    """Rewrite every string in the run's JSON artifacts that starts with the
    repository root as a path relative to it, so an artifact can be published
    without the machine's directory layout. A tool records the readback paths
    it resolved, which are absolute. Returns the files changed."""
    root = root.resolve()
    prefixes = sorted({str(root) + "/", str(root) + "\\", root.as_posix() + "/"},
                      key=len, reverse=True)
    changed = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rewritten = _relative(data, prefixes)
        if rewritten != data:
            path.write_text(json.dumps(rewritten, indent=2), encoding="utf-8")
            changed.append(path.name)
    return changed


# ---------------------------------------------------------------------------
# Portable shell replacements
# ---------------------------------------------------------------------------


# The imports below are inside the functions on purpose: run.sh starts this
# script several times a run, and the summary needs none of them.


def wait_port(host: str, port: int, timeout: float) -> bool:
    import socket
    import time

    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def http_request(method: str, url: str, timeout: float = 10.0) -> int:
    """The status of one request to a loopback URL, 0 when nothing answered."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return 0


def sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, dest: Path) -> None:
    """Unpack a .zip or .tar.gz into dest, refusing members that escape it."""
    import tarfile
    import zipfile

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise SystemExit(f"refusing archive member outside {dest}: {name}")
            bundle.extractall(dest)
        return
    with tarfile.open(archive, "r:*") as bundle:
        members = bundle.getmembers()
        for member in members:
            if not (root / member.name).resolve().is_relative_to(root):
                raise SystemExit(f"refusing archive member outside {dest}: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise SystemExit(f"refusing non-regular archive member: {member.name}")
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        bundle.extractall(dest, members=members, **safe)
    for member in members:
        if member.isfile() and member.mode & 0o111:
            (dest / member.name).chmod(0o755)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parse_settings(pairs: List[str]) -> Dict[str, str]:
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if sep:
            settings[key] = value
    return settings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.py")
    sub = parser.add_subparsers(dest="command", required=True)

    summ = sub.add_parser("summary")
    summ.add_argument("steps", type=Path)
    summ.add_argument("--out", type=Path, required=True)
    summ.add_argument("--helpdesk", required=True)
    summ.add_argument("--started", required=True)
    summ.add_argument("--finished", required=True)
    summ.add_argument("--version", default="")
    summ.add_argument("--config")
    summ.add_argument("--target")
    summ.add_argument("--setting", action="append", default=[])
    summ.add_argument("--note", action="append", default=[])

    wait = sub.add_parser("wait-port")
    wait.add_argument("host")
    wait.add_argument("port", type=int)
    wait.add_argument("--timeout", type=float, default=30.0)

    http = sub.add_parser("http")
    http.add_argument("method")
    http.add_argument("url")

    sha = sub.add_parser("sha256")
    sha.add_argument("file", type=Path)

    rel = sub.add_parser("relativize")
    rel.add_argument("run_dir", type=Path)

    ext = sub.add_parser("extract")
    ext.add_argument("archive", type=Path)
    ext.add_argument("dest", type=Path)

    args = parser.parse_args(argv)

    if args.command == "summary":
        summary = build_summary(
            read_steps(args.steps),
            helpdesk=args.helpdesk,
            started=args.started,
            finished=args.finished,
            version=args.version,
            config=args.config,
            target=args.target,
            settings=_parse_settings(args.setting),
            notes=args.note,
        )
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8",
                            newline="\n")
        print_table(summary)
        return int(summary["exit_code"])

    if args.command == "wait-port":
        if wait_port(args.host, args.port, args.timeout):
            return 0
        print(f"nothing listening on {args.host}:{args.port} after {args.timeout:g}s",
              file=sys.stderr)
        return 1

    if args.command == "http":
        status = http_request(args.method, args.url)
        print(status)
        return 0 if 200 <= status < 300 else 1

    if args.command == "sha256":
        print(sha256_of(args.file))
        return 0

    if args.command == "relativize":
        for name in relativize_artifacts(args.run_dir, Path.cwd()):
            print(f"relative paths in {name}")
        return 0

    if args.command == "extract":
        extract(args.archive, args.dest)
        return 0

    return 1  # pragma: no cover - argparse requires a command


if __name__ == "__main__":
    sys.exit(main())
