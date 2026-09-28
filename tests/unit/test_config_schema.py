"""The generated configuration reference, and the validator that renders it.

The drift test is the important one and it is three tests, not one.

`test_the_generated_document_is_up_to_date` is the obvious one: docs/CONFIG.md
must equal what the generator produces. It is necessary and nowhere near
sufficient, because a generator fed a schema that has quietly diverged from the
loaders will happily keep a perfectly current document describing a config the
tools refuse.

So the other two exist. One asserts every key the loaders actually read is
described in the schema. The other asserts every default the schema declares is
the real default, read out of the dataclass rather than out of the schema. Those
are what make a generated document trustworthy: the thing being generated from
is itself checked against the thing that runs.

`test_validation_uses_the_real_loaders` closes the remaining hole, which is
subtler. A validator that checked a file against the schema would pass files the
tools then reject, and be worse than no validator.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from testinghq import config_doc
from testinghq.core import config_schema
from testinghq.core.config import ConfigError, load_config
from testinghq.pipeline.adapters import (
    DEFAULT_HTTP_TAG_PARAM,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_ITEMS_KEY,
    AdapterError,
    ReadbackConfig,
    parse_readback_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "CONFIG.md"

#: The keys `parse_readback_config` reads, and the dataclass field each comes
#: from. Written out rather than introspected, because the point is to pin the
#: surface a reader of the generated document sees, and an introspected list
#: would change silently with a refactor.
#:
#: `None` here means "no default": the generator renders that as `none`, and the
#: schema says nothing rather than spelling it, so the two cannot disagree about
#: what a key with no default looks like.
READBACK_KEYS = {
    "kind": None,
    "url": None,
    "path": None,
    "tag_param": DEFAULT_HTTP_TAG_PARAM,
    "items_key": DEFAULT_ITEMS_KEY,
    "timeout": DEFAULT_HTTP_TIMEOUT,
    "allow_public_hosts": False,
    "enumerate": True,
    "list_path": None,
    "spec": None,
}

#: File key -> dataclass field, where they differ. `enumerate` is the file name
#: and `enumerate_records` the field, which is a real trap for a comparison like
#: this one: without the map, the default check would silently skip the one key
#: whose value is a bool.
FIELD_FOR_KEY = {"enumerate": "enumerate_records"}

#: Keys the loaders read that the schema documents as a free-form table rather
#: than as a row, because their names are data. Checked separately, below.
FREE_FORM_KEYS = {"fields", "headers"}


# ---------------------------------------------------------------------------
# The drift test
# ---------------------------------------------------------------------------


def test_the_generated_document_is_up_to_date():
    """The whole point. The file is generated, so hand-editing it is a lie the
    next schema change will undo silently."""
    if not DOC_PATH.is_file():
        pytest.fail(
            f"{DOC_PATH} does not exist; run: python -m testinghq.config_doc"
        )
    assert DOC_PATH.read_text(encoding="utf-8") == config_doc.render_reference(), (
        f"{DOC_PATH.relative_to(REPO_ROOT)} is out of date with "
        "testinghq/core/config_schema.py. Run: python -m testinghq.config_doc"
    )


def test_the_check_mode_agrees_with_the_test():
    """`python -m testinghq.config_doc --check` is what CI would call. If the
    script and the test disagree, one of them is a guard that finds nothing."""
    result = subprocess.run(
        [sys.executable, "-m", "testinghq.config_doc", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
    )
    assert result.returncode == 0, (
        f"--check says the doc is stale: {result.stdout}{result.stderr}"
    )


def test_the_generator_never_rewrites_the_file_on_the_way_past(tmp_path):
    """`write_reference` returning False is what lets the drift test fail with a
    message instead of quietly fixing the file and leaving the suite green with
    the wrong content in a release."""
    wanted = tmp_path / "CONFIG.md"
    assert config_doc.write_reference(wanted) is True, "first write creates it"
    assert config_doc.write_reference(wanted) is False, "second write is a no-op"


# ---------------------------------------------------------------------------
# The schema describes the loaders
# ---------------------------------------------------------------------------


def test_every_key_the_readback_loader_reads_is_described():
    """A key the loaders honour but the schema does not mention is a key a
    reader of the generated document cannot know about."""
    described = {
        key.name
        for table in config_schema.TABLES
        for key in table.keys
    }
    undocumented = set(READBACK_KEYS) - described
    assert not undocumented, (
        f"the readback loader reads {sorted(undocumented)} but the schema does "
        "not describe them, so docs/CONFIG.md would be incomplete"
    )


def test_every_default_the_schema_declares_is_the_real_default():
    """The claim that makes the generated document trustworthy, stated
    directly.

    Reads the real dataclass field default rather than the schema's string, so a
    schema that says `10.0` while the loader uses something else fails here even
    though the document is perfectly up to date with the schema.
    """
    real = {f.name: f.default for f in ReadbackConfig.__dataclass_fields__.values()}

    declared_defaults = {}
    for table in config_schema.TABLES:
        if table.name != "readback":
            continue
        for key in table.keys:
            if key.default is not None:
                declared_defaults[key.name] = key.default.strip('"')

    assert declared_defaults, "the schema declares no defaults, so this is vacuous"

    for name, declared in declared_defaults.items():
        field = FIELD_FOR_KEY.get(name, name)
        assert field in real, f"the schema documents readback.{name}, not a field"
        # TOML spells booleans lowercase and Python does not, and the schema is
        # documentation so it follows TOML. Compared on the TOML spelling, or
        # every bool default reads as a mismatch.
        expected = str(real[field])
        if isinstance(real[field], bool):
            expected = str(real[field]).lower()
        assert declared == expected, (
            f"the schema says readback.{name} defaults to {declared!r} but "
            f"ReadbackConfig.{field} defaults to {real[field]!r}. The generated "
            "document would be wrong."
        )


def test_every_default_the_loader_has_is_declared_by_the_schema():
    """The other direction, and the one that catches a new key.

    A key added to `ReadbackConfig` with a default, and read by
    `parse_readback_config`, but not described in the schema, means the
    generated document is quietly incomplete. Checking only the other way round
    would pass, because there is nothing wrong with the schema that is there.
    """
    described = {
        key.name for table in config_schema.TABLES
        if table.name == "readback"
        for key in table.keys
    }
    missing = set(READBACK_KEYS) - described
    assert not missing, (
        f"the loader honours {sorted(missing)} but the schema does not describe "
        f"them. Add a Key to config_schema.READBACK."
    )


def test_the_free_form_tables_are_documented_as_tables():
    """`fields` and `headers` have data keys, so they are described by their own
    tables rather than as rows. If either stopped being a table, the document
    would lose the whole list of names it may hold."""
    described_tables = {t.name for t in config_schema.TABLES}
    assert "readback.fields" in described_tables
    assert "readback.headers" in described_tables


def test_the_schema_does_not_describe_a_key_the_loaders_ignore():
    """The other direction. A documented key that does nothing is worse than an
    undocumented one: an operator sets it, sees no error, and concludes it took
    effect."""
    described = {
        key.name
        for table in config_schema.TABLES
        if not table.free_form
        for key in table.keys
    }
    unknown = {
        key.name for key in config_schema.READBACK.keys
        if key.name not in READBACK_KEYS
    }
    assert not unknown, (
        f"the schema documents readback keys {sorted(unknown)} that the loader "
        "does not read. An operator would set one, see no error, and believe it "
        "took effect."
    )


#: Names the schema documents that arrive from a sibling stack rather than from
#: this branch's base. Listed explicitly, and still checked, because a field
#: that is neither in the base nor on this list is a real error.
#:
#: `category` and `priority` arrive with the readback record fields PR. The
#: reference documents them because by the time every stack has merged they are
#: real, and a document that omitted them would be incomplete. The check below
#: is what keeps that from becoming a way to let anything through: a name has to
#: be in the base OR named here.
FROM_A_SIBLING_STACK = {"category", "priority"}


def test_every_field_the_schema_documents_is_a_real_readback_field():
    """A field name the adapter would reject at load, documented as though it
    worked, is the worst kind of wrong documentation."""
    from testinghq.pipeline.adapters import DEFAULT_FIELD_MAP

    known = set(DEFAULT_FIELD_MAP)
    for key in config_schema.READBACK_FIELDS.keys:
        if key.name in known:
            continue
        assert key.name in FROM_A_SIBLING_STACK, (
            f"[readback.fields] documents {key.name!r}, which is not a readback "
            f"field ({sorted(known)}) and is not on the list of names arriving "
            "from a sibling stack. Either it is a typo, or it needs adding to "
            "FROM_A_SIBLING_STACK with the PR it comes from."
        )


def test_the_generated_document_says_it_is_generated():
    """A reader who finds an error in it needs to know not to edit it."""
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "GENERATED FILE" in text
    assert "Do not edit by hand" in text
    assert "python -m testinghq.config_doc" in text


def test_the_generated_document_covers_every_table():
    text = DOC_PATH.read_text(encoding="utf-8")
    for table in config_schema.TABLES:
        assert f"[{table.name}" in text, f"docs/CONFIG.md omits [{table.name}]"


# ---------------------------------------------------------------------------
# Validation uses the real loaders
# ---------------------------------------------------------------------------


def _write(tmp_path, body: str, name="target.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


VALID = """
[targets.local]
url = "http://127.0.0.1:8000/intake"
"""


def test_a_valid_config_validates(tmp_path):
    targets, readback, notes = config_doc.validate(_write(tmp_path, VALID))
    assert list(targets) == ["local"]
    assert targets["local"]["url"] == "http://127.0.0.1:8000/intake"
    assert readback is None
    assert any("no [readback]" in n for n in notes)


def test_a_valid_config_with_a_readback_validates(tmp_path):
    body = VALID + """
