import datetime
from email.utils import parseaddr, parsedate_to_datetime
import re
from email.header import decode_header, make_header

from testinghq.blast.generate import RESERVED_DOMAINS, TOPICS, generate_corpus
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


def test_generate_corpus_returns_requested_count():
    corpus = generate_corpus(seed=1, count=7)
    assert len(corpus) == 7


def test_generate_corpus_zero_count_is_empty():
    assert generate_corpus(seed=1, count=0) == []


def test_generate_corpus_rejects_negative_count():
    import pytest

    with pytest.raises(ValueError):
        generate_corpus(seed=1, count=-1)


def test_same_seed_yields_equal_objects():
    first = generate_corpus(seed=42, count=10)
    second = generate_corpus(seed=42, count=10)
    assert first == second


def test_same_seed_yields_byte_identical_serialized_output():
    first = generate_corpus(seed=42, count=10)
    second = generate_corpus(seed=42, count=10)
    first_parts = [to_multipart_parts(email) for email in first]
    second_parts = [to_multipart_parts(email) for email in second]
    assert first_parts == second_parts


def test_different_seeds_yield_different_output():
    first = generate_corpus(seed=1, count=5)
    second = generate_corpus(seed=2, count=5)
    assert first != second


def test_generate_corpus_is_reproducible_regardless_of_prior_random_use():
    """The generator must not depend on any global/module-level random
    state: exercising the stdlib random module beforehand must not change
    generate_corpus's output for a given seed."""
    import random

    random.seed(12345)
    random.random()
    random.random()
    baseline = generate_corpus(seed=7, count=5)

    random.seed(999)
    for _ in range(50):
        random.random()
    after_unrelated_random_use = generate_corpus(seed=7, count=5)

    assert baseline == after_unrelated_random_use


def test_every_generated_address_uses_a_reserved_domain():
    corpus = generate_corpus(seed=3, count=25)
    for email in corpus:
        assert _is_reserved(_domain_of(email.from_addr)), email.from_addr
        assert _is_reserved(_domain_of(email.to)), email.to
        assert _is_reserved(_domain_of(email.envelope.from_addr)), email.envelope.from_addr
        for recipient in email.envelope.to:
            assert _is_reserved(_domain_of(recipient)), recipient


def test_reserved_domains_pool_only_contains_reserved_domains():
    for domain in RESERVED_DOMAINS:
        assert _is_reserved(domain), domain


def test_generated_email_has_well_formed_shape():
    corpus = generate_corpus(seed=5, count=10)
    for email in corpus:
        assert email.subject
        assert email.text
        assert email.html
        assert email.envelope.to
        assert email.envelope.from_addr
        assert email.ground_truth.subject == email.subject
        assert email.ground_truth.from_addr == email.envelope.from_addr
        assert email.ground_truth.body_core
        assert email.ground_truth.body_core in email.text
        assert email.charsets == {
            "to": "UTF-8",
            "from": "UTF-8",
            "subject": "UTF-8",
            "text": "UTF-8",
            "html": "UTF-8",
        }
        assert email.attachments == ()
        assert "From" in email.headers
        assert "Date" in email.headers
        assert "Message-ID" in email.headers


def test_generated_email_serializes_without_error():
    corpus = generate_corpus(seed=9, count=5)
    for email in corpus:
        parts = to_multipart_parts(email)
        names = [part.name for part in parts]
        assert names[:3] == ["headers", "to", "from"]


# ---------------------------------------------------------------------------
# Realism of the clean corpus
# ---------------------------------------------------------------------------

CORPUS = generate_corpus(seed=21, count=300)


def test_display_name_and_local_part_come_from_one_person():
    for item in CORPUS:
        name, address = parseaddr(item.from_addr)
        first, last = name.lower().split()
        local = address.split("@")[0]
        assert local.startswith(first) or local.startswith(first[0] + last), (name, address)


def test_subject_topic_and_body_topic_agree():
    for item in CORPUS:
        topics = [t for t in TOPICS if t.lower() in item.subject.lower()]
        assert topics, item.subject
        assert any(t in item.ground_truth.body_core for t in topics), (item.subject, item.text)


def test_weekday_matches_the_date():
    for item in CORPUS:
        parsed = parsedate_to_datetime(item.headers["Date"])
        assert item.headers["Date"].startswith(
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[parsed.weekday()]
        ), item.headers["Date"]
        assert isinstance(parsed, datetime.datetime)


def test_about_a_sixth_of_bodies_carry_a_url_order_number_or_signature():
    marked = [
        item for item in CORPUS
        if "https://" in item.text or "order number" in item.text or "\n--\n" in item.text
    ]
    assert 0.08 < len(marked) / len(CORPUS) < 0.28
    assert any("https://www.example.com/" in m.text for m in marked)
    assert any(re.search(r"order number is \d", m.text) for m in marked)
    assert any("\n--\n" in m.text for m in marked)


def test_html_has_a_gmail_or_outlook_wrapper():
    gmail = outlook = 0
    for item in CORPUS:
        assert item.html.startswith("<html>") and item.html.endswith("</body></html>")
        gmail += '<div dir="ltr">' in item.html
        outlook += '<div class="WordSection1">' in item.html
    assert gmail and outlook and gmail + outlook == len(CORPUS)


def test_replies_carry_in_reply_to_and_references():
    replies = [item for item in CORPUS if item.subject.startswith("Re:")]
    assert replies
    for item in CORPUS:
        if item.subject.startswith("Re:"):
            assert item.headers["In-Reply-To"] in item.headers["References"]
        else:
            assert "In-Reply-To" not in item.headers


def test_every_message_has_delivery_headers():
    for item in CORPUS:
        assert item.headers["Received"].startswith("from ")
        assert "[192.0.2." in item.headers["Received"]
        assert item.headers["Return-Path"] == f"<{item.envelope.from_addr}>"
        assert "spf=pass" in item.headers["Authentication-Results"]


def test_non_ascii_subjects_are_rfc2047_in_headers_and_decoded_in_the_field():
    non_ascii = [item for item in CORPUS if not item.subject.isascii()]
    assert non_ascii
    for item in CORPUS:
        assert item.headers["Subject"].isascii()
        assert str(make_header(decode_header(item.headers["Subject"]))) == item.subject
