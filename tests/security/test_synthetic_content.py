"""Generated content must never use a real or non-reserved domain.

Exact reserved set from the interface contract (RFC 2606 / RFC 6761):
  domains: example.com, example.net, example.org, example.edu
  TLDs:    .test, .invalid, .example, .localhost
"""
import pytest

from testinghq.core import guardrails


@pytest.mark.parametrize(
    "address",
    [
        "a@example.com",
        "a@example.net",
        "a@example.org",
        "a@example.edu",
        "a@sub.example.com",
        "a@deep.sub.example.org",
        "a@something.test",
        "a@something.invalid",
        "a@something.example",
        "a@something.localhost",
        "a@localhost",
    ],
)
def test_reserved_addresses_are_synthetic(address):
    assert guardrails.is_synthetic_address(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "a@gmail.com",
        "a@yahoo.com",
        "a@sendgrid.net.example.attacker.com",
        "a@example.com.evil.com",  # suffix trick: not actually reserved
        "a@notexample.com",  # substring trick: not a reserved subdomain
        "a@examplecom",
        "not-an-email",
        "",
        "a@",
        "@example.com",
        None,
        123,
    ],
)
def test_non_reserved_addresses_are_not_synthetic(address):
    assert guardrails.is_synthetic_address(address) is False


def test_require_synthetic_content_passes_for_all_reserved_addresses():
    guardrails.require_synthetic_content(["a@example.com", "b@something.test"])


def test_require_synthetic_content_raises_on_any_real_domain():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content(["a@example.com", "b@gmail.com"])


def test_require_synthetic_content_raises_on_empty_input():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content([])


def test_require_synthetic_content_accepts_a_single_address_string():
    guardrails.require_synthetic_content("a@example.com")


def test_require_synthetic_content_rejects_a_single_real_address_string():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content("a@gmail.com")
