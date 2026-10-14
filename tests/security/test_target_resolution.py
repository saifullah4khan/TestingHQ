"""`core.targets.resolve_target`: the one gate every send path goes through.

DNS is faked in every test here by replacing `socket.getaddrinfo`, so nothing
resolves a real name and the split-horizon case can be simulated.
"""
from __future__ import annotations

import socket

import pytest

from testinghq.core import guardrails
from testinghq.core.config import ConfigError
from testinghq.core.formats import MailgunFormat, SendgridFormat
from testinghq.core.targets import ResolvedTarget, resolve_target


@pytest.fixture
def dns(monkeypatch):
    """A fake resolver: host -> list of addresses. Unknown names fail to
    resolve. Records every lookup."""
    table = {}
    lookups = []

    def fake_getaddrinfo(host, *args, **kwargs):
        lookups.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in table[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guardrails.clear_dns_cache()
    yield table, lookups
    guardrails.clear_dns_cache()


def _config(tmp_path, url, *, name="t", extra=""):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{name}]\nurl = "{url}"\n{extra}', encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/in",
        "http://127.0.0.1:8000/in",
        "http://10.0.0.5/in",
        "http://192.168.1.10/in",
        "http://[::1]:8000/in",
        "https://staging.example.com/in",
        "https://hooks.pipeline.test/in",
        "http://intake:8000/in",
    ],
)
def test_reserved_and_private_hosts_resolve_without_any_flag(tmp_path, dns, url):
    resolved = resolve_target("t", _config(tmp_path, url))
    assert isinstance(resolved, ResolvedTarget)
    assert resolved.url == url
    assert resolved.public is False
    assert dns[1] == [], "a reserved or private host needs no DNS lookup"


def test_no_target_name_is_refused(tmp_path, dns):
    with pytest.raises(guardrails.GuardrailError, match="--send requires --target"):
        resolve_target(None, _config(tmp_path, "http://127.0.0.1/in"))


def test_an_unconfigured_name_is_refused(tmp_path, dns):
    with pytest.raises(guardrails.GuardrailError, match="unconfigured target"):
        resolve_target("other", _config(tmp_path, "http://127.0.0.1/in"))


def test_a_missing_config_file_is_a_config_error(tmp_path, dns):
    with pytest.raises(ConfigError):
        resolve_target("t", str(tmp_path / "absent.toml"))


def test_a_public_name_is_refused_without_the_flag(tmp_path, dns):
    dns[0]["ingest.mycompany.com"] = ["93.184.216.34"]
    with pytest.raises(guardrails.GuardrailError) as excinfo:
        resolve_target("t", _config(tmp_path, "https://ingest.mycompany.com/in"))
    assert "--allow-public-target" in str(excinfo.value)
    assert "ingest.mycompany.com" in str(excinfo.value)


def test_a_public_name_is_allowed_with_the_flag(tmp_path, dns):
    dns[0]["ingest.mycompany.com"] = ["93.184.216.34"]
    resolved = resolve_target(
        "t", _config(tmp_path, "https://ingest.mycompany.com/in"),
        allow_public_target=True,
    )
    assert resolved.public is True
    assert resolved.artifact_config() == {
        "format": "sendgrid", "allow_public_target": True,
    }


def test_a_name_that_fails_to_resolve_counts_as_public(tmp_path, dns):
    with pytest.raises(guardrails.GuardrailError, match="public host"):
        resolve_target("t", _config(tmp_path, "https://nowhere.mycompany.com/in"))


def test_a_split_horizon_name_resolving_only_to_private_addresses_is_private(tmp_path, dns):
    dns[0]["intake.corp.internal.net"] = ["10.1.2.3", "fd00::7"]
    resolved = resolve_target("t", _config(tmp_path, "https://intake.corp.internal.net/in"))
    assert resolved.public is False
    assert resolved.artifact_config()["allow_public_target"] is False


def test_one_public_address_among_private_ones_makes_the_name_public(tmp_path, dns):
    dns[0]["mixed.mycompany.com"] = ["10.1.2.3", "93.184.216.34"]
    with pytest.raises(guardrails.GuardrailError):
        resolve_target("t", _config(tmp_path, "https://mixed.mycompany.com/in"))


