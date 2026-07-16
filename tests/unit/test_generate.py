from testinghq.blast.generate import RESERVED_DOMAINS, generate_corpus
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
