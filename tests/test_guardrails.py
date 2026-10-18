import pytest

from testinghq.core import guardrails


def test_dry_run_is_default():
    decision = guardrails.evaluate_send(send_flag=False)
    assert decision.will_send is False


def test_send_flag_enables_send():
    decision = guardrails.evaluate_send(send_flag=True)
    assert decision.will_send is True


def test_unconfigured_target_is_refused():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_configured_target(
            "https://evil.example", ["https://ok.example"]
        )


def test_configured_target_is_allowed():
    ok = guardrails.require_configured_target(
        "https://ok.example", ["https://ok.example"]
    )
    assert ok == "https://ok.example"


def test_is_private_host_uses_the_string_check_before_any_lookup():
    def resolver(host):
        raise AssertionError(f"looked up {host}")

    for host in ("localhost", "intake", "a.example.com", "x.test", "10.0.0.1", "::1"):
        assert guardrails.is_private_host(host, resolver=resolver) is True


def test_is_private_host_with_an_injected_resolver():
    table = {
        "split.corp.net": ["10.0.0.7", "fe80::1%eth0"],
        "mixed.corp.net": ["10.0.0.7", "8.8.8.8"],
        "mapped.corp.net": ["::ffff:8.8.8.8"],
    }
    resolve = lambda host: table.get(host, [])  # noqa: E731
    assert guardrails.is_private_host("split.corp.net", resolver=resolve) is True
    assert guardrails.is_private_host("mixed.corp.net", resolver=resolve) is False
    assert guardrails.is_private_host("mapped.corp.net", resolver=resolve) is False
    assert guardrails.is_private_host("gone.corp.net", resolver=resolve) is False
    assert guardrails.is_private_host("8.8.8.8", resolver=resolve) is False
    assert guardrails.is_private_host("", resolver=resolve) is False
