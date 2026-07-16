import pytest

from testinghq.barrage.payloads import build_payload_pool, payload_for_index
from testinghq.blast.generate import generate_corpus


def test_build_payload_pool_matches_generate_corpus_directly():
    pool = build_payload_pool(seed=7, pool_size=12)
    expected = generate_corpus(7, 12)
    assert pool == expected


def test_build_payload_pool_is_deterministic_across_calls():
    pool_a = build_payload_pool(seed=42, pool_size=20)
    pool_b = build_payload_pool(seed=42, pool_size=20)
    assert pool_a == pool_b


def test_build_payload_pool_rejects_non_positive_size():
    with pytest.raises(ValueError):
        build_payload_pool(seed=1, pool_size=0)
    with pytest.raises(ValueError):
        build_payload_pool(seed=1, pool_size=-5)


def test_payload_for_index_cycles_through_the_pool():
    pool = build_payload_pool(seed=3, pool_size=5)
    assert payload_for_index(pool, 0) == pool[0]
    assert payload_for_index(pool, 4) == pool[4]
    assert payload_for_index(pool, 5) == pool[0]
    assert payload_for_index(pool, 12) == pool[2]


def test_payload_for_index_is_reproducible_for_the_same_index():
    pool = build_payload_pool(seed=9, pool_size=10)
    first = payload_for_index(pool, 37)
    second = payload_for_index(pool, 37)
    assert first == second


def test_payload_for_index_rejects_empty_pool_or_negative_index():
    pool = build_payload_pool(seed=1, pool_size=3)
    with pytest.raises(ValueError):
        payload_for_index([], 0)
    with pytest.raises(ValueError):
        payload_for_index(pool, -1)


def test_payload_pool_only_uses_reserved_domains():
    from testinghq.core.guardrails import is_synthetic_address

    pool = build_payload_pool(seed=5, pool_size=15)
    for email in pool:
        assert is_synthetic_address(email.envelope.from_addr)
        for addr in email.envelope.to:
            assert is_synthetic_address(addr)
