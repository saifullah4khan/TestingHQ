"""require_configured_target: refuse unconfigured AND obvious public hosts.

Two independent refusals are under test:
1. The target must be in the configured allow-list at all (pre-existing).
2. Even a configured target is refused if it looks like a real, publicly
   routable host, unless the caller explicitly opts in with
   allow_public_hosts=True. This catches a copy-pasted real hostname
   landing in config by mistake.
"""
import pytest

from testinghq.core import guardrails


def test_unconfigured_target_is_refused():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(
            "https://evil.example.com", ["https://ok.example"]
        )


def test_reserved_domain_configured_target_is_allowed():
    ok = guardrails.require_configured_target(
        "https://ok.example", ["https://ok.example"]
    )
    assert ok == "https://ok.example"


def test_localhost_configured_target_is_allowed():
    target = "http://localhost:8080/ingest"
    ok = guardrails.require_configured_target(target, [target])
    assert ok == target


def test_loopback_ip_configured_target_is_allowed():
    target = "http://127.0.0.1:9000/ingest"
    ok = guardrails.require_configured_target(target, [target])
    assert ok == target


def test_private_ip_configured_target_is_allowed():
    target = "http://10.0.0.5:9000/ingest"
    ok = guardrails.require_configured_target(target, [target])
    assert ok == target


def test_bare_internal_name_is_allowed():
    ok = guardrails.require_configured_target("local", ["local"])
    assert ok == "local"


def test_public_host_is_refused_even_when_configured():
    # Present in the allow-list is not enough on its own: a real public
    # domain is exactly the mistake this guard exists to catch.
    target = "https://ingest.mycompany.com"
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(target, [target])


def test_public_ip_is_refused_even_when_configured():
    target = "http://93.184.216.34"
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(target, [target])


def test_public_host_allowed_only_with_explicit_override():
    target = "https://ingest.mycompany.com"
    ok = guardrails.require_configured_target(
        target, [target], allow_public_hosts=True
    )
    assert ok == target


def test_override_does_not_bypass_the_allow_list_check():
    # allow_public_hosts only relaxes the public-host check, never the
    # base "must be configured" check.
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(
            "https://not-configured.mycompany.com",
            ["https://ingest.mycompany.com"],
            allow_public_hosts=True,
        )
