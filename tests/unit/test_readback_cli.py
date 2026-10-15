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
from testinghq.pipeline.common import EXIT_REFUSED, prepare_readback

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


# ---------------------------------------------------------------------------
# A custom adapter's own keys, from the file through a real verify run
# ---------------------------------------------------------------------------


class _VirtualClock:
    """A clock and a sleep that advances it, so polling costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _AcceptingClient:
    def __init__(self):
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return ClientResponse(status=200, body=b'{"ok": true}')


@pytest.fixture
def keys_module(monkeypatch):
    module = types.ModuleType("hq_cli_keys_probe")
    module.tables = []
    module.fetched = []

    class Recorder:
        def __init__(self, table):
            module.tables.append(table)

        def fetch(self, probe):
            module.fetched.append(probe.tag)
            return []

    def build(config):
        module.tables.append(config)
        return Recorder.__new__(Recorder)

    module.Recorder = Recorder
    module.build = build
    monkeypatch.setitem(sys.modules, "hq_cli_keys_probe", module)
    return module


def _verify_in_process(tmp_path, args):
    from testinghq.pipeline import verify

    clock = _VirtualClock()
    client = _AcceptingClient()
    code = verify.execute(
        7,
        2,
        TARGET,
        args.config,
        str(tmp_path / "verify.json"),
        readback=_resolve_readback(args),
        client=client,
        sleep=clock.sleep,
        clock=clock,
        printer=lambda line: None,
        quiet_window=0.0,
        max_wait=1.0,
        poll_interval=0.1,
    )
    return code, client


@pytest.mark.parametrize("attribute", ["Recorder", "build"])
def test_a_verify_run_hands_a_custom_adapter_its_own_keys(
    tmp_path, keys_module, attribute
):
    """From the TOML, through the CLI's resolution and a real (in-process)
    `verify fire --send`, to the class or factory the file names."""
    path = _config(
        tmp_path,
        f'\n[readback]\nkind = "hq_cli_keys_probe:{attribute}"\n'
        'project_id = "proj-42"\n',
    )
    code, client = _verify_in_process(tmp_path, _args("--config", path, "--send"))
    assert code != EXIT_REFUSED, "the run was refused"
    assert client.sent, "nothing was sent, so the run never got past the adapter"
    assert keys_module.fetched
    (table,) = keys_module.tables
    assert table["project_id"] == "proj-42"
    assert table["kind"] == f"hq_cli_keys_probe:{attribute}"


def test_a_readback_flag_keeps_the_files_adapter_keys(tmp_path, keys_module):
    """`--readback module:attr` says which adapter; the file still says how
    it is configured, own keys included."""
    path = _config(
        tmp_path,
        '\n[readback]\nkind = "hq_cli_keys_probe:Recorder"\n'
        'project_id = "proj-42"\n',
    )
    code, _client = _verify_in_process(
        tmp_path,
        _args("--config", path, "--send", "--readback", "hq_cli_keys_probe:build"),
    )
    assert code != EXIT_REFUSED
    (table,) = keys_module.tables
    assert table["project_id"] == "proj-42"
    assert table["kind"] == table["spec"] == "hq_cli_keys_probe:build"
