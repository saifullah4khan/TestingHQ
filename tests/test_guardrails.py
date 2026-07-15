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
