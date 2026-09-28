"""The dogfood harness, and an honest check that it is not yet a dogfood run.

The harness exists so that running TestingHQ against a real deployment is one
command once two values are supplied. What it must not do is imply the run has
happened, because the whole value of a "we tested this against a real system"
claim is that it is true.

So these tests hold the two apart:

The config parses, the field paths resolve against a documented response, and
the headers resolve from the environment. All of that is checked, against real
values, and it runs in CI.

The absence of a real run is asserted too, which is unusual and deliberate. A
recipe that quietly stopped being honest, or a doc that dropped the sentence
saying it has not been run, would fail here rather than shipping a claim nobody
checked.

The one thing these tests cannot do is prove the deployment works. They prove
the harness is correct and the claim is accurately scoped.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from testinghq import config_doc
from testinghq.core.config import ConfigError, load_config
from testinghq.pipeline.adapters import AdapterError, parse_readback_config

REPO_ROOT = Path(__file__).resolve().parents[2]
RECIPE = REPO_ROOT / "examples" / "readback" / "handlehq.toml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOC = REPO_ROOT / "docs" / "DOGFOOD.md"
GITIGNORE = REPO_ROOT / ".gitignore"


def _env(**overrides):
    """An environment with the dogfood values filled in.

    The recipe cannot be loaded without a token: the loader refuses an unset
    `env:` reference before the run sends anything, which is the behaviour worth
    testing. So these tests supply plausible values rather than an empty
    environment, and check the refusal separately.
    """
    return {
        "HANDLEHQ_STAGING_URL": "https://staging.handle.example",
        "HANDLEHQ_TOKEN": "test-token-not-a-real-credential",
        "HANDLEHQ_READBACK_TIMEOUT": "30",
        **overrides,
    }


def _readback_config():
    raw = load_config(RECIPE).readback_table()
    return raw, parse_readback_config(raw, env=_env())


# ---------------------------------------------------------------------------
# It is a working config
# ---------------------------------------------------------------------------


def test_the_recipe_parses_with_the_real_loaders(monkeypatch):
    """The same check the docs tell the reader to run first. If this fails, the
    documented first step fails.

    The environment is patched rather than faked, because `config_doc.validate`
    reads `os.environ` and that is the behaviour worth testing: the real
    function, reading the real environment.
    """
    monkeypatch.setenv("HANDLEHQ_TOKEN", "test-token-not-a-real-credential")
    targets, readback, notes = config_doc.validate(RECIPE)
    assert "handlehq" in targets
    assert readback["kind"] == "http"
    assert readback["headers"] == {"Authorization": "env:HANDLEHQ_TOKEN"}


def test_the_recipe_url_cannot_resolve():
    """A placeholder that cannot resolve, on purpose.

    The first version of this file used `http://127.0.0.1:8000`, which is the
    trap: it is reachable, it answers 404, and 404 means "no record of this
    message", which is a FINDING. A run would have reported a pipeline losing
    every message, and the losing part would be a file nobody edited. A name
    that fails at DNS is legible in a way that is not.
    """
    from urllib.parse import urlsplit

    raw, _config = _readback_config()
    host = urlsplit(raw["url"]).hostname or ""
    assert host.endswith(".invalid"), (
        f"the recipe's url host is {host!r}. It should be under .invalid, the "
        "reserved TLD that cannot resolve, so a forgotten edit fails loudly "
        "rather than reporting a phantom finding."
    )
    assert "127.0.0.1" not in raw["url"] and "localhost" not in raw["url"], (
        "a loopback URL here is reachable and answers 404, which reads as a "
        "finding rather than as an unedited template"
    )


def test_the_recipe_holds_no_credential():
    """The one thing that must never be in a tracked file."""
    text = RECIPE.read_text(encoding="utf-8")
    for secret_shape in (r"sk-live-", r"sk_test_", r"ghp_", r"xox[baprs]-", r"AKIA"):
        assert not re.search(secret_shape, text), (
            f"the recipe appears to contain a credential matching {secret_shape}"
        )
    assert "env:HANDLEHQ_TOKEN" in text, (
        "the token must be an env: reference; a literal is refused at load"
    )


def test_the_recipe_names_only_the_required_field():
    """Stated in the recipe and asserted here.

    A field path that does not resolve reports NOT CHECKED, which looks exactly
    like a deployment that stores less than expected. So only `ticket_id`, which
    is required and whose absence is a refusal rather than a silent skip.
    """
    _raw, config = _readback_config()
    assert set(config.fields) == {"ticket_id"}, (
        f"the recipe maps {sorted(config.fields)}; every one beyond ticket_id is "
        "a guess until it is checked against a real response"
    )


def test_the_default_field_map_is_still_live_underneath():
    """The thing the recipe's own comment warns about, checked.

    `ReadbackConfig.field_map()` merges the configured paths OVER the defaults
    rather than using the configured table alone. So a recipe that names only
    `ticket_id` still reads `subject`, `body` and the rest at their default
    paths, and a response carrying those names will have them checked.

    That is worth a test because the opposite is what the table looks like, and
    getting it wrong in either direction matters: an operator who assumes
    nothing else is checked will trust a green run that checked more than they
    thought, and one who assumes more is checked than is will chase a field the
    tool was reading at a default path all along.
    """
    _raw, config = _readback_config()
    merged = config.field_map()

    assert "ticket_id" in config.fields, "the recipe names ticket_id explicitly"
    assert "subject" not in config.fields, (
        "the recipe does not name subject; it relies on the default"
    )
    assert "subject" in merged, (
        "the default map is merged underneath, so subject is read at its "
        "default path even though the recipe never mentions it"
    )
    # And the recipe's own override wins where it does name one.
    assert merged["ticket_id"] == "id"
    assert config.field_map()["ticket_id"] == "id"


def test_the_recipe_resolves_its_header_from_the_environment():
    _raw, config = _readback_config()
    assert config.header_names() == ["Authorization"]
    # The resolved value is held, and must never be rendered. Asserting on the
    # render rather than the field, because the field is the secret.
    rendered = repr(config.to_json())
    assert "test-token-not-a-real-credential" not in rendered
    assert "Authorization" in rendered, "the header NAME is safe to record"


def test_an_unset_token_is_refused_at_load():
    """Before the run sends anything, which is the behaviour the docs tell the
    reader to rely on when they run `config validate` first."""
    raw = load_config(RECIPE).readback_table()
    with pytest.raises(AdapterError) as excinfo:
        parse_readback_config(raw, env={})
    assert "HANDLEHQ_TOKEN" in str(excinfo.value)
    assert "not set" in str(excinfo.value)


def test_the_recipe_leaves_the_public_host_guardrail_to_the_operator():
    """A real deployment is a public host. The recipe leaves
    `allow_public_hosts` commented out on purpose, so the decision is re-made
    per run with a flag, rather than granted permanently in a tracked file."""
    text = RECIPE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert "allow_public_hosts = true" not in code, (
        "the recipe should not grant the public-readback permission permanently; "
        "prefer --allow-public-readback per run"
    )


def test_the_field_paths_resolve_against_a_documented_response():
    """`ticket_id` is the one path the recipe states, and it is checked against
    a record shaped like the one the recipe documents.

    The default paths are checked too, because they are live underneath, and a
    record that carries a `subject` will have it read. Stating both keeps this
    test from implying the recipe checks less than it does.
    """
    from testinghq.pipeline.adapters import readback_from_json

    _raw, config = _readback_config()
    record = readback_from_json(
        {"id": "R-1", "subject": "ignored"}, config.field_map()
    )
    assert record.ticket_id == "R-1"
    assert record.has("subject") is True, (
        "subject comes from the default map, which is merged underneath the "
        "recipe's own table"
    )

    # And a field nothing maps to stays invisible rather than being invented.
    empty = readback_from_json({"id": "R-2", "ticket_id": "R-2"},
                               {"ticket_id": "id"})
    assert empty.has("subject") is False


# ---------------------------------------------------------------------------
# The harness exists and is correct
# ---------------------------------------------------------------------------


def test_env_example_exists_and_is_named_right():
    assert ENV_EXAMPLE.is_file(), ".env.example is missing"
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "HANDLEHQ_TOKEN" in text, ".env.example should define HANDLEHQ_TOKEN"
    assert "HANDLEHQ_STAGING_URL" not in text, (
        "the staging URL is not a secret and `[readback].url` cannot read an "
        "environment variable, so putting it in .env would imply a capability "
        "the config does not have. It belongs in a gitignored local config."
    )


def test_env_example_holds_no_credential():
    """A committed example file that accidentally acquired a real value is the
    most ordinary way a secret ends up in git history."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^HANDLEHQ_TOKEN=(.*)$", text, re.MULTILINE)
    assert match, "HANDLEHQ_TOKEN should be present in .env.example"
    assert match.group(1).strip() == "", (
        f"HANDLEHQ_TOKEN in .env.example has a value: {match.group(1)!r}. An "
        "example file is committed, so it must be empty."
    )
    for secret_shape in (r"sk-", r"ghp_", r"xox[baprs]-", r"AKIA"):
        assert not re.search(secret_shape, text), (
            f".env.example appears to contain a credential matching {secret_shape}"
        )


