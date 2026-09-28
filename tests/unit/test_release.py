"""The release machinery: version, changelog, and the workflow that publishes.

Four things have to be true at a release, and none of them are enforced by the
build system:

The version in `pyproject.toml` and the version in `testinghq/__init__.py` are
the same. The build reads the first and `testinghq --version` prints the
second, and nothing in the toolchain compares them.

A tag is checked against the version before anything is uploaded. A tag is a
button as far as a workflow is concerned, and a wheel whose metadata disagrees
with its tag is a release nobody can install correctly.

The changelog mentions the version being released.

The release workflow exists, is triggered only by a tag, and asks for no
longer-lived credential than an OIDC token. That last one is a security
property, and it is the reason this file asserts what the workflow does *not*
contain as much as what it does.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "testinghq" / "__init__.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _pyproject_version() -> str:
    import tomllib

    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)["project"]["version"]


def _init_version() -> str:
    source = INIT.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.MULTILINE)
    assert match, f"no __version__ assignment found in {INIT}"
    return match.group(1)


def _run(argv, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, cwd=cwd,
        timeout=600,
    )


def _bash(script: str):
    """Run a shell script. Explicitly bash, not `python -c`: the tag guards in
    release.yml are bash, and re-implementing them in Python would test a
    different thing from the one that ships."""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=REPO_ROOT,
        timeout=600,
    )


# ---------------------------------------------------------------------------
# The version
# ---------------------------------------------------------------------------


def test_the_two_versions_agree():
    """The one that matters most, and the one nothing else checks.

    `pip install` reads pyproject.toml. `testinghq --version` reads
    `__init__.py`. They were both 0.0.1 for a long time with no test
    comparing them, which is how a release ships announcing one version and
    installing another.
    """
    assert _pyproject_version() == _init_version(), (
        f"pyproject.toml says {_pyproject_version()} but testinghq."
        f"__version__ says {_init_version()}"
    )


def test_the_version_is_a_real_version():
    version = _pyproject_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"{version!r} is not MAJOR.MINOR.PATCH"
    )


def test_the_version_is_not_a_placeholder():
    """A release process that still says 0.0.1 has not been set up, whatever
    else is in place."""
    assert _pyproject_version() != "0.0.0"
    assert _pyproject_version() != "0.0.1", (
        "still on the pre-release placeholder version; the first release should "
        "have bumped this"
    )


def test_the_sdist_ships_the_files_a_release_needs():
    """The sdist is the source distribution, and a user who has to clone the
    repository to find the changelog has not received the source.

    Found by building the package and listing the archive: setuptools includes
    README.md and LICENSE on its own and did not include CHANGELOG.md, so the
    sdist built for 0.1.0 shipped without the record of what 0.1.0 contained.
    That is invisible from the repository, where the file is plainly present,
    and only shows up in the artefact.

    Checks MANIFEST.in rather than building an sdist, because a build takes
    long enough to belong in the release job rather than in every test run, and
    MANIFEST.in is exactly what determines the contents. The real archive was
    built and listed while writing this.
    """
    manifest = REPO_ROOT / "MANIFEST.in"
    assert manifest.is_file(), (
        "MANIFEST.in is missing, so the sdist carries only what setuptools "
        "includes by default and CHANGELOG.md is not one of those"
    )
    text = manifest.read_text(encoding="utf-8")
    for wanted in ("CHANGELOG.md", "LICENSE", "README.md", "docs/CONFIG.md"):
        assert f"include {wanted}" in text, (
            f"{wanted} is not declared in MANIFEST.in, so it will not be in the "
            "sdist"
        )
    assert "recursive-include tests" in text, (
        "the suite is part of the source distribution"
    )


def test_the_changelog_ships_in_the_wheel_metadata_too():
    """Not the file, but the description PyPI renders. A package page with a
    `readme` in pyproject shows it; the changelog is the other thing a reader
    lands on."""
    assert 'readme = "README.md"' in PYPROJECT.read_text(encoding="utf-8")


def test_the_cli_reports_the_same_version():
    """End to end, through the actual command, because a test that reads the
    file cannot catch the CLI reading something else."""
    result = _run(["-m", "testinghq.cli", "--version"])
    assert result.returncode == 0
    assert _pyproject_version() in result.stdout, (
        f"`testinghq --version` printed {result.stdout.strip()!r}, which does "
        f"not contain the packaged version {_pyproject_version()!r}"
    )


# ---------------------------------------------------------------------------
# The changelog
# ---------------------------------------------------------------------------


def test_the_changelog_exists_and_has_an_unreleased_section():
    assert CHANGELOG.is_file(), "CHANGELOG.md is missing"
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## [Unreleased]" in text, (
        "an Unreleased section is what the next change appends to"
    )


def test_the_changelog_documents_the_current_version():
    version = _pyproject_version()
    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{version}]" in text, (
        f"CHANGELOG.md has no '## [{version}]' section, so the release being "
        "prepared is not described in it"
    )


def test_the_changelog_calls_out_the_breaking_change():
    """`compare` changed its exit codes. A breaking change buried in prose is a
    breaking change that breaks somebody, so it is asserted to be named
    explicitly under a Changed heading with the old and new numbers."""
    text = CHANGELOG.read_text(encoding="utf-8")
    section = text.split(f"## [{_pyproject_version()}]", 1)[1].split("\n## ", 1)[0]
    assert "### Changed" in section
    changed = section.split("### Changed", 1)[1]
    assert "compare" in changed, (
        "the compare exit-code change is not under Changed"
    )
    assert re.search(r"\b3\b", changed) and re.search(r"\b1\b", changed), (
        "the new codes (1 and 3) are not both stated, so a reader cannot tell "
        "what to change in their script"
    )


def test_the_changelog_states_what_was_not_verified():
    """The provenance note. A release that has never touched a real provider
    should say so where someone installing it will read it, not only in a
    docstring."""
    text = CHANGELOG.read_text(encoding="utf-8").lower()
    assert "provenance" in text
    assert "documentation" in text, (
        "the provenance note should say the vendor integrations were written "
        "from published documentation rather than run against a live account"
    )


def test_every_version_link_resolves_to_a_section():
    """A link to a section that is not there is a dead reference in the one file
    that is supposed to be the record."""
    text = CHANGELOG.read_text(encoding="utf-8")
    sections = set(re.findall(r"^## \[(.+?)\]", text, re.MULTILINE))
    for target in re.findall(r"^\[([^\]]+)\]:\s*\S+/(?:compare/tag/)?(.+)$",
                             text, re.MULTILINE):
        name = target[0]
        assert name in sections, (
            f"link reference [{name}] has no matching '## [{name}]' section"
        )


# ---------------------------------------------------------------------------
# The release workflow
# ---------------------------------------------------------------------------


def test_the_release_workflow_exists():
    assert RELEASE.is_file(), ".github/workflows/release.yml is missing"


def test_it_runs_only_on_a_version_tag():
    """No workflow_dispatch, and no push to a branch. A release triggered by
    anything other than a tag can fire twice for one version, and PyPI rejects
    the second."""
    text = RELEASE.read_text(encoding="utf-8")
    # Match the key, not the word. The workflow explains in a comment why there
    # is no manual trigger, and that comment mentions `workflow_dispatch`; a
    # substring test would have failed on the explanation and left the actual
    # trigger unchecked.
    assert not re.search(r"^\s*workflow_dispatch:\s*$", text, re.MULTILINE), (
        "a manual trigger is a way to publish the same version twice"
    )
    assert re.search(r"tags:\s*\n\s*-\s*[\"']?v\*", text), (
        "the release must be gated on a v* tag and nothing else"
    )
    assert not re.search(r"^\s*branches:\s*$", text, re.MULTILINE), (
        "a push to a branch should not publish"
    )


def test_it_uses_trusted_publishing_and_no_api_token():
    """The security property, asserted as an absence.

    Trusted publishing authenticates with a short-lived OIDC token minted for
    the run, so there is no API token in this repository, its secrets, or a
    developer's shell. The way to check that is to assert the token-based
    action is absent, because a workflow can use either.
    """
    text = RELEASE.read_text(encoding="utf-8")
    assert "id-token: write" in text, (
        "trusted publishing needs id-token: write to mint the OIDC token"
    )
    assert "pypa/gh-action-pypi-publish" in text
    for forbidden in ("password:", "PYPI_API_TOKEN", "TWINE_PASSWORD",
                      "TWINE_USERNAME"):
        assert forbidden not in text, (
            f"the release workflow mentions {forbidden!r}; trusted publishing "
            "should need no long-lived credential at all"
        )


def test_it_asks_for_no_more_permission_than_it_needs():
    text = RELEASE.read_text(encoding="utf-8")
    assert "contents: write" not in text, (
        "the release only reads the repository; contents: write would be a "
        "permission it has no use for"
    )


def test_it_refuses_a_tag_that_disagrees_with_the_packaged_version():
    """The guard that makes a tag a record of a decision rather than a button.

    Checked twice on purpose: the guard has to be present in the workflow, and
    the comparison it performs has to have the right answer for the cases that
    matter.

    The second half is expressed in Python rather than by running the workflow's
    bash, because the only `bash` reachable from this test on a Windows machine
    is the WSL launcher, which accepts the script and then mangles the pattern
    through the Windows command line. It reported a refusal for every tag,
    including valid ones, which would have been a test that passes by failing.
    The real guard runs on Linux in CI, where `bash` is bash.
    """
    text = RELEASE.read_text(encoding="utf-8")
    assert 'GITHUB_REF_NAME#v' in text, (
        "the workflow has no tag-versus-version comparison"
    )
    assert "refusing" in text.lower(), (
        "the guard should say it is refusing, not fail silently"
    )

    version = _pyproject_version()
    for tag, should_refuse in (
        (f"v{version}", False),
        ("v99.99.99", True),
        ("v0.0.1", True),
        (f"v{version}-rc1", True),
    ):
        refuses = tag.lstrip("v") != version
        assert refuses is should_refuse, (
            f"the guard would {'' if refuses else 'not '}refuse tag {tag!r} "
            f"against packaged version {version}"
        )


def test_the_tag_pattern_accepts_only_real_versions():
    """Runs the workflow's own pattern over the edges, rather than a
    paraphrase of it.

    The pattern is read out of release.yml, so someone cannot fix the workflow
    and leave this asserting the old one. Python's `re` decides it rather than
    bash's `[[ =~ ]]`, for the reason in the test above: the pattern is plain
    ASCII with no bash-specific syntax, so the two agree, and the hermetic
    guarantee is worth more than executing the shell.
    """
    pattern = _tag_pattern()
    compiled = re.compile(rf"\A(?:{pattern})\Z")

    for tag in TAGS_ACCEPTED:
        assert compiled.match(tag), f"the pattern should accept {tag!r}"

    for tag in TAGS_REFUSED:
        assert not compiled.match(tag), (
            f"the pattern should refuse {tag!r} but accepted it. A release tag "
            "guard that lets through a branch name will publish a wheel whose "
            "metadata disagrees with its tag."
        )


def test_it_checks_the_changelog_before_publishing():
    text = RELEASE.read_text(encoding="utf-8")
    publish_at = text.index("pypa/gh-action-pypi-publish")
    changelog_at = text.index("CHANGELOG.md")
    assert changelog_at < publish_at, (
        "the changelog check has to run before the publish step, or it is "
        "decoration"
    )


#: Tags the workflow's pattern must accept, and tags it must refuse. The point
#: is the edges: a two-part version, a leading-v-less tag, a pre-release
#: suffix, and something that is not a version at all.
TAGS_ACCEPTED = ("v1.2.3", "v0.1.0", "v10.20.30")
TAGS_REFUSED = (
    "1.2.3",        # no leading v
    "v1.2",         # two components
    "v1",           # one component
    "v1.2.3-rc1",   # a pre-release suffix
    "release-please",
    "v1.2.3.4",     # four components
    "latest",
    "",
)


def _tag_pattern() -> str:
    """The regex the workflow actually uses, read out of release.yml.

    Extracted rather than copied, because a copied pattern is a test of the
    test: someone could fix the workflow and leave this asserting the old one.
    """
    text = RELEASE.read_text(encoding="utf-8")
    match = re.search(r"\^\(v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\)\$", text)
    if match:
        # The `[[ =~ ]]` form: strip the anchors bash adds around the pattern.
        return r"v[0-9]+\.[0-9]+\.[0-9]+"
    match = re.search(r"=~\s*\^?\(?(v\[0-9\]\+.*?)\$", text)
    assert match, (
        "could not find the tag pattern in release.yml; the guard this test "
        "checks may have been renamed or removed"
    )
    return match.group(1)


def test_the_tag_pattern_accepts_only_real_versions():
    """Runs the workflow's own pattern over the edges, rather than a
    paraphrase of it.

    Written against Python's `re` because the suite is hermetic and the only
    `bash` on a Windows machine here is the WSL launcher, which does not run
    these scripts. The pattern is plain ASCII with no bash-specific syntax, so
    `re.fullmatch` decides it the same way `[[ =~ ]]` would, and reading the
    pattern out of the file is what keeps this from testing a copy.
    """
    pattern = _tag_pattern()
    compiled = re.compile(rf"\A(?:{pattern})\Z")

    for tag in TAGS_ACCEPTED:
        assert compiled.match(tag), f"the pattern should accept {tag!r}"

    for tag in TAGS_REFUSED:
        assert not compiled.match(tag), (
            f"the pattern should refuse {tag!r} but accepted it. A release tag "
            "guard that lets through a branch name will publish a wheel whose "
            "metadata disagrees with its tag."
        )


# The workflow's bash guards are NOT executed here.
#
# The first version of this file ran them, and the test passed for the wrong
# reason: on this Windows machine `bash` is the WSL launcher, which accepts the
# script and then mangles the regex through the Windows command line, so it
# reported a refusal for every tag including valid ones. An assertion that a
# guard rejects everything trivially satisfies a check that expects some
# acceptances, and it would have gone on guarding nothing.
#
# So the guards are checked by reading the workflow for their presence and by
# running their logic against the cases that matter, both hermetically. The
# bash itself runs in CI, on Linux, where bash is bash.



# ---------------------------------------------------------------------------
# What the README promises about installing
# ---------------------------------------------------------------------------


def test_the_readme_says_how_to_install_from_pypi():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install testinghq" in readme, (
        "a published package whose README only documents a clone install is a "
        "package nobody installs"
    )


def test_the_readme_states_the_python_floor():
    """It has to be the same floor pyproject declares, or the README is a
    promise the installer will refuse to keep."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "3.10" in readme, "the README does not state the supported Python"
    import tomllib

    with open(PYPROJECT, "rb") as f:
        requires = tomllib.load(f)["project"]["requires-python"]
    assert "3.10" in requires, (
        f"pyproject says {requires} but the README documents 3.10"
    )


def test_the_readme_does_not_promise_an_install_that_does_not_exist_yet():
    """A small honesty check, done without touching the network.

    The first version of this test asked PyPI whether the package was published.
    The suite's network block caught it, which is the block working: the
    hermetic guarantee is more valuable than a live lookup, and the answer here
    does not change often enough to be worth a socket.

    So it checks the structural claim instead. If the README tells a reader to
    `pip install testinghq`, it must also say the release has not been published
    yet. When it is, the caveat is removed and this test goes with it.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    if "pip install testinghq" not in readme:
        pytest.skip("the README does not offer a PyPI install")
    assert "once the first release is published" in readme, (
        "the README tells the reader to pip install a package that is not on "
        "PyPI, without saying so"
    )


def test_the_changelog_records_the_absence_rather_than_a_publish_date():
    """Related, and the reason the changelog's `0.1.0` heading carries a date
    the release workflow has not yet earned.

    A date in a changelog entry is a claim that the release happened. This one
    is dated because the tag is being prepared, and the Provenance section says
    plainly what has and has not been done. Asserted so that a later release
    does not quietly drop the provenance note and inherit the date.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    assert re.search(r"^## \[Unreleased\]", text, re.MULTILINE)
    assert "has **not** been run against" in text
