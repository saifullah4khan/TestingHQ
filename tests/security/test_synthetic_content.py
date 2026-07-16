"""Generated content must never use a real or non-reserved domain.

Exact reserved set from the interface contract (RFC 2606 / RFC 6761):
  domains: example.com, example.net, example.org, example.edu
  TLDs:    .test, .invalid, .example, .localhost

is_synthetic_address stays strict and bare-address-only; it is a primitive
and is deliberately not widened. require_synthetic_content is the one that
accepts real header forms (bare, display-name, multi-recipient) and checks
every address it can extract from them.
"""
from email.utils import parseaddr

import pytest

from testinghq.blast.generate import generate_corpus
from testinghq.core import guardrails


def _legacy_parseaddr_extract(values):
    """A parseaddr-based extractor, standing in for the tempting-but-wrong
    implementation of guardrails._extract_addresses.

    This uses parseaddr's pre-hardening semantics (`strict=False`), which is
    what Python exhibits BY DEFAULT before the CVE-2023-27043 fix landed
    (< 3.9.19 / 3.10.14 / 3.11.9 / 3.12.4). This project's
    requires-python = ">=3.9" permits every one of those interpreters, so
    this is not a hypothetical: it is the behavior a supported install can
    have. Used below to demonstrate that the multi-recipient bypass is real
    and that using getaddresses is what kills it.
    """
    extracted = []
    for value in values:
        try:
            _name, address = parseaddr(value, strict=False)
        except TypeError:  # pragma: no cover - Python without the strict kwarg
            _name, address = parseaddr(value)
        if address:
            extracted.append(address)
    return extracted


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


# ---------------------------------------------------------------------------
# Header forms the engine actually emits
# ---------------------------------------------------------------------------


def test_display_name_form_is_accepted():
    # The seam that broke integration: the engine emits RFC 5322
    # display-name form in `to` and `from_addr`, not a bare address.
    guardrails.require_synthetic_content(["Ivan Jones <frank.nguyen7@example.edu>"])


def test_display_name_form_with_a_real_domain_is_refused():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content(["Ivan Jones <ivan@gmail.com>"])


def test_multi_recipient_header_all_synthetic_is_accepted():
    header = "A <a@example.com>, B <b@example.net>, C <c@thing.test>"
    guardrails.require_synthetic_content([header])


def test_quoted_display_name_containing_a_comma_is_accepted():
    # The quoted comma must not be mistaken for a recipient separator.
    guardrails.require_synthetic_content(['"Jones, Ivan" <ivan@example.org>'])


def test_mixed_bare_and_display_and_multi_forms_together():
    guardrails.require_synthetic_content(
        [
            "bare@example.com",
            "Display Name <display@example.net>",
            "M1 <m1@example.org>, M2 <m2@some.invalid>",
        ]
    )


# ---------------------------------------------------------------------------
# THE BYPASS: every address in a multi-recipient header must be checked
# ---------------------------------------------------------------------------

# A reserved address first, a real one second. A guard that checks only the
# first address passes this on the strength of a@example.com while
# b@evil-real-domain.com sails through unchecked.
BYPASS_HEADER = "Good <a@example.com>, Evil <b@evil-real-domain.com>"


def test_multi_recipient_header_hiding_a_real_domain_is_refused():
    with pytest.raises(guardrails.GuardrailError) as excinfo:
        guardrails.require_synthetic_content([BYPASS_HEADER])
    # Refused for the RIGHT reason: it named the evil address, meaning it
    # actually looked past the first recipient.
    assert "evil-real-domain.com" in str(excinfo.value)


def test_real_domain_refused_in_every_position_of_a_multi_recipient_header():
    evil = "evil@evil-real-domain.com"
    good = ["a@example.com", "b@example.net", "c@example.org"]
    for position in range(len(good) + 1):
        recipients = good[:position] + [evil] + good[position:]
        header = ", ".join(recipients)
        with pytest.raises(guardrails.GuardrailError):
            guardrails.require_synthetic_content([header])


def test_the_bypass_is_real_a_parseaddr_extractor_misses_the_evil_address():
    """Demonstrate the vulnerability rather than assume it away.

    Legacy parseaddr semantics return at most one address, so the evil
    address is never even extracted, and therefore never checked.
    """
    extracted = _legacy_parseaddr_extract([BYPASS_HEADER])
    assert "b@evil-real-domain.com" not in extracted
    assert extracted == ["a@example.com"]


def test_our_extractor_finds_every_address_the_parseaddr_one_misses():
    extracted = guardrails._extract_addresses([BYPASS_HEADER])
    assert extracted == ["a@example.com", "b@evil-real-domain.com"]


def test_bypass_test_goes_red_if_extraction_is_swapped_for_parseaddr(monkeypatch):
    """Pin the refusal to the extractor choice.

    Swap in the parseaddr-based extractor and the guard ACCEPTS the
    malicious header: no exception, evil address unchecked. That is the
    bypass, executed live. This test failing would mean the refusal above
    no longer depends on extracting every address, i.e. the demonstration
    had gone vacuous.
    """
    monkeypatch.setattr(guardrails, "_extract_addresses", _legacy_parseaddr_extract)
    # No raise: the guard is bypassed.
    guardrails.require_synthetic_content([BYPASS_HEADER])


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "garbage-with-no-at-sign",
        "undisclosed-recipients:;",
        "<>",
        "Name <>",
    ],
)
def test_unparseable_or_empty_header_refuses(value):
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content([value])


def test_non_string_address_field_refuses():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content([None])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content([123])


def test_a_good_header_alongside_an_unparseable_one_still_refuses():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content(["a@example.com", "<>"])


# ---------------------------------------------------------------------------
# Regression: real engine output must be ACCEPTED
# ---------------------------------------------------------------------------


def _address_fields(email):
    return [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]


def test_real_generated_corpus_is_accepted():
    """The regression that would have caught the integration seam.

    Feed the engine's actual generated output straight into the guard. If
    this ever goes red, either the guard rejects legitimate synthetic
    payloads (guard bug) or the generator emits a non-reserved domain
    (generator bug). Either way it must not be "fixed" by loosening the
    reserved-domain check.
    """
    corpus = generate_corpus(seed=7, count=50)
    assert corpus
    values = []
    for email in corpus:
        values.extend(_address_fields(email))
    guardrails.require_synthetic_content(values)


def test_each_generated_email_is_accepted_individually():
    for email in generate_corpus(seed=3, count=25):
        guardrails.require_synthetic_content(_address_fields(email))


def test_generated_to_and_from_are_display_name_form_not_bare():
    """Pin the seam itself. If the engine ever switches to bare addresses
    this test tells us the shape changed, rather than letting the change go
    unnoticed."""
    email = generate_corpus(seed=1, count=1)[0]
    assert "<" in email.to and ">" in email.to
    # The strict bare-address primitive does NOT accept this form, which is
    # exactly why require_synthetic_content has to do extraction.
    assert guardrails.is_synthetic_address(email.to) is False
    guardrails.require_synthetic_content([email.to])


def test_real_corpus_with_a_spliced_real_address_is_refused():
    corpus = generate_corpus(seed=7, count=10)
    values = []
    for email in corpus:
        values.extend(_address_fields(email))
    values.append("Evil <attacker@evil-real-domain.com>")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content(values)


def test_real_corpus_with_a_real_address_spliced_into_a_header_is_refused():
    email = generate_corpus(seed=11, count=1)[0]
    spliced = f"{email.to}, Evil <attacker@evil-real-domain.com>"
    with pytest.raises(guardrails.GuardrailError):
        guardrails.require_synthetic_content([spliced])