def test_an_ipv4_mapped_public_address_is_public(tmp_path, dns):
    dns[0]["mapped.mycompany.com"] = ["::ffff:93.184.216.34"]
    with pytest.raises(guardrails.GuardrailError):
        resolve_target("t", _config(tmp_path, "https://mapped.mycompany.com/in"))


@pytest.mark.parametrize(
    "url", ["http://93.184.216.34/in", "http://[2606:4700::1111]/in"]
)
def test_a_public_ip_literal_is_refused_without_a_lookup(tmp_path, dns, url):
    with pytest.raises(guardrails.GuardrailError):
        resolve_target("t", _config(tmp_path, url))
    assert dns[1] == []


def test_a_public_ipv6_literal_is_not_mistaken_for_a_bare_internal_name():
    """An IPv6 literal has no dot, and the string check used to read it as a
    single-label internal name and wave it through."""
    target = "http://[2606:4700::1111]/hook"
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(target, [target])


def test_resolution_is_cached_per_process(tmp_path, dns):
    dns[0]["intake.corp.internal.net"] = ["10.1.2.3"]
    config = _config(tmp_path, "https://intake.corp.internal.net/in")
    resolve_target("t", config)
    resolve_target("t", config)
    assert dns[1] == ["intake.corp.internal.net"]


def test_an_env_override_to_a_public_url_is_refused(tmp_path, dns, monkeypatch):
    monkeypatch.setenv("TESTINGHQ_TARGET_T_URL", "https://ingest.mycompany.com/in")
    dns[0]["ingest.mycompany.com"] = ["93.184.216.34"]
    with pytest.raises(guardrails.GuardrailError):
        resolve_target("t", _config(tmp_path, "http://127.0.0.1/in"))


def test_the_wire_format_is_the_configured_one(tmp_path, dns):
    resolved = resolve_target(
        "t", _config(tmp_path, "http://127.0.0.1/in", extra='format = "mailgun"\n')
    )
    assert isinstance(resolved.wire_format, MailgunFormat)
    assert resolved.format_name == "mailgun"
    default = resolve_target("t", _config(tmp_path, "http://127.0.0.1/in"))
    assert isinstance(default.wire_format, SendgridFormat)


# ---------------------------------------------------------------------------
# Barrage: a public host needs the flag AND `public = true`
# ---------------------------------------------------------------------------


@pytest.fixture
def public_host(dns):
    dns[0]["load.mycompany.com"] = ["93.184.216.34"]
    return "https://load.mycompany.com/in"


def test_barrage_at_a_public_host_with_only_the_flag_is_refused(tmp_path, public_host):
    with pytest.raises(guardrails.GuardrailError) as excinfo:
        resolve_target(
            "t", _config(tmp_path, public_host),
            allow_public_target=True, load_tool="barrage",
        )
    message = str(excinfo.value)
    assert "--allow-public-target" in message and "public = true" in message
    assert "Missing: public = true" in message


def test_barrage_at_a_public_host_with_only_the_config_key_is_refused(tmp_path, public_host):
    with pytest.raises(guardrails.GuardrailError) as excinfo:
        resolve_target(
            "t", _config(tmp_path, public_host, extra="public = true\n"),
            load_tool="barrage",
        )
    assert "Missing: --allow-public-target." in str(excinfo.value)


def test_barrage_at_a_public_host_with_both_is_allowed(tmp_path, public_host):
    resolved = resolve_target(
        "t", _config(tmp_path, public_host, extra="public = true\n"),
        allow_public_target=True, load_tool="barrage",
    )
    assert resolved.public is True


def test_the_config_key_alone_does_not_open_a_public_host_to_other_tools(tmp_path, public_host):
    with pytest.raises(guardrails.GuardrailError):
        resolve_target("t", _config(tmp_path, public_host, extra="public = true\n"))


def test_barrage_at_a_private_host_needs_neither(tmp_path, dns):
    resolved = resolve_target(
        "t", _config(tmp_path, "http://127.0.0.1/in"), load_tool="barrage"
    )
    assert resolved.public is False
