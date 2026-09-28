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


def test_no_duplicate_test_function_names_within_a_file():
    """Two test functions with the same name in one module.

    The worst kind of duplicate, because the kind that hides. Python lets the
    second definition shadow the first, so pytest collects the second and the
    first is dead code that reads exactly like a passing test. A duplicate that
    asserts something DIFFERENT fails somewhere and gets noticed; a duplicate
    that asserts the same thing is indistinguishable from one test that passed,
    and a green suite says nothing about whether the thing it was written to
    check is checked at all.

    This is not hypothetical in this repository. Building the ten-task backlog
    produced two of them, and the earlier one, in
    tests/unit/test_cli_pipeline.py, predates that work. Both were found by an
    AST sweep rather than by a failure, which is the argument for the sweep.

    The same class of bug as the basename check above, one level down: that one
    stops two modules colliding across directories, this stops two functions
    colliding inside one.
    """
    import ast
    import collections

    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        names = collections.Counter(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        for name, count in names.items():
            if count > 1:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {name} x{count}")

    assert not offenders, (
        f"test functions defined more than once in the same module, where the "
        f"second silently shadows the first: {offenders}. Remove the later "
        "copy, or rename it if it was meant to be a different test."
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


def _code_only(source: str) -> str:
    """The source with its comments removed.

    Needed because several of these guards name the very strings they forbid,
    and the files explain the history at length. A test that failed on its own
    explanation would have to be deleted along with the explanation, so the
    guards read code rather than text.

    Deliberately simple: it strips full-line `//` comments and `/* ... */`
    blocks, which is enough here. It is not a JavaScript parser and does not
    pretend to be, and a string literal containing `//` would confuse it. What
    it is for is the guards below, whose subjects appear in identifiers rather
    than in string values.
    """
    import re as _re

    without_blocks = _re.sub(r"/\*.*?\*/", "", source, flags=_re.DOTALL)
    return "\n".join(
        line
        for line in without_blocks.splitlines()
        if not line.strip().startswith("//")
    )


def test_web_static_has_no_copy_of_the_expectation_rules():
    """The rules were in this repo three times, not twice.

    web/static/app.js re-derived each record's outcome from its status code,
    which made the browser an independent copy of core/report.py that no test
    could check, because no CI here ran JavaScript. The server now annotates
    every record with the engine's verdict and the browser renders it.

    The outcome is now read in web/static/render.js rather than app.js, because
    the presentation was moved there so that `node --test` can run it. Both
    files are checked, since either one re-deriving the verdict would be the
    same defect.
    """
    app_js = REPO_ROOT / "web" / "static" / "app.js"
    assert app_js.is_file(), "web/static/app.js is missing"

    source = app_js.read_text(encoding="utf-8")
    assert "function classifyRecord(" not in _code_only(source), (
        "web/static/app.js must not define a local copy of the expectation "
        "rules; the rules live in core/report.py only"
    )
    for helper in ("function is2xx(", "function is5xx(", "function isTimeout("):
        assert helper not in _code_only(source), (
            f"web/static/app.js must not define {helper[:-1]}(), which existed "
            "only to support a local copy of the expectation rules"
        )

    render_js = REPO_ROOT / "web" / "static" / "render.js"
    assert render_js.is_file(), "web/static/render.js is missing"
    render_code = _code_only(render_js.read_text(encoding="utf-8"))
    assert "function classifyRecord(" not in render_code, (
        "web/static/render.js must not define a local copy of the expectation "
        "rules either; it renders the engine's annotation"
    )
    for helper in ("function is2xx(", "function is5xx(", "function isTimeout("):
        assert helper not in render_code, (
            f"web/static/render.js must not define {helper[:-1]}(), which "
            "existed only to support a local copy of the expectation rules"
        )

    # The annotation is what both files read, rather than the status code.
    assert "outcome" in render_code, (
        "web/static/render.js should read record.outcome, the verdict the "
        "server computed"
    )


def test_app_js_does_not_reference_the_removed_classifier():
    """The check the one above was missing, and the reason app.js was broken.

    `test_web_static_has_no_copy_of_the_expectation_rules` asserts
    `function classifyRecord(` is not in the source, which is true and was true
    the whole time app.js was throwing a ReferenceError on every page load. The
    refactor that removed the function left behind:

        window.__testingHQBlast = { classifyRecord };

    A DEFINITION was gone, a REFERENCE to it was not, and the existing test
    could not tell the difference. So this asserts the name does not appear in
    executable code at all, comments excluded, which is the property that was
    actually needed.

    Comments are excluded deliberately: app.js and render.js both explain this
    failure at length, and a test that failed on its own explanation would have
    to be deleted along with the explanation.
    """
    app_js = REPO_ROOT / "web" / "static" / "app.js"
    assert app_js.is_file(), "web/static/app.js is missing"

    code = _code_only(app_js.read_text(encoding="utf-8"))
    assert "classifyRecord" not in code, (
        "web/static/app.js still references classifyRecord, which does not "
        "exist. In strict mode that is a ReferenceError, and it is why the web "
        "UI did not load at all. See web/static/render.js for the history."
    )


def test_the_rendering_module_is_where_the_browser_presentation_lives():
    """The rendering decision has to be somewhere CI can execute it.

    The rules themselves are not in render.js and must not be: the engine
    computes the outcome and the browser renders it. What render.js holds is the
    presentation, and holding it in a separate file is what makes
    `node --test web/tests/` possible at all, since a Python-only suite cannot
    run app.js.
    """
    render_js = REPO_ROOT / "web" / "static" / "render.js"
    assert render_js.is_file(), (
        "web/static/render.js is missing; the browser presentation has to live "
        "in a file a JavaScript test runner can load"
    )
    source = _code_only(render_js.read_text(encoding="utf-8"))
    assert "summarizeRun" in source
    # Still not a copy of the rules: it reads the annotation, it does not
    # recompute it.
    assert "outcome" in source
    assert "function classifyRecord(" not in source


def test_index_html_loads_the_rendering_module_before_app_js():
    """Order matters, and nothing else in the tree would notice if it broke.
    app.js reads window.TestingHQRender at load time, so a reordered script tag
    throws in a browser and in nothing else."""
    html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    render_at = html.find("render.js")
    app_at = html.find("app.js")
    assert render_at != -1, "index.html should load /render.js"
    assert app_at != -1, "index.html should load /app.js"
    assert render_at < app_at, (
        "render.js must be loaded before app.js; app.js reads the module at "
        "load time and a reversed order is a ReferenceError in a browser"
    )


def test_ci_runs_the_javascript_tests():
    """No JavaScript ran in CI before this, which is the whole reason the
    broken page went unnoticed.

    Read with comments stripped. The first version of this checked for the
    string `node --test` in the file, and the job's own comment block explains
    that the command exists, so commenting out the run step left the test
    passing. Same trap as the classifier guards, and the same fix.
    """
    ci_source = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    ci = "\n".join(
        line
        for line in ci_source.splitlines()
        if not line.strip().startswith("#")
    )
    assert "node --test" in ci, (
        "ci.yml should run the JavaScript tests; nothing else in the tree does, "
        "and that is how a page that threw on every load shipped"
    )
    assert "setup-node" in ci, "ci.yml should set up a Node runtime"
    assert "web/tests/" in ci, "ci.yml should point the runner at the web tests"


def test_nothing_imports_the_deleted_web_generator():
    """web/generator.py is gone, and it should stay gone.

    It was a deterministic stand-in for the real engine, written when
    blast/generate.py did not exist. When the adapter moved onto the real
    engine in #22, the file became dead weight that still looked authoritative:
    it had a `generate_run()` that produced convincing artifacts, a category
    list, and its own `GeneratorError`, and nothing would have complained if
    some later piece of code had imported it and quietly served the UI
    fixtures instead of the engine.

    That is the failure this guards. The check is on imports rather than on
    behaviour because the whole risk is that a caller is wired to the wrong
    module, not that the wrong module misbehaves.
    """
    generator = REPO_ROOT / "web" / "generator.py"
    assert not generator.exists(), (
        "web/generator.py is the deleted fixture stand-in; if it is genuinely "
        "needed again it should be rebuilt on top of the engine, not restored"
    )
    test_file = REPO_ROOT / "tests" / "web" / "test_generator.py"
    assert not test_file.exists(), (
        "tests/web/test_generator.py tested the deleted stand-in; the engine's "
        "own tests are tests/unit/test_corrupt.py and "
        "tests/integration/test_corpus_generation.py"
    )

    forbidden = ("web.generator", "web import generator", "from . import generator")
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if ".git" in relative.parts or ".venv" in relative.parts:
            continue
        if relative.as_posix() in (
            "tests/test_lane_hygiene.py",
        ):
            continue  # this file names them in order to forbid them
        source = path.read_text(encoding="utf-8-sig")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{relative.as_posix()}: {needle!r}")
    assert not offenders, (
        f"nothing may import the deleted web.generator: {offenders}"
    )


def test_the_fixtures_survive_the_deletion_of_the_generator_that_made_them():
    """The run artifacts under web/tests/fixtures/ are data, not a generator,
    and they are still the schema corpus the suite checks against. Worth
    pinning, because deleting the stand-in is exactly the kind of cleanup that
    takes the fixtures with it by accident, and they cannot be regenerated
    from the tree because nothing generates them any more.
    """
    fixtures = REPO_ROOT / "web" / "tests" / "fixtures"
    names = sorted(p.name for p in fixtures.glob("*.json"))
    assert names == ["sample_run_clean.json", "sample_run_with_failures.json"], (
        f"the shipped run artifacts changed: {names}"
    )
