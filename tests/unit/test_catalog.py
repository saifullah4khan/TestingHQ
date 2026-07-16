import pytest

from testinghq.blast.catalog import CATALOG, build, build_all
from testinghq.blast.serialize import to_multipart_parts

RESERVED_TLDS = (".test", ".invalid", ".example", ".localhost")


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].rstrip(">")


def _is_reserved(domain: str) -> bool:
    return domain in (
        "example.com",
        "example.net",
        "example.org",
        "example.edu",
    ) or domain.endswith(RESERVED_TLDS)


def test_build_unknown_case_raises_key_error():
    with pytest.raises(KeyError):
        build("not-a-real-case", seed=1)


def test_build_is_deterministic_for_same_seed():
    for name in CATALOG:
        first = build(name, seed=1)
        second = build(name, seed=1)
        assert first == second, name


def test_build_all_covers_every_registered_case():
    results = build_all(seed=1)
    assert {name for name, _ in results} == set(CATALOG)


def test_every_case_serializes_without_error():
    for name, email in build_all(seed=1):
        parts = to_multipart_parts(email)
        assert parts, name


def test_every_case_uses_reserved_domains():
    for name, email in build_all(seed=1):
        if email.from_addr:
            assert _is_reserved(_domain_of(email.from_addr)), (name, email.from_addr)
        assert _is_reserved(_domain_of(email.envelope.from_addr)), name
        for recipient in email.envelope.to:
            assert _is_reserved(_domain_of(recipient)), (name, recipient)


def test_empty_subject_case_has_empty_subject():
    email = build("empty_subject", seed=1)
    assert email.subject == ""


def test_html_only_no_text_case_has_no_text():
    email = build("html_only_no_text", seed=1)
    assert email.text == ""
    assert email.html


def test_text_only_no_html_case_has_no_html():
    email = build("text_only_no_html", seed=1)
    assert email.html == ""
    assert email.text


def test_many_recipients_case_has_many_envelope_recipients():
    email = build("many_recipients", seed=1)
    assert len(email.envelope.to) >= 20


def test_plus_addressing_case_has_plus_tag():
    email = build("plus_addressing", seed=1)
    assert "+" in email.envelope.to[0]


def test_single_char_local_part_case():
    email = build("single_char_local_part", seed=1)
    local_part = email.envelope.from_addr.split("@", 1)[0]
    assert local_part == "a"


def test_zero_length_envelope_case_has_no_recipients():
    email = build("zero_length_envelope", seed=1)
    assert email.envelope.to == ()
    assert email.to == ""


def test_header_injection_attempt_embeds_crlf_in_subject():
    email = build("header_injection_attempt", seed=1)
    assert "\r\n" in email.subject


def test_zero_byte_attachment_case_has_one_empty_attachment():
    email = build("zero_byte_attachment", seed=1)
    assert len(email.attachments) == 1
    assert email.attachments[0].content == b""


def test_declared_charset_mismatch_case_sets_a_charset_label():
    email = build("declared_charset_mismatch", seed=1)
    assert email.charsets.get("subject") or email.charsets.get("html")


def test_extremely_long_subject_case_is_much_longer_than_normal():
    email = build("extremely_long_subject", seed=1)
    assert len(email.subject) > 500


def test_unicode_heavy_subject_case_has_non_ascii_content():
    email = build("unicode_heavy_subject", seed=1)
    assert any(ord(ch) > 127 for ch in email.subject)
