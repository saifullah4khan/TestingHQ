"""Every send path fires in the target's configured format, and honours the
public-target gate.

One case per tool: blast, barrage, verify, ledger, loop, redeliver, steady. Each
runs its real send path against a fake HTTP client, with a target configured as
`format = "mailgun"`, and asserts every request on the wire is Mailgun-shaped
and the artifact records the format. DNS is faked, so the public-host cases
never resolve a real name.
"""
from __future__ import annotations

import json
import socket

import pytest

from testinghq.barrage import fire as barrage_fire
from testinghq.cli import blast as blast_cli
from testinghq.core import guardrails
from testinghq.core.exit_codes import EXIT_REFUSED
from testinghq.core.transport import ClientResponse
from testinghq.pipeline import ledger, loop, redeliver, steady, verify
from testinghq.pipeline.adapters import ReadbackConfig

PUBLIC_HOST = "intake.mycompany.com"


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.0)


class _Client:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=200, body=b"{}")


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        if host == PUBLIC_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guardrails.clear_dns_cache()
    yield
    guardrails.clear_dns_cache()


def _config(tmp_path, url="http://127.0.0.1:9/intake", extra=""):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.t]\nurl = "{url}"\nformat = "mailgun"\n{extra}', encoding="utf-8"
    )
    return str(path)


def _readback(tmp_path):
    return ReadbackConfig(kind="mailbox", path=str(tmp_path / "sink.jsonl"))


def _pipeline_kwargs(tmp_path, client, clock):
    return dict(
        readback=_readback(tmp_path),
        client=client,
        sleep=clock.sleep,
        clock=clock,
        printer=lambda _line: None,
        quiet_window=0.0,
        max_wait=0.0,
        poll_interval=0.05,
    )


def _run_blast(tmp_path, client, clock, config, out, **flags):
    pairs = blast_cli._build_corpus(1, 4)
    return blast_cli._run_fire(
        pairs, 1, 4, "t", 1000.0, out, config, client=client, **flags
    )


def _run_barrage(tmp_path, client, clock, config, out, **flags):
    plan = barrage_fire.build_plan("open", 5.0, 1.0, 1, 0.0)
    return barrage_fire.execute(
        plan, 1, 3, "t", config, out, client=client, clock=clock,
        sleep=clock.sleep, printer=lambda _line: None, **flags,
    )


def _run_verify(tmp_path, client, clock, config, out, **flags):
    return verify.execute(1, 3, "t", config, out,
                          **_pipeline_kwargs(tmp_path, client, clock), **flags)


def _run_ledger(tmp_path, client, clock, config, out, **flags):
    return ledger.execute(1, 3, "t", config, out,
                          **_pipeline_kwargs(tmp_path, client, clock), **flags)


def _run_loop(tmp_path, client, clock, config, out, **flags):
    return loop.execute(1, 3, "t", config, out, outbound=None, tag_prefix="hq-x",
                        rate=1000.0, **_pipeline_kwargs(tmp_path, client, clock),
                        **flags)


def _run_redeliver(tmp_path, client, clock, config, out, **flags):
    return redeliver.execute(1, 1, "t", config, out, retry_after=0.0,
                             **_pipeline_kwargs(tmp_path, client, clock), **flags)


def _run_steady(tmp_path, client, clock, config, out, **flags):
    return steady.execute(1, 1, "t", config, out,
                          **_pipeline_kwargs(tmp_path, client, clock), **flags)


