"""sweep/run.sh, run for real against stubs of `docker` and `testinghq`.

The script is copied into a scratch repository holding stand-in compose,
setup, config and profile files, so nothing it writes lands in this checkout.
The stubs log every invocation and exit with whatever code a test asks for,
which is how these tests check the order of the tools, the flags each gets,
what the profile and `.env` feed them, and how exit codes become the summary
and the script's own exit status. Nothing opens a socket.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPDESKS = ("chatwoot", "osticket", "zammad", "freescout", "frappe")
#: Readback tools first, then blast and the corpus, barrage last.
TOOL_STEPS = [
    "config-validate", "verify", "ledger", "redeliver", "loop", "steady",
    "blast-catalog", "blast-corpus", "barrage",
]

#: osticket and freescout read back through PyMySQL; run.sh refuses them
#: up front when the interpreter cannot import it.
MYSQL_READY = importlib.util.find_spec("pymysql") is not None

#: The bash first on PATH. Not the bare name: on Windows, CreateProcess looks
#: in System32 before PATH, so "bash" starts WSL's, which cannot read the
#: Windows paths these tests hand it.
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

STUB_TESTINGHQ = r"""#!/usr/bin/env bash
printf 'testinghq %s\n' "$*" >>"$STUB_LOG"
printf 'auth=%s\n' "${CHATWOOT_INGRESS_AUTH-unset}" >>"$STUB_LOG"
if [ "$1" = --version ]; then
  echo "testinghq 9.9.9"
  exit 0
fi
tool=$1 prev="" out="" html=""
for arg in "$@"; do
  case $prev in
    --out) out=$arg ;;
    --html) html=$arg ;;
  esac
  prev=$arg
done
if [ "$tool" = report ]; then
  echo "<!doctype html>" >"$html"
  exit 0
fi
if [ -n "$out" ]; then
  # The directory as the tool would record it: native, which under Git Bash
  # is what `pwd -W` prints, not the MSYS spelling in $PWD.
  here=$(pwd -W 2>/dev/null || pwd)
  printf '{"tool": "%s", "sink": "%s/sweep/runs/sink.jsonl"}' "$tool" "$here" >"$out"
fi
var="STUB_EXIT_$(printf '%s' "$tool" | tr '[:lower:]' '[:upper:]')"
exit "${!var:-0}"
"""

STUB_DOCKER = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >>"$STUB_LOG"
exit "${STUB_DOCKER_EXIT:-0}"
"""

SETUP = """#!/usr/bin/env bash
set -euo pipefail
printf 'setup python=%s pythonpath=%s\\n' "${{PYTHON-unset}}" "${{PYTHONPATH-unset}}" >>"$STUB_LOG"
printf 'CHATWOOT_INGRESS_AUTH="Basic dXNlcjpwYXNz"\\r\\n' >sweep/.env.{h}
echo "URL: http://127.0.0.1:3100"
echo "Login: admin@helpdesk.test"
echo "Add to .env: nothing"
exit {code}
"""

#: Profiles are sweep/profiles/<helpdesk>.env. Spelled apart so the suite's
#: guard against tests reading an operator's `.env` file stays exact.
PROFILE_EXT = "env"

PROFILE = (
    "# a profile saved with Windows line endings\r\n"
    "SWEEP_MAX_WAIT=180\r\n"
    "SWEEP_QUIET_WINDOW=30\r\n"
    "SWEEP_CORPUS=0\r\n"
    "SWEEP_BARRAGE=1\r\n"
    "SWEEP_HEALTH_URL=http://127.0.0.1:3100/\r\n"
)