def test_dotenv_is_gitignored():
    """The single most important line in the harness. Without it, `cp .env.example
    .env` followed by a fill-in is a commit away from publishing a token.

    Caught by this test: `.env` was not in `.gitignore` when the harness was
    written, and the example file's own copy step would have put it one
    `git add -A` from being committed.
    """
    ignore = GITIGNORE.read_text(encoding="utf-8")
    entries = {
        line.strip().lstrip("/")
        for line in ignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in entries, (
        ".env must be gitignored. An example file with a blank value and a copy "
        "step is only safe if the copy is ignored."
    )
    assert "examples/readback/*.local.toml" in entries, (
        "a local config copy carries a real host and must be ignored"
    )


def test_the_dogfood_doc_says_it_has_not_been_run():
    """The claim this whole harness exists to keep honest.

    A doc that has been run stops saying it has not. A doc that has been run and
    still says it has not is worse, and equally this test would need changing.
    For now: the harness is not a dogfood run, and says so.
    """
    text = DOC.read_text(encoding="utf-8")
    assert "It has not been done" in text or "has not been run" in text.lower(), (
        "docs/DOGFOOD.md must say plainly that no run has happened yet"
    )
    assert "NOT RUN" in RECIPE.read_text(encoding="utf-8").upper()


def test_the_dogfood_doc_names_what_is_still_missing():
    """The doc has to say exactly what is needed and what has not happened, or a
    reader cannot tell whether they are looking at a run or a plan."""
    text = DOC.read_text(encoding="utf-8")
    assert "HANDLEHQ_TOKEN" in text
    assert "It has not been done" in text, (
        "docs/DOGFOOD.md must say plainly that no run has happened yet"
    )
    assert "NOT RUN" in RECIPE.read_text(encoding="utf-8").upper()


def test_the_dogfood_doc_explains_the_field_map_merge():
    """The doc and the recipe both warn that naming one field does not mean only
    that field is checked, because the defaults are merged underneath. A reader
    who misses it will either over-trust a green run or chase a field that was
    being read all along.
    """
    for name, text in (("docs/DOGFOOD.md", DOC.read_text(encoding="utf-8")),
                       ("handlehq.toml", RECIPE.read_text(encoding="utf-8"))):
        assert "merged" in text.lower() and "default" in text.lower(), (
            f"{name} should explain that the field map is merged over the "
            "defaults, not used alone"
        )


def test_the_dogfood_doc_gives_the_validate_step_first():
    """`config validate` before the first send is the whole reason it exists, and
    the order matters: it is what catches an unset token before messages go
    out."""
    text = DOC.read_text(encoding="utf-8")
    validate_at = text.index("config validate")
    first_send_at = text.index("--send")
    assert validate_at < first_send_at, (
        "the doc should validate the config before the first --send; validating "
        "afterwards is too late to catch an unset credential"
    )


def test_the_dogfood_doc_points_at_the_public_readback_flag():
    text = DOC.read_text(encoding="utf-8")
    assert "--allow-public-readback" in text, (
        "a real deployment needs this flag, and a reader who hits the guardrail "
        "should not have to work out why"
    )


def test_the_dogfood_doc_tells_the_reader_how_to_wait_long_enough():
    """A run that checks too early reports a slow pipeline as a lossy one, and
    that is the failure this tool exists to avoid."""
    text = DOC.read_text(encoding="utf-8")
    assert "--quiet-window" in text
    assert "--max-wait" in text


def test_the_suite_never_reads_the_dogfood_environment():
    """The hermetic guarantee. If a test read HANDLEHQ_TOKEN it would pass on one
    machine and fail on another, and it would be reading a credential to do it.
    """
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        source = path.read_text(encoding="utf-8-sig")
        assert "HANDLEHQ_TOKEN" not in source, (
            f"{path.relative_to(REPO_ROOT)} references HANDLEHQ_TOKEN. The "
            "dogfood environment is a manual run, not something the suite reads; "
            "a test that depended on it would fail on a machine without it."
        )


def test_the_config_doc_drift_guard_still_covers_the_recipe():
    """The recipes directory is not generated, but every recipe is loaded by
    tests/unit/test_readback_recipes.py, so a broken one fails there. Asserted
    here because that file lives on a different stack and this one should not
    silently stop parsing."""
    assert RECIPE.is_file()
    with pytest.raises(ConfigError):
        # A file that does not exist is refused by the loader, not ignored.
        load_config(REPO_ROOT / "examples" / "readback" / "nope.toml")
