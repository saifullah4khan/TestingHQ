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


def test_many_recipients_puts_500_in_the_to_header_and_one_on_the_envelope():
    email = build("many_recipients", seed=1)
    assert len(email.headers["To"].split(", ")) == 500
    assert email.headers["To"] == email.to
    assert len(email.envelope.to) == 1
    assert email.envelope.to[0] in email.to.split(", ")


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
    assert len(email.subject) == 10_000
    assert email.headers["Subject"] == email.subject


def test_unicode_heavy_subject_case_has_non_ascii_content():
    email = build("unicode_heavy_subject", seed=1)
    assert any(ord(ch) > 127 for ch in email.subject)


def test_every_case_has_a_verdict_category():
    from testinghq.blast.catalog import CATALOG_CATEGORIES
    from testinghq.core.report import CATEGORIES

    assert set(CATALOG_CATEGORIES) == set(CATALOG)
    assert set(CATALOG_CATEGORIES.values()) <= set(CATEGORIES)


def test_cases_do_not_share_one_base_message():
    subjects = {build(name, seed=1).ground_truth.body_core for name in ("plus_addressing", "reply_to_mismatch", "empty_subject")}
    assert len(subjects) == 3


def test_null_byte_in_body_case():
    email = build("null_byte_in_body", seed=2)
    assert "\x00" in email.text and "\x00" in email.html


def test_rtl_override_in_filename_case():
    (attachment,) = build("rtl_override_in_filename", seed=2).attachments
    assert "\u202e" in attachment.filename
    assert attachment.filename.endswith(".exe")
    assert attachment.content_type == "application/pdf"
    assert attachment.content.startswith(b"MZ")


def test_missing_boundary_field_case_writes_a_part_with_no_name():
    from testinghq.core.transport import encode_multipart

    body = encode_multipart(to_multipart_parts(build("missing_boundary_field", seed=2)))
    assert b"Content-Disposition: form-data\r\n\r\n" in body


def test_duplicate_form_field_case_sends_two_subject_parts():
    parts = to_multipart_parts(build("duplicate_form_field", seed=2))
    assert [p.name for p in parts].count("subject") == 2


def test_malformed_envelope_json_case_is_not_json():
    import json

    parts = {p.name: p.value for p in to_multipart_parts(build("malformed_envelope_json", seed=2))}
    with pytest.raises(ValueError):
        json.loads(parts["envelope"])


def test_oversized_body_case_is_exactly_one_mebibyte():
    email = build("oversized_body_1mb", seed=2)
    assert len(email.text.encode("utf-8")) == 1024 * 1024
    assert email.ground_truth.body_core in email.text


def test_heavily_quoted_reply_chain_is_fixed_in_shape():
    for seed in range(5):
        email = build("heavily_quoted_reply_chain", seed=seed)
        lines = email.text.splitlines()
        attributions = [line for line in lines if line.lstrip("> ").endswith("wrote:")]
        assert len(attributions) == 4, attributions
        depths = [quote_depth(line) for line in attributions]
        assert depths == [0, 1, 2, 3]
        original = [line for line in lines if "-----Original Message-----" in line]
        assert len(original) == 1
        assert quote_depth(original[0]) == 4
        assert email.html.count('class="gmail_quote"') == 8
        assert email.subject.startswith("Re: ")
        assert len(email.headers["References"].split()) == 5


def test_attachment_cases_switch_to_multipart_mixed():
    for name in ("zero_byte_attachment", "degenerate_attachment", "rtl_override_in_filename"):
        assert email_content_type(build(name, seed=3)).startswith("multipart/mixed")


def quote_depth(line):
    prefix = line[: len(line) - len(line.lstrip("> "))]
    return prefix.count(">")


def email_content_type(email):
    return email.headers["Content-Type"]