def _repo(tmp_path, failing_setup=(), profiles=None):
    root = tmp_path / "repo"
    sweep = root / "sweep"
    for sub in ("compose", "setup", "configs", "profiles"):
        (sweep / sub).mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "sweep" / "run.sh", sweep / "run.sh")
    shutil.copy2(REPO_ROOT / "sweep" / "harness.py", sweep / "harness.py")
    for h in HELPDESKS:
        (sweep / "compose" / f"{h}.yml").write_text(f"name: thq-{h}\n", encoding="utf-8")
        code = 1 if h in failing_setup else 0
        (sweep / "setup" / f"{h}.sh").write_text(SETUP.format(h=h, code=code),
                                                 encoding="utf-8")
        (sweep / "configs" / f"{h}.toml").write_text(f"[targets.{h}]\n", encoding="utf-8")
        profile = (profiles or {}).get(h, PROFILE)
        (sweep / "profiles" / f"{h}.{PROFILE_EXT}").write_bytes(profile.encode("utf-8"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("testinghq", STUB_TESTINGHQ), ("docker", STUB_DOCKER),
                       ("curl", "#!/usr/bin/env bash\nexit 0\n")):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return root


def _run(tmp_path, root, *args, **env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWEEP_", "STUB_"))}
    env.pop("CHATWOOT_INGRESS_AUTH", None)
    env.pop("PYTHONPATH", None)
    env.update(
        PATH=f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}",
        TESTINGHQ=str(tmp_path / "bin" / "testinghq"),
        PYTHON=sys.executable,
        STUB_LOG=str(tmp_path / "calls.log"),
    )
    env.update(env_overrides)
    return subprocess.run(
        [BASH, str(root / "sweep" / "run.sh"), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


def _summary(root, helpdesk):
    (path,) = (root / "sweep" / "runs" / helpdesk).glob("*/summary.json")
    return json.loads(path.read_text(encoding="utf-8")), path.parent


def _calls(tmp_path):
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def _steps(summary):
    return {step["step"]: step for step in summary["steps"]}


def test_a_run_records_every_step_in_order_and_a_finding_does_not_fail_it(tmp_path):
    root = _repo(tmp_path)
    result = _run(tmp_path, root, "chatwoot", STUB_EXIT_LEDGER="3")
    assert result.returncode == 0, result.stdout + result.stderr

    summary, run_dir = _summary(root, "chatwoot")
    assert re.fullmatch(r"\d{8}T\d{6}Z", run_dir.name)
    assert summary["helpdesk"] == "chatwoot"
    assert summary["testinghq_version"] == "9.9.9"
    assert summary["config"] == "sweep/configs/chatwoot.toml"
    assert summary["target"] == "chatwoot"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", summary["started"])
    assert summary["finished"] >= summary["started"]
    assert summary["result"] == "findings" and summary["exit_code"] == 0
    assert [s["step"] for s in summary["steps"]] == ["compose-up", "setup", *TOOL_STEPS]
    assert "readback tools" in summary["notes"][0]

    steps = _steps(summary)
    assert steps["compose-up"]["command"] == "docker compose -f sweep/compose/chatwoot.yml up -d"
    assert steps["setup"]["command"] == "bash sweep/setup/chatwoot.sh"
    assert steps["ledger"]["exit_code"] == 3
    assert steps["ledger"]["meaning"] == "ran, answer was no"
    assert steps["blast-corpus"]["skipped"] == "SWEEP_CORPUS=0"
    assert steps["config-validate"]["artifact"] is None
    for name in ("blast-catalog", "verify", "ledger", "redeliver", "loop", "steady", "barrage"):
        assert steps[name]["artifact"] == f"{name}.json"
        assert steps[name]["html"] == f"{name}.html"
        assert steps[name]["report_exit_code"] == 0
        assert (run_dir / f"{name}.html").exists()
        assert (run_dir / f"{name}.log").exists()

    prefix = f"sweep/runs/chatwoot/{run_dir.name}"
    assert steps["barrage"]["command"] == (
        "testinghq barrage fire --config sweep/configs/chatwoot.toml --target chatwoot "
        f"--send --rate 5 --duration 60 --out {prefix}/barrage.json"
    )
    assert steps["blast-catalog"]["command"] == (
        "testinghq blast fire --config sweep/configs/chatwoot.toml --target chatwoot "
        f"--send --catalog --out {prefix}/blast-catalog.json"
    )
    # The artifact the stub wrote carried an absolute path; the run rewrote it.
    verify = json.loads((run_dir / "verify.json").read_text(encoding="utf-8"))
    assert verify["sink"] == "sweep/runs/sink.jsonl"


def test_the_profile_and_env_file_are_parsed_not_executed_and_are_crlf_safe(tmp_path):
    root = _repo(tmp_path)
    assert _run(tmp_path, root, "chatwoot").returncode == 0
    summary, run_dir = _summary(root, "chatwoot")
    prefix = f"sweep/runs/chatwoot/{run_dir.name}"
    calls = _calls(tmp_path)
    for tool in ("verify", "ledger", "redeliver", "loop", "steady"):
        assert (
            f"testinghq {tool} fire --config sweep/configs/chatwoot.toml --target chatwoot "
            f"--send --max-wait 180 --quiet-window 30 --out {prefix}/{tool}.json"
        ) in calls
    # A value with a space, quoted, written with CRLF by the setup script.
    after_setup = calls[calls.index("testinghq config validate sweep/configs/chatwoot.toml") + 1]
    assert after_setup == "auth=Basic dXNlcjpwYXNz"
    # The secret reaches the tools through the environment, never a command.
    assert "dXNlcjpwYXNz" not in json.dumps(summary)
    assert summary["settings"]["SWEEP_MAX_WAIT"] == "180"


def test_a_variable_in_the_callers_environment_wins_over_the_profile(tmp_path):
    root = _repo(tmp_path)
    assert _run(tmp_path, root, "zammad", SWEEP_MAX_WAIT="7", SWEEP_BARRAGE="0").returncode == 0
    summary, _ = _summary(root, "zammad")
    steps = _steps(summary)
    assert "--max-wait 7 --quiet-window 30" in steps["verify"]["command"]
    assert steps["barrage"]["skipped"] == "SWEEP_BARRAGE=0"
    assert not any(line.startswith("testinghq barrage") for line in _calls(tmp_path))


def test_an_expected_route_goes_to_the_tools_that_check_routing(tmp_path):
    root = _repo(tmp_path)
    assert _run(tmp_path, root, "zammad", SWEEP_EXPECT_ROUTE="Users").returncode == 0
    steps = _steps(_summary(root, "zammad")[0])
    for tool in ("verify", "ledger", "redeliver"):
        assert "--quiet-window 30 --expect-route Users --out" in steps[tool]["command"]
    for tool in ("loop", "steady"):
        assert "--expect-route" not in steps[tool]["command"]


def test_setup_and_the_tools_get_python_and_the_repository_on_pythonpath(tmp_path):
    """A custom adapter named as examples.readback.adapters.<module> imports
    only with the repository root on the path; setup scripts call $PYTHON."""
    root = _repo(tmp_path)
    assert _run(tmp_path, root, "zammad").returncode == 0
    assert f"setup python={sys.executable} pythonpath=." in _calls(tmp_path)


def test_without_testinghq_set_the_run_uses_the_interpreters_cli(tmp_path):
    """The default command is `$PYTHON -m testinghq.cli`; the summary records
    the version that command reports."""
    import testinghq

    root = _repo(tmp_path, failing_setup=("zammad",))
    env = {"TESTINGHQ": ""}
    assert _run(tmp_path, root, "zammad", **env).returncode == 1
    assert _summary(root, "zammad")[0]["testinghq_version"] == testinghq.__version__


def test_a_mysql_backed_helpdesk_needs_pymysql_before_anything_starts(tmp_path):
    if MYSQL_READY:
        pytest.skip("PyMySQL is installed here")
    root = _repo(tmp_path)
    result = _run(tmp_path, root, "osticket")
    assert result.returncode == 1
    assert "-m pip install PyMySQL" in result.stderr
    assert not (tmp_path / "calls.log").exists()


def test_a_refused_tool_fails_the_run_but_every_later_tool_still_runs(tmp_path):
    root = _repo(tmp_path)
    result = _run(tmp_path, root, "chatwoot", STUB_EXIT_VERIFY="1")
    assert result.returncode == 1
    summary, _ = _summary(root, "chatwoot")
    steps = _steps(summary)
    assert summary["result"] == "error" and summary["exit_code"] == 1
    assert steps["verify"]["exit_code"] == 1
    later = ("ledger", "redeliver", "loop", "steady", "blast-catalog", "barrage")
    assert [steps[t]["exit_code"] for t in later] == [0] * len(later)


def test_a_failed_setup_skips_every_tool_and_fails_the_run(tmp_path):
    root = _repo(tmp_path, failing_setup=("frappe",))
    result = _run(tmp_path, root, "frappe")
    assert result.returncode == 1
    summary, _ = _summary(root, "frappe")
    steps = _steps(summary)
    assert steps["setup"]["exit_code"] == 1
    assert all(steps[name]["skipped"] == "blocked: setup failed" for name in TOOL_STEPS)
    assert not any(line.startswith("testinghq ") and "fire" in line for line in _calls(tmp_path))


def test_the_corpus_step_fires_the_corpus_directory_when_switched_on(tmp_path):
    root = _repo(tmp_path)
    corpus = "sweep/.cache/zammad/test/data/mail"
    result = _run(tmp_path, root, "zammad", SWEEP_CORPUS="1", SWEEP_CORPUS_DIR=corpus)
    assert result.returncode == 0, result.stdout + result.stderr
    summary, run_dir = _summary(root, "zammad")
    assert _steps(summary)["blast-corpus"]["command"] == (
        "testinghq blast fire --config sweep/configs/zammad.toml --target zammad --send "
        f"--corpus {corpus} --count 100 --out sweep/runs/zammad/{run_dir.name}/blast-corpus.json"
    )


def test_all_runs_the_five_in_turn_and_stops_each(tmp_path):
    """chatwoot and zammad complete and frappe fails at setup. osticket and
    freescout need PyMySQL, and without it stop before starting anything.
    zammad's profile sets no wait: chatwoot's exports must not leak into it."""
    root = _repo(tmp_path, failing_setup=("frappe",),
                 profiles={"zammad": "SWEEP_BARRAGE=0\n"})
    result = _run(tmp_path, root, "all")
    assert result.returncode == 1
    mysql = ("osticket", "freescout")
    failed = ["frappe"] if MYSQL_READY else ["osticket", "freescout", "frappe"]
    assert f"errors in: {' '.join(failed)}" in result.stderr
    started = [h for h in HELPDESKS if MYSQL_READY or h not in mysql]
    calls = _calls(tmp_path)
    ups = [line for line in calls if line.endswith(" up -d")]
    downs = [line for line in calls if line.endswith(" down")]
    assert ups == [f"docker compose -f sweep/compose/{h}.yml up -d" for h in started]
    assert downs == [f"docker compose -f sweep/compose/{h}.yml down" for h in HELPDESKS]
    results = {h: _summary(root, h)[0]["result"] for h in started}
    assert results["chatwoot"] == results["zammad"] == "passed"
    assert results["frappe"] == "error"
    chatwoot = _steps(_summary(root, "chatwoot")[0])
    zammad = _steps(_summary(root, "zammad")[0])
    assert "--max-wait 180" in chatwoot["verify"]["command"]
    assert "--max-wait" not in zammad["verify"]["command"]
    assert zammad["barrage"]["skipped"] == "SWEEP_BARRAGE=0"


def test_all_leaves_every_helpdesk_running_with_sweep_keep(tmp_path):
    root = _repo(tmp_path, failing_setup=HELPDESKS)
    _run(tmp_path, root, "all", SWEEP_KEEP="1")
    calls = _calls(tmp_path)
    assert len([line for line in calls if line.endswith(" up -d")]) == (5 if MYSQL_READY else 3)
    assert not any(line.endswith(" down") for line in calls)


def test_an_unknown_helpdesk_is_refused_with_the_usage(tmp_path):
    root = _repo(tmp_path)
    result = _run(tmp_path, root, "helpscout")
    assert result.returncode == 1
    assert "usage: sweep/run.sh <chatwoot|osticket|zammad|freescout|frappe|all|mailpit>" \
        in result.stderr
    assert not (root / "sweep" / "runs").exists()