RUNNERS = {
    "blast": _run_blast,
    "barrage": _run_barrage,
    "verify": _run_verify,
    "ledger": _run_ledger,
    "loop": _run_loop,
    "redeliver": _run_redeliver,
    "steady": _run_steady,
}


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_every_request_is_in_the_configured_format(tmp_path, tool):
    client, clock = _Client(), _Clock()
    out = tmp_path / "run.json"
    RUNNERS[tool](tmp_path, client, clock, _config(tmp_path), str(out))

    assert client.requests, f"{tool} sent nothing"
    for request in client.requests:
        assert b'name="body-plain"' in request.body, f"{tool} did not send mailgun"
        assert b'name="headers"' not in request.body, f"{tool} sent sendgrid fields"
    config = json.loads(out.read_text(encoding="utf-8"))["config"]
    assert config["format"] == "mailgun"
    assert config["allow_public_target"] is False


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_a_public_target_is_refused_without_the_flag(tmp_path, tool, capsys):
    client, clock = _Client(), _Clock()
    config = _config(tmp_path, url=f"https://{PUBLIC_HOST}/intake")
    try:
        code = RUNNERS[tool](tmp_path, client, clock, config, None)
    except guardrails.GuardrailError:
        code = EXIT_REFUSED  # barrage raises; its CLI turns that into exit 1
    assert code == EXIT_REFUSED
    assert client.requests == []


@pytest.mark.parametrize("tool", sorted(set(RUNNERS) - {"barrage"}))
def test_a_public_target_is_fired_at_with_the_flag(tmp_path, tool):
    client, clock = _Client(), _Clock()
    out = tmp_path / "run.json"
    config = _config(tmp_path, url=f"https://{PUBLIC_HOST}/intake")
    RUNNERS[tool](tmp_path, client, clock, config, str(out), allow_public_target=True)
    assert client.requests
    assert json.loads(out.read_text(encoding="utf-8"))["config"]["allow_public_target"] is True


def test_barrage_at_a_public_target_needs_the_flag_and_the_config_key(tmp_path):
    client, clock = _Client(), _Clock()
    flag_only = _config(tmp_path, url=f"https://{PUBLIC_HOST}/intake")
    with pytest.raises(guardrails.GuardrailError, match="public = true"):
        _run_barrage(tmp_path, client, clock, flag_only, None, allow_public_target=True)
    assert client.requests == []

    out = tmp_path / "run.json"
    both = _config(tmp_path, url=f"https://{PUBLIC_HOST}/intake", extra="public = true\n")
    _run_barrage(tmp_path, client, clock, both, str(out), allow_public_target=True)
    assert client.requests
    assert json.loads(out.read_text(encoding="utf-8"))["config"]["allow_public_target"] is True


# ---------------------------------------------------------------------------
# SMTP targets and target headers, through the same seven send paths
# ---------------------------------------------------------------------------


class _SmtpClient:
    """An SmtpClient seam that accepts every message without a socket."""

    def __init__(self):
        self.requests = []

    def send(self, request):
        from testinghq.core.transport import SmtpReply

        self.requests.append(request)
        return SmtpReply(250, "2.0.0 Ok: queued")


def _smtp_config(tmp_path, host="127.0.0.1", extra=""):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.t]\ntransport = "smtp"\nhost = "{host}"\nport = 2525\n{extra}',
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_every_message_to_an_smtp_target_goes_over_smtp(tmp_path, tool):
    from email import message_from_bytes

    client, clock = _SmtpClient(), _Clock()
    out = tmp_path / "run.json"
    RUNNERS[tool](tmp_path, client, clock, _smtp_config(tmp_path), str(out))

    assert client.requests, f"{tool} sent nothing"
    for request in client.requests:
        assert request.endpoint.host == "127.0.0.1" and request.endpoint.port == 2525
        assert request.envelope_to, f"{tool} sent a message with no recipient"
        message = message_from_bytes(request.message)
        assert message["Subject"] is not None and message["From"] is not None
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["config"]["transport"] == "smtp"
    assert artifact["config"]["format"] == "mime"


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_an_http_artifact_records_its_transport(tmp_path, tool):
    client, clock = _Client(), _Clock()
    out = tmp_path / "run.json"
    RUNNERS[tool](tmp_path, client, clock, _config(tmp_path), str(out))
    assert json.loads(out.read_text(encoding="utf-8"))["config"]["transport"] == "http"


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_target_headers_reach_every_request_and_no_artifact(tmp_path, tool, monkeypatch):
    monkeypatch.setenv("HQ_TEST_TARGET_AUTH", "Basic c2VjcmV0LXRva2Vu")
    client, clock = _Client(), _Clock()
    out = tmp_path / "run.json"
    config = _config(
        tmp_path, extra='\n[targets.t.headers]\nAuthorization = "env:HQ_TEST_TARGET_AUTH"\n'
    )
    RUNNERS[tool](tmp_path, client, clock, config, str(out))

    assert client.requests, f"{tool} sent nothing"
    for request in client.requests:
        assert request.headers.get("Authorization") == "Basic c2VjcmV0LXRva2Vu"
    assert "c2VjcmV0LXRva2Vu" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_an_unset_target_header_variable_sends_nothing(tmp_path, tool, monkeypatch):
    monkeypatch.delenv("HQ_TEST_TARGET_AUTH", raising=False)
    client, clock = _Client(), _Clock()
    config = _config(
        tmp_path, extra='\n[targets.t.headers]\nAuthorization = "env:HQ_TEST_TARGET_AUTH"\n'
    )
    from testinghq.core.config import ConfigError

    try:
        code = RUNNERS[tool](tmp_path, client, clock, config, None)
    except ConfigError:
        code = EXIT_REFUSED  # barrage raises; its CLI turns that into exit 1
    assert code == EXIT_REFUSED
    assert client.requests == []


