"""Tests for testinghq/compare, the regression differ.

The interesting assertions here are the negative ones. compare's value
depends on two properties that are easy to break silently:

1. It cannot send anything. It has no transport, no target, no --send.
2. It defines no classification rules of its own.

Both are checked structurally, at the source level, because that is the only
way to check them. A behavioural test that "it did not make a network call"
would pass just as happily if the code were rewritten to make one on a branch
the test never exercised, which is the same failure mode the fake sink's
import check was written to catch.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from testinghq.compare import runs as bw
from testinghq.core import report

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPARE_DIR = REPO_ROOT / "testinghq" / "compare"


# ---------------------------------------------------------------------------
# Fixtures: hand-built artifacts, so each test states exactly the records it
# cares about instead of depending on a generated corpus.
# ---------------------------------------------------------------------------


def _record(record_id, category, status, passed=None):
    """One record. `passed` overrides the assertion; when None, a non-2xx is
    treated as a failed assertion the way StatusOnlyMatcher would."""
    if passed is None:
        passed = status is not None and 200 <= status < 300
    return {
        "id": record_id,
        "category": category,
        "payload_sha256": "0" * 64,
        "intended": {},
        "response": {
            "status": status,
            "latency_ms": 1.0,
            "body_snippet": "",
        },
        "assertion": {"passed": passed, "mismatches": []},
    }


def _artifact(records, seed=1, count=None, mix=None, target="local"):
    config = {
        "mix": list(mix or report.CATEGORIES),
        "count": len(records) if count is None else count,
        "seed": seed,
        "dry_run": False,
        "target": target,
    }
    return {
        "seed": seed,
        "config": config,
        "summary": report.compute_summary(records, seed, config),
        "records": records,
    }


# ---------------------------------------------------------------------------
# The three transitions
# ---------------------------------------------------------------------------


def test_identical_artifacts_report_no_change():
    records = [
        _record("clean-1-0000", report.CLEAN, 200),
        _record("degenerate-1-0001", report.DEGENERATE, 400),
    ]
    diff = bw.compare(_artifact(records), _artifact(list(records)))

    assert diff["regressed"] is False
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 0
    assert diff["changes"]["count"] == 0
    assert diff["unchanged"] == 2
    assert diff["comparable"] is True
    assert diff["aligned_by"] == "id"


def test_a_payload_that_stopped_passing_is_a_regression():
    baseline = _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    candidate = _artifact([_record("clean-1-0000", report.CLEAN, 500)])

    diff = bw.compare(baseline, candidate)

    assert diff["regressed"] is True
    assert diff["regressions"]["count"] == 1
    entry = diff["regressions"]["records"][0]
    assert entry["id"] == "clean-1-0000"
    assert entry["from"] == report.OK
    assert entry["to"] == report.CLEAN_FAILED
    assert entry["status_from"] == "2xx"
    assert entry["status_to"] == "5xx"
    assert "did not 2xx" in entry["flag"]


def test_a_payload_that_started_passing_is_a_fix():
    baseline = _artifact([_record("clean-1-0000", report.CLEAN, 500)])
    candidate = _artifact([_record("clean-1-0000", report.CLEAN, 200)])

    diff = bw.compare(baseline, candidate)

    assert diff["regressed"] is False
    assert diff["fixes"]["count"] == 1
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["records"][0]["to"] == report.OK


def test_swapping_one_failure_for_another_is_neither_fix_nor_regression():
    """A degenerate payload that used to be rejected with a 4xx and now
    returns 500 has not improved, and a tool that called that a fix would
    flatter a change that traded one breakage for another.

    A degenerate payload that simply changes its status without changing its
    verdict is even more common and is reported as unchanged, not as a change:
    a 400 and a 500 for that category are both DEGENERATE_FAILED, and only the
    outcome is being compared. That is the same reason status is not the unit
    of comparison, and it is asserted separately below.
    """
    baseline = _artifact(
        [_record("degenerate-1-0000", report.DEGENERATE, 400, passed=False)]
    )
    candidate = _artifact(
        [_record("degenerate-1-0000", report.DEGENERATE, 500, passed=False)]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["changes"]["count"] == 1
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 0
    assert diff["regressed"] is False
    entry = diff["changes"]["records"][0]
    assert entry["from"] == report.ASSERTION_FAILED
    assert entry["to"] == report.DEGENERATE_FAILED
    assert entry["status_from"] == "4xx"
    assert entry["status_to"] == "5xx"


def test_a_verdict_that_holds_while_the_status_moves_is_unchanged():
    """Both statuses are a failure for this category, so the payload did not
    get better or worse. Reporting it as a change would bury the real changes
    in noise, and reporting it as a regression would be wrong."""
    baseline = _artifact(
        [_record("degenerate-1-0000", report.DEGENERATE, None, passed=False)]
    )
    candidate = _artifact(
        [_record("degenerate-1-0000", report.DEGENERATE, 500, passed=False)]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["unchanged"] == 1
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 0
    assert diff["changes"]["count"] == 0
    # The status movement is still visible in the totals.
    rows = {row["key"]: row for row in diff["totals"]}
    assert rows["timeout"]["delta"] == -1
    assert rows["5xx"]["delta"] == 1


def test_outcome_is_compared_not_raw_status():
    """The reason this tool exists rather than a JSON diff. A structurally
    malformed payload returning 422 where it returned 400 has not regressed:
    both are a clean rejection, which is a PASS for that category. A status
    differ would flag it and teach people to ignore the output."""
    baseline = _artifact(
        [_record("structurally-malformed-1-0000", report.STRUCTURALLY_MALFORMED, 400)]
    )
    candidate = _artifact(
        [_record("structurally-malformed-1-0000", report.STRUCTURALLY_MALFORMED, 422)]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 0
    assert diff["changes"]["count"] == 0
    assert diff["unchanged"] == 1
    # The status movement is still reported, as a subordinate total.
    rows = {row["key"]: row for row in diff["totals"]}
    assert rows["4xx"]["delta"] == 0


# ---------------------------------------------------------------------------
# Alignment and comparability
# ---------------------------------------------------------------------------


def test_records_are_aligned_by_id_not_by_position():
    """Same two payloads, delivered in opposite order, with clean-0001 broken
    in the baseline and fixed in the candidate. Aligning by position would
    compare the wrong records to each other and report two bogus movements
    instead of one real fix."""
    baseline = _artifact(
        [
            _record("clean-1-0000", report.CLEAN, 200),
            _record("clean-1-0001", report.CLEAN, 500),
        ]
    )
    candidate = _artifact(
        [
            _record("clean-1-0001", report.CLEAN, 200),
            _record("clean-1-0000", report.CLEAN, 200),
        ]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["aligned_by"] == "id"
    assert diff["records_compared"] == 2
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 1
    assert diff["fixes"]["records"][0]["id"] == "clean-1-0001"
    assert diff["unchanged"] == 1


def test_positional_alignment_would_have_given_a_different_answer():
    """The control for the test above, so the id-alignment claim is not merely
    an assertion about the implementation.

    Four payloads. The baseline breaks two of them; the candidate fixes one of
    those two, and delivers the payloads rotated by one. Aligned by id there is
    exactly one movement, the genuine fix. Aligned by position there are three,
    because position also pairs payloads that are not the same payload, and two
    of those three are invented. That difference is the whole reason alignment
    is by id rather than by index.
    """
    baseline = _artifact(
        [
            _record("clean-1-0000", report.CLEAN, 200),
            _record("clean-1-0001", report.CLEAN, 200),
            _record("clean-1-0002", report.CLEAN, 500),
            _record("clean-1-0003", report.CLEAN, 500),
        ]
    )
    candidate = _artifact(
        [
            _record("clean-1-0001", report.CLEAN, 200),
            _record("clean-1-0002", report.CLEAN, 500),
            _record("clean-1-0003", report.CLEAN, 200),
            _record("clean-1-0000", report.CLEAN, 200),
        ]
    )

    positional_movements = sum(
        1
        for i, base_record in enumerate(baseline["records"])
        if bw._outcome(base_record) != bw._outcome(candidate["records"][i])
    )
    diff = bw.compare(baseline, candidate)

    assert positional_movements == 3, (
        "if this stops being 3 the id-aligned test above has stopped proving "
        "that alignment changed the answer and this case needs rethinking"
    )
    assert diff["regressions"]["count"] == 0
    assert diff["fixes"]["count"] == 1
    assert diff["fixes"]["records"][0]["id"] == "clean-1-0003"
    assert diff["unchanged"] == 3
    assert diff["records_compared"] == 4


def test_records_without_ids_fall_back_to_position_and_say_so():
    records = [_record(None, report.CLEAN, 200), _record(None, report.CLEAN, 200)]
    for record in records:
        del record["id"]
    diff = bw.compare(_artifact(list(records)), _artifact(list(records)))

    assert diff["aligned_by"] == "index"
    assert diff["regressed"] is False
    assert any("aligned by position" in w for w in diff["warnings"])


def test_differing_corpus_shape_is_reported_not_silently_compared():
    baseline = _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    candidate = _artifact(
        [
            _record("clean-1-0000", report.CLEAN, 200),
            _record("clean-1-0001", report.CLEAN, 200),
        ]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["comparable"] is False
    assert any("differ in shape" in w for w in diff["warnings"])
    assert diff["records_compared"] == 1


@pytest.mark.parametrize(
    "baseline_kwargs, candidate_kwargs, expected",
    [
        ({"seed": 1}, {"seed": 2}, "seeds differ"),
        ({"count": 10}, {"count": 20}, "counts differ"),
        ({"mix": ["clean"]}, {"mix": ["degenerate"]}, "mixes differ"),
        ({"target": "a"}, {"target": "b"}, "targets differ"),
    ],
)
def test_incomparable_runs_produce_a_warning_not_an_error(
    baseline_kwargs, candidate_kwargs, expected
):
    """Comparing runs from different targets or mixes is sometimes exactly
    what someone wants. Refusing would be presumptuous; staying quiet would be
    worse, because a diff of two different corpora is noise."""
    baseline = _artifact([_record("clean-1-0000", report.CLEAN, 200)], **baseline_kwargs)
    candidate = _artifact(
        [_record("clean-1-0000", report.CLEAN, 200)], **candidate_kwargs
    )

    diff = bw.compare(baseline, candidate)

    assert diff["comparable"] is False
    assert any(expected in w for w in diff["warnings"])


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_long_lists_truncate_but_the_counts_stay_exact():
    total = bw.MAX_LISTED + 10
    baseline = _artifact(
        [_record(f"clean-1-{i:04d}", report.CLEAN, 200) for i in range(total)]
    )
    candidate = _artifact(
        [_record(f"clean-1-{i:04d}", report.CLEAN, 500) for i in range(total)]
    )

    diff = bw.compare(baseline, candidate)

    assert diff["regressions"]["count"] == total
    assert len(diff["regressions"]["records"]) == bw.MAX_LISTED
    assert diff["regressions"]["truncated"] is True

    rendered = bw.format_diff(diff)
    assert f"and {total - bw.MAX_LISTED} more, not shown" in rendered


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def test_a_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(bw.CompareError) as exc:
        bw.load_artifact(str(tmp_path / "nope.json"))
    assert "could not read" in str(exc.value)


def test_invalid_json_is_reported_clearly(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(bw.CompareError) as exc:
        bw.load_artifact(str(path))
    assert "not valid JSON" in str(exc.value)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("[]", "top level must be a JSON object"),
        ('{"seed": 1}', "missing required key"),
        ('{"seed": 1, "config": {}, "summary": {}, "records": {}}', "'records' must be a list"),
        ('{"seed": 1, "config": {}, "records": []}', "missing required key 'summary'"),
        (
            '{"seed": 1, "config": {}, "summary": {}, "records": []}',
            "'config' must be an object",
        ),
    ],
)
def test_malformed_artifacts_are_refused_with_a_reason(tmp_path, payload, expected):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(bw.CompareError) as exc:
        bw.load_artifact(str(path))
    assert expected in str(exc.value)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_verdict_is_the_first_thing_on_the_page():
    """The first question is always "do I care", so the answer leads."""
    records = [_record("clean-1-0000", report.CLEAN, 200)]
    regressed = bw.compare(_artifact(records), _artifact([_record("clean-1-0000", report.CLEAN, 500)]))
    clean = bw.compare(_artifact(records), _artifact(records))

    assert bw.format_diff(regressed).splitlines()[0].startswith("compare: REGRESSED")
    assert bw.format_diff(clean).splitlines()[0].startswith("compare: no regressions")


def test_warnings_appear_near_the_top_not_buried():
    diff = bw.compare(
        _artifact([_record("clean-1-0000", report.CLEAN, 200)], seed=1),
        _artifact([_record("clean-1-0000", report.CLEAN, 200)], seed=2),
    )
    lines = bw.format_diff(diff).splitlines()
    warning_line = next(i for i, line in enumerate(lines) if line.strip().startswith("warning:"))
    assert warning_line < 6


def test_unchanged_totals_are_not_printed_as_a_wall_of_zeros():
    records = [_record("clean-1-0000", report.CLEAN, 200)]
    rendered = bw.format_diff(bw.compare(_artifact(records), _artifact(records)))
    assert "TOTALS: nothing moved" in rendered


# ---------------------------------------------------------------------------
# Structural guarantees. These are the tests that matter most.
# ---------------------------------------------------------------------------


def test_compare_defines_no_classification_rules_of_its_own():
    """A fourth copy of the expectation rules is a mistake this repository has
    already made three times: once for the guardrails, once in
    web/expectations.py, and once in web/static/app.js. Each one was correct
    and each one drifted. The guard is on the source text because the whole
    point is that two correct bodies can be compared forever and still differ
    where it matters."""
    offenders = []
    for path in sorted(COMPARE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for rule in ("def classify_record", "def flag_for_record", "def compute_summary"):
            if rule in source:
                offenders.append(f"{path.name}: {rule}")
    assert not offenders, (
        "compare must classify through testinghq.core.report, not define "
        f"its own rules: {offenders}"
    )


def test_compare_never_imports_a_transport():
    """It reads two JSON files. That is the entire job, and it is why it needs
    no dry-run mode and no guardrail gate: there is nothing to gate. If it ever
    gains a network import it has stopped being the thing that cannot hurt
    anything.

    Checked against the AST rather than the raw text. A substring check on the
    source is what this test did first, and it failed on its own docstrings,
    which discuss what the module deliberately does not do. Checking imports
    and call targets is both correct and immune to prose.
    """
    import ast

    forbidden_modules = {
        "socket",
        "urllib",
        "http",
        "requests",
        "asyncio",
    }
    for path in sorted(COMPARE_DIR.glob("*.py")):
        assert not (_imported_names(path) & forbidden_modules), (
            f"{path.name} imports a networking module"
        )
        assert "transport" not in _imported_names(path), (
            f"{path.name} imports a transport; compare reads files and "
            "must not acquire a network path"
        )


def _imported_names(path):
    """Every module and symbol name an import statement brings in.

    Both halves matter. `from ..core import report` names a module in `module`
    and a symbol in the alias list; `from ..core import transport` is the case
    the previous version of this collector missed, because it only looked at
    `node.module` and recorded "core". Checking the aliases too is what makes
    the guard able to see the thing it exists to prevent.

    Read as utf-8-sig so a stray byte-order mark makes this guard fail on the
    mark rather than with an opaque SyntaxError. One did, on a file this
    repository's own guard was meant to check.
    """
    import ast

    names = set()
    source = path.read_text(encoding="utf-8-sig")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return names


def test_no_module_in_the_package_carries_a_byte_order_mark():
    """Not style. A BOM makes `ast.parse` fail, so the transport guard above
    would raise instead of reporting, and a guard that raises on one file and
    passes on another is not a guard."""
    offenders = [
        path.name
        for path in sorted(COMPARE_DIR.glob("*.py"))
        if path.read_bytes()[:3] == b"\xef\xbb\xbf"
    ]
    assert not offenders, f"byte-order marks found: {offenders}"


def test_the_transport_guard_would_notice_if_compare_grew_a_transport_import():
    """Proven red, not assumed. The same collector that passes over the real
    modules does reject a module that reaches for the transport, which is the
    only way to know the guard is a guard."""
    import ast

    names = set()
    for node in ast.walk(ast.parse("from ..core import transport\n")):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name.split(".")[0])

    assert "transport" in names
    assert "core" in names

    # And the real modules pass, so the rejection above is discriminating
    # rather than a check that rejects everything.
    for path in sorted(COMPARE_DIR.glob("*.py")):
        assert "transport" not in _imported_names(path)


def test_compare_does_not_import_the_guardrails_either():
    """It has nothing to guard, so importing the guardrails would be
    decorative. Checked because a future author wiring up a target would reach
    for it out of habit, and the whole point of this subcommand is that it does
    not have one."""
    for path in sorted(COMPARE_DIR.glob("*.py")):
        assert "guardrails" not in path.read_text(encoding="utf-8"), (
            f"{path.name} imports guardrails; compare has no send path to gate"
        )


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def _write(tmp_path, name, artifact):
    path = tmp_path / name
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return str(path)


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_cli_compare_exits_zero_when_nothing_regressed(tmp_path):
    records = [_record("clean-1-0000", report.CLEAN, 200)]
    path = _write(tmp_path, "run.json", _artifact(records))

    result = _run_cli("compare", "--baseline", path, "--candidate", path)

    assert result.returncode == bw.EXIT_NO_REGRESSION
    assert "no regressions" in result.stdout


def test_cli_compare_exits_zero_on_a_regression_by_default(tmp_path):
    """A comparison is a comparison. Exiting non-zero by default would make
    the tool annoying to use by hand for no benefit."""
    baseline = _write(
        tmp_path, "base.json", _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    )
    candidate = _write(
        tmp_path, "cand.json", _artifact([_record("clean-1-0000", report.CLEAN, 500)])
    )

    result = _run_cli("compare", "--baseline", baseline, "--candidate", candidate)

    assert result.returncode == bw.EXIT_NO_REGRESSION
    assert "REGRESSED" in result.stdout


def test_cli_fail_on_regression_exits_nonzero(tmp_path):
    baseline = _write(
        tmp_path, "base.json", _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    )
    candidate = _write(
        tmp_path, "cand.json", _artifact([_record("clean-1-0000", report.CLEAN, 500)])
    )

    result = _run_cli(
        "compare", "--baseline", baseline,
        "--candidate", candidate, "--fail-on-regression",
    )

    assert result.returncode == bw.EXIT_REGRESSION


def test_cli_refuses_a_missing_file_with_the_shared_refused_code(tmp_path):
    """A missing file is a refusal, which is exit 1 across the package. It used
    to be exit 2 here, which every other tool reads as a dry run, so a script
    checking "did anything get sent" would have been told the tool held back
    rather than the tool having refused."""
    good = _write(
        tmp_path, "base.json", _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    )
    result = _run_cli(
        "compare", "--baseline", good, "--candidate", str(tmp_path / "gone.json")
    )

    assert result.returncode == bw.EXIT_USAGE
    assert result.returncode == 1
    assert "compare:" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_writes_a_machine_readable_report(tmp_path):
    baseline = _write(
        tmp_path, "base.json", _artifact([_record("clean-1-0000", report.CLEAN, 200)])
    )
    candidate = _write(
        tmp_path, "cand.json", _artifact([_record("clean-1-0000", report.CLEAN, 500)])
    )
    out = tmp_path / "diff.json"

    result = _run_cli(
        "compare", "--baseline", baseline, "--candidate", candidate,
        "--out", str(out),
    )

    assert result.returncode == bw.EXIT_NO_REGRESSION
    diff = json.loads(out.read_text(encoding="utf-8"))
    assert diff["regressed"] is True
    assert diff["regressions"]["count"] == 1


def test_compare_needs_no_target_or_config(tmp_path):
    """The property that makes it safe to run anywhere, including in a CI
    container with no config file and no network."""
    records = [_record("clean-1-0000", report.CLEAN, 200)]
    path = _write(tmp_path, "run.json", _artifact(records))

    result = _run_cli("compare", "--baseline", path, "--candidate", path)

    assert result.returncode == bw.EXIT_NO_REGRESSION
    assert "target" not in result.stdout.lower()
    assert "config" not in result.stdout.lower()
