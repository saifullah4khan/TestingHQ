"""Unknown config keys are refused, never ignored.

A key nothing reads is the worst thing a config file can hold: `formt =
"mailgun"` loads, `config validate` passes, and every message goes out as
SendGrid. These tests pin the refusal through the loader, the tool-table
reader, `config validate` and a real `--send`, and pin the three places that
stay open on purpose: `[readback.fields]` and `[readback.headers]` names, and a
custom adapter's own keys.
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from testinghq import cli, config_doc
from testinghq.core import config_schema
from testinghq.core.config import ConfigError, load_config, unknown_key_problems
from testinghq.pipeline.adapters import parse_readback_config
from testinghq.pipeline.loop import LoopConfigError, load_loop_config
from testinghq.pipeline.sigcheck import SigcheckError, load_sigcheck_config
from testinghq.pipeline.steady import SteadyConfigError, load_steady_config

REPO_ROOT = Path(__file__).resolve().parents[2]

TARGET = '[targets.a]\nurl = "http://127.0.0.1:8000/intake"\n'


def _write(tmp_path, body: str, name: str = "target.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The loader refuses, naming the file, the table, the key and the suggestion
# ---------------------------------------------------------------------------


def test_a_typo_in_a_target_table_is_refused_with_the_closest_key(tmp_path):
    path = _write(tmp_path, TARGET + 'formt = "mailgun"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "[targets.a]" in message
    assert "'formt'" in message
    assert "did you mean 'format'?" in message


def test_every_unknown_key_in_the_file_is_named_at_once(tmp_path):
    body = (
        TARGET + 'formt = "mailgun"\npublik = true\n'
        '[readback]\nkind = "http"\nurl = "http://127.0.0.1:1/t"\nenumerat = false\n'
    )
    problems = unknown_key_problems(
        {"targets": {"a": {"url": "x", "formt": 1, "publik": 1}},
         "readback": {"kind": "http", "enumerat": False}}
    )
    assert problems == [
        "[targets.a] has unknown key 'formt' (did you mean 'format'?)",
        "[targets.a] has unknown key 'publik' (did you mean 'public'?)",
        "[readback] has unknown key 'enumerat' (did you mean 'enumerate'?)",
    ]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


def test_a_key_with_nothing_close_lists_the_known_keys(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, TARGET + "zzz = 1\n"))
    assert "known keys: format, name, public, url" in str(caught.value)


@pytest.mark.parametrize(
    "body, table, key, suggestion",
    [
        ('[readback]\nkind = "http"\nurl = "http://127.0.0.1:1/t"\ntimout = 3\n',
         "[readback]", "timout", "timeout"),
        ('[loop]\nreply_adress = "a@example.com"\n', "[loop]", "reply_adress", "reply_address"),
        ('[loop.outbound]\nkind = "mailbox"\npaht = "x.jsonl"\n',
         "[loop.outbound]", "paht", "path"),
        ('[steady]\nlabel_feild = "route"\n', "[steady]", "label_feild", "label_field"),
        ('[sigcheck]\nschme = "mailgun"\n', "[sigcheck]", "schme", "scheme"),
    ],
)
def test_every_schema_table_refuses_an_unknown_key(tmp_path, body, table, key, suggestion):
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, TARGET + body))
    message = str(caught.value)
    assert f"{table} has unknown key {key!r}" in message
    assert f"did you mean {suggestion!r}?" in message


def test_an_unknown_top_level_table_is_refused(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, '[target.a]\nurl = "http://127.0.0.1/"\n'))
    message = str(caught.value)
    assert "unknown table [target]" in message
    assert "did you mean [targets]?" in message


def test_an_unknown_top_level_key_is_refused(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, 'verbose = true\n' + TARGET))
    assert "unknown top-level key 'verbose'" in str(caught.value)


# ---------------------------------------------------------------------------
# The tool tables refuse the same way, on a dry run too
# ---------------------------------------------------------------------------


def test_the_tool_table_readers_refuse_an_unknown_key(tmp_path):
    loop = _write(tmp_path, TARGET + '[loop]\nticket_polcy = "allowed"\n', "loop.toml")
    with pytest.raises(LoopConfigError) as caught:
        load_loop_config(str(loop), required=False)
    assert "did you mean 'ticket_policy'?" in str(caught.value)

    steady = _write(tmp_path, TARGET + '[steady]\nintent_path = "x.json"\n', "steady.toml")
    with pytest.raises(SteadyConfigError) as caught:
        load_steady_config(str(steady))
    assert "did you mean 'intents_path'?" in str(caught.value)

    sig = _write(tmp_path, TARGET + "[sigcheck]\nwindow = 30\n", "sig.toml")
    with pytest.raises(SigcheckError) as caught:
        load_sigcheck_config(str(sig), required=False)
    assert "[sigcheck] has unknown key 'window'" in str(caught.value)


# ---------------------------------------------------------------------------
# config validate reports the same error
# ---------------------------------------------------------------------------


def test_config_validate_refuses_a_typo_with_the_suggestion(tmp_path, capsys):
    path = _write(tmp_path, TARGET + 'formt = "mailgun"\n')
    with pytest.raises(config_doc.ValidationError):
        config_doc.validate(path)

    code = cli.main(["config", "validate", str(path)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("config validate: ")
    assert str(path) in err
    assert "[targets.a] has unknown key 'formt' (did you mean 'format'?)" in err


# ---------------------------------------------------------------------------
# A real --send refuses before anything reaches the endpoint
# ---------------------------------------------------------------------------


class _Sink(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):  # noqa: N802 - stdlib name
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def sink():
    _Sink.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], _Sink.hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("tool", ["blast", "verify"])
def test_a_send_with_a_typo_key_exits_1_and_sends_nothing(tmp_path, capsys, sink, tool):
    port, hits = sink
    path = _write(
        tmp_path,
        f'[targets.a]\nurl = "http://127.0.0.1:{port}/intake"\nformt = "mailgun"\n'
        f'[readback]\nkind = "mailbox"\npath = "{tmp_path / "mail.jsonl"}"\n',
    )
    code = cli.main([tool, "fire", "--config", str(path), "--target", "a",
                     "--count", "2", "--send"])
    err = capsys.readouterr().err
    assert code == 1
    assert "did you mean 'format'?" in err
    assert hits == [], "a request reached the endpoint before the refusal"


# ---------------------------------------------------------------------------
# What stays open on purpose
# ---------------------------------------------------------------------------


def test_header_names_are_data_and_stay_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HQ_T", "v")
    body = TARGET + (
        '[readback]\nkind = "http"\nurl = "http://127.0.0.1:1/t"\n'
        '[readback.headers]\nX-Anything-At-All = "env:HQ_T"\n'
    )
    config = load_config(_write(tmp_path, body))
    assert config.readback_table()["headers"] == {"X-Anything-At-All": "env:HQ_T"}


def test_a_custom_adapter_with_its_own_key_still_loads(tmp_path):
    body = TARGET + (
        '[readback]\nkind = "mypkg.adapters:build"\ndsn = "postgres://localhost/hq"\n'
        "poll_batch = 50\n"
        '[loop.outbound]\nkind = "mypkg.adapters:outbox"\nqueue = "replies"\n'
    )
    path = _write(tmp_path, body)
    config = load_config(path)
    assert config.readback_table()["dsn"] == "postgres://localhost/hq"
    assert parse_readback_config(config.readback_table(), env={}).spec == (
        "mypkg.adapters:build"
    )
    _targets, readback, _notes = config_doc.validate(path)
    assert readback["kind"] == "mypkg.adapters:build"
    assert load_loop_config(str(path))["outbound"]["queue"] == "replies"


def test_a_custom_adapter_named_by_spec_also_keeps_its_keys(tmp_path):
    body = TARGET + '[readback]\nkind = "http"\nspec = "mypkg.adapters:build"\ndsn = "x"\n'
    assert load_config(_write(tmp_path, body)).readback_table()["dsn"] == "x"


def test_a_built_in_kind_does_not_get_the_custom_adapter_allowance(tmp_path):
    body = TARGET + '[readback]\nkind = "mailbox"\npath = "x.jsonl"\ndsn = "x"\n'
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, body))
    assert "[readback] has unknown key 'dsn'" in str(caught.value)


# ---------------------------------------------------------------------------
# Every shipped config still loads, and the whitelist matches what is read
# ---------------------------------------------------------------------------


def _shipped_configs():
    found = sorted(REPO_ROOT.glob("examples/*.toml"))
    found += sorted(REPO_ROOT.glob("examples/readback/*.toml"))
    found += sorted(REPO_ROOT.glob("tests/fixtures/**/*.toml"))
    return found


def test_there_are_shipped_configs_to_check():
    names = {p.name for p in _shipped_configs()}
    assert "target.example.toml" in names
    assert "zendesk.toml" in names


@pytest.mark.parametrize("path", _shipped_configs(), ids=lambda p: p.name)
def test_every_shipped_config_loads(path):
    config = load_config(path)
    assert config.targets
    table = config.readback_table()
    if table is not None:
        # The header variables a recipe names, bound to a placeholder in a
        # private env mapping: the real environment is never read.
        env = {
            reference.split(":", 1)[1]: "placeholder"
            for reference in (table.get("headers") or {}).values()
        }
        parse_readback_config(table, env=env)
    load_loop_config(str(path))
    load_steady_config(str(path))
    load_sigcheck_config(str(path))


def test_the_whitelist_is_exactly_what_the_tools_read():
    """The schema is now a whitelist, so a key a tool reads but the schema
    omits would be refused. Pinned by hand per table: the readback keys are
    pinned against the loader in test_config_schema.py."""
    expected = {
        "targets": ["format", "name", "public", "url"],
        "loop": ["outbound", "reply_address", "ticket_policy"],
        "steady": ["intents_path", "label_field"],
        "sigcheck": ["scheme", "secret", "timestamp_window_seconds"],
    }
    for name, keys in expected.items():
        assert config_schema.known_keys(config_schema.table_named(name)) == keys
    readback = config_schema.known_keys(config_schema.READBACK)
    assert "fields" in readback and "headers" in readback
    assert config_schema.known_keys(config_schema.LOOP_OUTBOUND) == readback
    assert config_schema.top_level_names() == [
        "targets", "readback", "loop", "steady", "sigcheck",
    ]