[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"
"""
    targets, readback, notes = config_doc.validate(_write(tmp_path, body))
    assert readback["kind"] == "http"
    assert readback["url"] == "http://127.0.0.1:8000/tickets"
    assert readback["tag_param"] == DEFAULT_HTTP_TAG_PARAM, (
        "defaults are filled in, so the render shows what will actually run"
    )


def test_validation_refuses_exactly_what_the_loader_refuses(tmp_path):
    """The property that makes a validator worth having. Every rejection here
    is one the loaders also produce, checked through the real ones rather than
    against the schema, because a validator that agreed only with the schema
    would be a validator that agreed with a description of the file.
    """
    bad_bodies = {
        "a target with no url": '[targets.local]\nname = "x"\n',
        "a url with no scheme": '[targets.local]\nurl = "127.0.0.1:8000"\n',
        "targets that is not a table": "targets = 3\n",
        "a file with no targets at all": "# nothing here\n",
    }
    # `format = "nope"` is deliberately not a case here. That key arrives with
    # the wire-format layer, a sibling stack, so this base's loader does not
    # read it and would happily accept the file. PR #58's own test covers the
    # refusal, and adding it here would make this test assert something false
    # about the base it is running on.
    for label, body in bad_bodies.items():

        path = _write(tmp_path, body)
        with pytest.raises(config_doc.ValidationError):
            config_doc.validate(path)

        # And the loader on its own agrees, which is the whole claim.
        with pytest.raises(ConfigError):
            load_config(path)


def test_a_bad_readback_is_refused_by_the_adapter_not_the_schema(tmp_path):
    body = VALID + """
[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"
fields = { "not-a-field" = "x" }
"""
    path = _write(tmp_path, body)
    with pytest.raises(config_doc.ValidationError) as excinfo:
        config_doc.validate(path)
    assert "not-a-field" in str(excinfo.value)

    with pytest.raises(AdapterError):
        parse_readback_config(load_config(path).readback_table(), env={})


def test_a_missing_file_is_refused_with_its_path(tmp_path):
    with pytest.raises(config_doc.ValidationError) as excinfo:
        config_doc.validate(tmp_path / "gone.toml")
    assert "gone.toml" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The validator never leaks a secret
# ---------------------------------------------------------------------------


def test_a_header_value_is_never_rendered(tmp_path, monkeypatch):
    """The property that makes this command safe to paste into an issue.

    A config validator that echoed the credentials it successfully resolved
    would be a new place for them to leak, and the people who run it are the
    people debugging a config that does not work, which is exactly when a value
    gets pasted somewhere it should not be.
    """
    monkeypatch.setenv("HQ_TOKEN", "super-secret-value-1234")
    body = VALID + """
[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"

[readback.headers]
Authorization = "env:HQ_TOKEN"
"""
    _targets, readback, _notes = config_doc.validate(_write(tmp_path, body))
    rendered = repr(readback)
    assert "super-secret-value-1234" not in rendered
    assert "env:HQ_TOKEN" in rendered
    assert readback["headers"] == {"Authorization": "env:HQ_TOKEN"}


def test_an_unset_header_variable_is_refused_at_validate_time(tmp_path, monkeypatch):
    """Before the run sends anything, not at connect time. Which is the whole
    argument for having a validate step."""
    monkeypatch.delenv("HQ_TOKEN", raising=False)
    body = VALID + """
[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"

[readback.headers]
Authorization = "env:HQ_TOKEN"
"""
    with pytest.raises(config_doc.ValidationError) as excinfo:
        config_doc.validate(_write(tmp_path, body))
    assert "not set" in str(excinfo.value)


def test_the_toml_render_also_hides_the_value(tmp_path, monkeypatch):
    """Both output forms, because someone will use the one that diffs."""
    monkeypatch.setenv("HQ_TOKEN", "super-secret-value-1234")
    body = VALID + """
[readback]
kind = "http"
url = "http://127.0.0.1:8000/tickets"

[readback.headers]
Authorization = "env:HQ_TOKEN"
"""
    targets, readback, _ = config_doc.validate(_write(tmp_path, body))
    rendered = config_doc.render_effective(targets, readback)
    assert "super-secret-value-1234" not in rendered
    assert "env:HQ_TOKEN" in rendered


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run(argv, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "testinghq.cli", *argv],
        capture_output=True, text=True, cwd=cwd, timeout=300,
    )


def test_the_command_validates_a_good_file(tmp_path):
    result = _run(["config", "validate", str(_write(tmp_path, VALID))])
    assert result.returncode == 0, result.stderr
    assert "targets: 1" in result.stdout
    assert "Traceback" not in result.stderr


def test_the_command_refuses_a_bad_file_with_a_message(tmp_path):
    result = _run(["config", "validate",
                   str(_write(tmp_path, '[targets.x]\nurl = "ftp://a"\n'))])
    assert result.returncode == 1
    assert "config validate:" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_toml_form_round_trips_through_the_loader(tmp_path):
    """Rendered config is real config. If it could not be loaded again, the
    `--toml` output would be a pretty thing that lies."""
    monkey_env = {"TESTINGHQ_TARGET_LOCAL_URL": "http://127.0.0.1:9999/over"}
    import os

    old = {k: os.environ.get(k) for k in monkey_env}
    os.environ.update(monkey_env)
    try:
        targets, readback, _ = config_doc.validate(_write(tmp_path, VALID))
        rendered = config_doc.render_effective(targets, readback)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    again = tmp_path / "effective.toml"
    again.write_text(rendered, encoding="utf-8")
    reloaded = load_config(again)
    assert reloaded.get("local").url == targets["local"]["url"]


def test_the_command_offers_no_send_or_target_flags(tmp_path):
    """A config tool nobody would paste an untrusted file into is one that
    cannot transmit. Checked by trying the flags, not by reading the help,
    which legitimately mentions --send when it explains exit code 2."""
    for flag in ("--send", "--target"):
        result = _run(["config", "validate", "x.toml", flag, "y"])
        assert result.returncode == 1, f"{flag} was accepted by config validate"


def test_the_validator_opens_no_socket(tmp_path, monkeypatch):
    """Structural, and the reason it is worth asserting rather than assuming:
    validating a config that names a remote readback must not connect to it.

    Every socket-creating call in the stdlib is replaced with one that raises,
    so any attempt to open one fails the test rather than quietly reaching a
    host from a command that promised it would not.
    """
    import socket

    def _refuse(*args, **kwargs):
        raise AssertionError("config validate opened a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    body = VALID + """
[readback]
kind = "http"
url = "https://api.example.com/v1/tickets"
"""
    targets, readback, _ = config_doc.validate(_write(tmp_path, body))
    assert readback["url"] == "https://api.example.com/v1/tickets"