@pytest.mark.parametrize("tool", sorted(RUNNERS))
def test_a_public_smtp_host_is_refused_without_the_flag(tmp_path, tool):
    client, clock = _SmtpClient(), _Clock()
    try:
        code = RUNNERS[tool](tmp_path, client, clock, _smtp_config(tmp_path, PUBLIC_HOST), None)
    except guardrails.GuardrailError:
        code = EXIT_REFUSED
    assert code == EXIT_REFUSED
    assert client.requests == []


def test_barrage_at_a_public_smtp_host_needs_the_flag_and_the_config_key(tmp_path):
    client, clock = _SmtpClient(), _Clock()
    with pytest.raises(guardrails.GuardrailError, match="public = true"):
        _run_barrage(tmp_path, client, clock, _smtp_config(tmp_path, PUBLIC_HOST), None,
                     allow_public_target=True)
    assert client.requests == []

    out = tmp_path / "run.json"
    both = _smtp_config(tmp_path, PUBLIC_HOST, extra="public = true\n")
    _run_barrage(tmp_path, client, clock, both, str(out), allow_public_target=True)
    assert client.requests
    config = json.loads(out.read_text(encoding="utf-8"))["config"]
    assert config["allow_public_target"] is True
    assert config["transport"] == "smtp"


def test_sigcheck_refuses_an_smtp_target(tmp_path):
    from testinghq.pipeline import sigcheck

    client = _SmtpClient()
    lines = []
    code = sigcheck.execute(
        scheme_name="mailgun", secret="key", secret_ref="env:K", target_name="t",
        config_path=_smtp_config(tmp_path), out=None, client=client,
        printer=lines.append,
    )
    assert code == EXIT_REFUSED
    assert client.requests == []
    assert "smtp target" in lines[0] and "HTTP concept" in lines[0]
    assert "HTTP concept" in sigcheck.configured_format_mismatch(
        "mailgun", "t", _smtp_config(tmp_path)
    )


def test_sigcheck_sends_the_target_headers_too(tmp_path, monkeypatch):
    from testinghq.pipeline import sigcheck

    monkeypatch.setenv("HQ_TEST_TARGET_AUTH", "Bearer gateway-token")
    client = _Client()
    config = _config(
        tmp_path, extra='\n[targets.t.headers]\nX-Gateway-Auth = "env:HQ_TEST_TARGET_AUTH"\n'
    )
    sigcheck.execute(
        scheme_name="mailgun", secret="key", secret_ref="env:K", target_name="t",
        config_path=config, out=None, client=client, printer=lambda _line: None,
    )
    assert client.requests
    assert all(r.headers.get("X-Gateway-Auth") == "Bearer gateway-token"
               for r in client.requests)
