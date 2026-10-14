"""The readback seam as a tool reaches it: the shared CLI arguments, and the
one helper every tool calls to build and check its adapter before sending.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from testinghq import cli
from testinghq.cli.common import _resolve_readback
from testinghq.core.transport import ClientResponse
from testinghq.pipeline import adapters
from testinghq.pipeline.adapters import AdapterError
from testinghq.pipeline.common import prepare_readback

TARGET = "local"
RUN_PREFIX = re.compile(r"^hq-\d{10}-[0-9a-f]{4}$")


def _config(tmp_path, readback: str = "") -> str:
    path = tmp_path / "conf" / "target.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[targets.{TARGET}]\nurl = "http://127.0.0.1:8000/intake"\n{readback}',
        encoding="utf-8",
    )
    return str(path)


def _args(*argv):
    return cli.build_parser().parse_args(["verify", "fire", "--target", TARGET, *argv])


# ---------------------------------------------------------------------------
# --readback-rate, --allow-public-readback, and the config directory
# ---------------------------------------------------------------------------


def test_the_readback_rate_defaults_to_four_a_second(tmp_path):
    config = _resolve_readback(_args("--config", _config(tmp_path), "--readback", "mailbox"))
    assert config.rate == adapters.DEFAULT_READBACK_RATE == 4.0


def test_the_readback_rate_flag_reaches_the_adapter_config(tmp_path):
    args = _args("--config", _config(tmp_path), "--readback", "mailbox", "--readback-rate", "0.5")
    assert _resolve_readback(args).rate == 0.5


@pytest.mark.parametrize("value", ["0", "-1", "fast"])
def test_a_readback_rate_that_is_not_positive_is_a_usage_refusal(value):
    with pytest.raises(SystemExit) as caught:
        _args("--readback-rate", value)
    assert caught.value.code == 1


def test_the_public_readback_flag_wins_over_a_table_that_says_false(tmp_path):
    """`--readback http --allow-public-readback` with `allow_public_hosts =
    false` in the file used to keep the file's false, silently."""
    path = _config(
        tmp_path,
        '[readback]\nkind = "http"\nurl = "http://127.0.0.1:9/t"\n'
        "allow_public_hosts = false\n",
    )
    args = _args("--config", path, "--readback", "http", "--allow-public-readback")
    assert _resolve_readback(args).allow_public_hosts is True


def test_a_relative_mailbox_path_means_the_config_files_directory(tmp_path, monkeypatch):
    path = _config(tmp_path, '[readback]\nkind = "mailbox"\npath = "sink.jsonl"\n')
    monkeypatch.chdir(tmp_path)
    config = _resolve_readback(_args("--config", path))
    adapter = adapters.build_adapter(config)
    assert adapter.path == Path(path).resolve().parent / "sink.jsonl"


# ---------------------------------------------------------------------------
# The per-run tag prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["verify", "ledger", "redeliver", "loop", "steady"])
def test_every_sending_tool_defaults_to_a_per_run_tag_prefix(tool):
    args = cli.build_parser().parse_args([tool, "fire", "--target", TARGET])
    assert RUN_PREFIX.match(args.tag_prefix), args.tag_prefix


def test_tag_prefix_still_pins_a_fixed_prefix():
    assert _args("--tag-prefix", "hq").tag_prefix == "hq"


def test_the_dry_run_names_the_prefix_this_run_will_use(tmp_path, capsys):
    code = cli.main(
        ["verify", "fire", "--target", TARGET, "--config", _config(tmp_path),
         "--readback", "mailbox", "--count", "2"]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert re.search(r"tag prefix 'hq-\d{10}-[0-9a-f]{4}'", out), out


# ---------------------------------------------------------------------------
# prepare_readback: build, resolve, preflight, before anything is sent
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_module(monkeypatch):
    module = types.ModuleType("hq_prepare_spec")
    monkeypatch.setitem(sys.modules, "hq_prepare_spec", module)
    return module


def test_prepare_readback_runs_preflight_once(spec_module):
    calls = []

    class Adapter:
        def preflight(self):
            calls.append("preflight")

        def fetch(self, probe):
            return []

    spec_module.Adapter = Adapter
    adapter = prepare_readback("hq_prepare_spec:Adapter")
    assert isinstance(adapter, Adapter)
    assert calls == ["preflight"]


def test_a_failing_preflight_is_raised_and_the_adapter_closed(spec_module):
    closed = []

    class Adapter:
        def preflight(self):
            raise AdapterError("credentials refused")

        def fetch(self, probe):
            return []

        def close(self):
            closed.append(True)

    spec_module.Adapter = Adapter
    with pytest.raises(AdapterError, match="credentials refused"):
        prepare_readback("hq_prepare_spec:Adapter")
    assert closed == [True]


def test_preflight_can_be_skipped_for_a_dry_run(spec_module):
    class Adapter:
        def preflight(self):  # pragma: no cover - must not run
            raise AssertionError("preflight ran on a dry run")

        def fetch(self, probe):
            return []

    spec_module.Adapter = Adapter
    prepare_readback("hq_prepare_spec:Adapter", preflight=False)


def test_a_spec_that_cannot_be_imported_is_refused_by_the_helper():
    with pytest.raises(AdapterError, match="could not import"):
        prepare_readback("hq_no_such_module_anywhere:build")


def test_prepare_readback_resolves_a_mailbox_path_against_the_config_file(tmp_path):
    config_path = tmp_path / "conf" / "target.toml"
    config_path.parent.mkdir()
    adapter = prepare_readback(
        {"kind": "mailbox", "path": "sink.jsonl"}, config_path=str(config_path)
    )
    assert adapter.path == config_path.resolve().parent / "sink.jsonl"


def test_prepare_readback_refuses_a_sink_in_a_missing_directory(tmp_path):
    with pytest.raises(AdapterError, match="does not exist"):
        prepare_readback(
            {"kind": "mailbox", "path": "nowhere/sink.jsonl"},
            config_dir=str(tmp_path),
        )


def test_prepare_readback_refuses_rejected_credentials_before_sending():
    class Refusing:
        def __init__(self):
            self.requests = []

        def send(self, request):
            self.requests.append(request.url)
            return ClientResponse(status=401, body=b'{"error": "unauthorized"}')

    client = Refusing()
    with pytest.raises(AdapterError, match="401"):
        prepare_readback(
            {"kind": "http", "url": "http://127.0.0.1:9/tickets"}, client=client
        )
    assert len(client.requests) == 1
