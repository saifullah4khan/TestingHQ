"""Repo-wide invariants that no single lane owns.

Every defect found on 2026-07-16 while running the assignment pack by hand lived
in a seam between lanes, not inside one. In all three cases both lanes obeyed the
collision rule in GOALS.md exactly, because that rule governs which files a lane
writes, and none of these were about a shared file:

1. Lane B imported testinghq.blast.serialize from an unmerged Lane A branch. No
   shared file. The branch could not pass CI until Lane A merged.
2. The web lane reimplemented the guardrails instead of importing them. No shared
   file. The two copies disagreed about what was safe within hours.
3. tests/unit/test_config.py and tests/web/test_config.py collided in the pytest
   module namespace. No shared file. Both lanes green alone, uncollectable
   together.

Prose in GOALS.md documents all three. Prose does not fail a build. These tests
do. If you are about to delete one of these because it is inconvenient, that is
the moment it is doing its job.
"""
from __future__ import annotations

import collections
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Written as an escape on purpose. Spelling the character literally here would
# make this file violate the rule it enforces. It did, on the first run, and this
# test caught its own source. Leave it as an escape.
EM_DASH = chr(0x2014)

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".html", ".css", ".js", ".json", ".txt"}
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", "node_modules", ".mypy_cache"}


def _tracked_text_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def test_no_duplicate_test_module_basenames():
    """Two test files sharing a basename make the suite uncollectable.

    None of the tests/ directories carry an __init__.py, so pytest derives each
    test module's name from its basename alone. Two files called test_config.py in
    different directories both become the module `test_config`, and collection
    dies for the whole suite, not just for those two files.

    This is a cross-lane hazard with no owner: the engine lane writes tests/unit,
    the web lane writes tests/web, neither can see the other's basenames, and each
    is green on its own branch. It only detonates when they meet on main.

    Fixing this by adding __init__.py or switching to --import-mode=importlib was
    tried and reverted: both break the integration lane's sibling import of
    fake_sink and six of the web lane's modules. Unique basenames is the cheap
    invariant. Keep it.
    """
    test_files = [p for p in (REPO_ROOT / "tests").rglob("test_*.py")
                  if not any(part in SKIP_DIRS for part in p.parts)]
    assert test_files, "found no test modules, this test is not doing anything"

    by_name = collections.defaultdict(list)
    for path in test_files:
        by_name[path.name].append(str(path.relative_to(REPO_ROOT)))

    duplicates = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    assert not duplicates, (
        "test modules share a basename and pytest cannot collect them together: "
        f"{duplicates}. Rename one. See the docstring for why __init__.py and "
        "--import-mode=importlib are not the fix here."
    )


def test_no_em_dashes_in_tracked_text():
    """No em-dashes anywhere: code, comments, docs, commit messages.

    A house rule from day one, enforced until now only by whoever was reading. Six
    agents wrote code today and every one of them was told this in prose. Prose
    scales badly; a failing test does not.
    """
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if EM_DASH in text:
            line_no = next(
                (i for i, line in enumerate(text.splitlines(), 1) if EM_DASH in line),
                None,
            )
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}")

    assert not offenders, (
        "em-dashes found, house rule forbids them everywhere. Use hyphens or "
        f"rephrase: {offenders}"
    )


def test_web_delegates_to_canonical_guardrails():
    """The web lane must import the guardrails, never reimplement them.

    The first version of web/adapter.py defined its own AdapterGuardrailError and
    its own target and confirm checks. Both lanes were individually correct and the
    two copies disagreed within hours: the security lane hardened
    require_configured_target to refuse non-reserved public hosts, and the web copy
    did not inherit it, so a target the CLI refused the UI would have fired at.

    The whole reason core/guardrails.py is a separate lane that no coder may edit
    is that there is exactly one place the safety rules live. A second copy defeats
    that regardless of how correct it looks in isolation.
    """
    adapter = REPO_ROOT / "web" / "adapter.py"
    assert adapter.is_file(), "web/adapter.py is missing"

    source = adapter.read_text(encoding="utf-8")
    assert "from testinghq.core import guardrails" in source, (
        "web/adapter.py must import the canonical guardrails module"
    )
    assert "guardrails.require_configured_target" in source, (
        "web/adapter.py must gate targets through the canonical guardrail, not a local copy"
    )
    assert "guardrails.evaluate_send" in source, (
        "web/adapter.py must gate sending through the canonical guardrail, not a local copy"
    )


def test_web_expectations_delegates_to_canonical_report():
    """The same lesson, learned the same way, one module over.

    web/expectations.py carried a second copy of classify_record,
    flag_for_record and compute_summary, written before core/report.py
    existed. The guardrail incident above is the precedent: two correct copies
    that disagree are worse than one copy, and nothing in the build notices
    until the disagreement has already shipped.

    The check is structural rather than behavioural on purpose. A behavioural
    check would compare outputs and pass happily while two implementations
    drifted, which is precisely the failure mode being guarded against. What
    must be impossible is a second body to drift.
    """
    expectations = REPO_ROOT / "web" / "expectations.py"
    assert expectations.is_file(), "web/expectations.py is missing"

    source = expectations.read_text(encoding="utf-8")
    assert "from testinghq.core.report import" in source, (
        "web/expectations.py must import the canonical report module"
    )
    for rule in ("classify_record", "flag_for_record", "compute_summary"):
        assert f"def {rule}(" not in source, (
            f"web/expectations.py must not define its own {rule}(); the rules "
            "live in testinghq/core/report.py and are inherited, not copied"
        )


def test_web_static_has_no_copy_of_the_expectation_rules():
    """The rules were in this repo three times, not twice.

    web/static/app.js re-derived each record's outcome from its status code,
    which made the browser an independent copy of core/report.py that no test
    could check, because no CI here runs JavaScript. The server now annotates
    every record with the engine's verdict and the browser renders it.

    Asserted on the source text because that is the only thing available. A
    behavioural test would need a JavaScript runtime, and adding one to a
    Python-only repo to protect code that should not exist is the wrong
    trade. The rule literals are what a drifted copy would contain.
    """
    app_js = REPO_ROOT / "web" / "static" / "app.js"
    assert app_js.is_file(), "web/static/app.js is missing"

    source = app_js.read_text(encoding="utf-8")
    assert "function classifyRecord(" not in source, (
        "web/static/app.js must read record.outcome from the server, not "
        "re-derive it; the expectation rules live in core/report.py only"
    )
    for helper in ("function is2xx(", "function is5xx(", "function isTimeout("):
        assert helper not in source, (
            f"web/static/app.js must not define {helper[:-1]}(), which existed "
            "only to support a local copy of the expectation rules"
        )
    assert "record.outcome" in source, (
        "web/static/app.js should read the outcome the server annotated"
    )
