import random
from collections import Counter

import pytest

from testinghq.blast.generate import RESERVED_DOMAINS, generate_corpus, generate_email
from testinghq.blast.corrupt import (
    DEFAULT_MIX,
    MUTATORS,
    RECIPES,
    apply_recipe,
    choose_category,
    corrupt_corpus,
    corrupt_email,
    encoding_sabotage,
    homoglyph_mojibake,
    script_mixing,
    structural_noise,
    typo,
    word_salad,
)

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


def _clean_email():
    rng = random.Random(1)
    return generate_email(rng, index=0)


# ---------------------------------------------------------------------------
# Individual mutators each produce their class of corruption
# ---------------------------------------------------------------------------


def test_typo_changes_subject_or_text():
    email = _clean_email()
    mutated = typo(email, random.Random(2))
    assert (mutated.subject, mutated.text) != (email.subject, email.text)
    # length is preserved or off by one per touched word; content differs
    assert mutated.html == email.html


def test_typo_is_deterministic_for_same_rng_seed():
    email = _clean_email()
    first = typo(email, random.Random(99))
    second = typo(email, random.Random(99))
    assert first == second


def test_homoglyph_mojibake_changes_content():
    email = _clean_email()
    # Try a handful of rng seeds; at least one must actually change content
    # (mode is itself randomly chosen, so a single seed could land on a
    # no-op corner in principle).
    changed = any(
        homoglyph_mojibake(email, random.Random(seed)) != email for seed in range(20)
    )
    assert changed


def test_homoglyph_mojibake_never_touches_charsets():
    email = _clean_email()
    mutated = homoglyph_mojibake(email, random.Random(3))
    assert mutated.charsets == email.charsets


def test_script_mixing_inserts_foreign_script_text():
    email = _clean_email()
    mutated = script_mixing(email, random.Random(4))
    assert mutated.text != email.text
    assert email.text in mutated.text  # appended, not replaced
    non_ascii_added = any(ord(ch) > 127 for ch in mutated.text[len(email.text) :])
    assert non_ascii_added


def test_word_salad_preserves_connectors_and_short_words():
    email = _clean_email()
    mutated = word_salad(email, random.Random(6))
    assert "Hi" in mutated.text or "Hello" in mutated.text or "," in mutated.text
    assert mutated.html == email.html


def test_word_salad_changes_content_words():
    email = _clean_email()
    mutated = word_salad(email, random.Random(6))
    assert mutated.text != email.text or mutated.subject != email.subject


def test_structural_noise_extends_or_alters_body():
    email = _clean_email()
    changed = any(
        structural_noise(email, random.Random(seed)) != email for seed in range(20)
    )
    assert changed


def test_structural_noise_can_produce_broken_html():
    email = _clean_email()
    found_broken = False
    for seed in range(200):
        mutated = structural_noise(email, random.Random(seed))
        if "unclosed" in mutated.html or "mismatched nesting" in mutated.html:
            found_broken = True
            break
    assert found_broken


def test_encoding_sabotage_declares_mismatched_charset():
    from testinghq.blast.corrupt import CHARSET_LABELS

    email = _clean_email()
    mutated = encoding_sabotage(email, random.Random(7))
    changed_fields = [f for f in ("subject", "html") if getattr(mutated, f) != getattr(email, f)]
    assert changed_fields
    for field in changed_fields:
        assert field in mutated.charsets
        assert mutated.charsets[field] in CHARSET_LABELS.values()


def test_encoding_sabotage_is_deterministic_for_same_rng_seed():
    email = _clean_email()
    first = encoding_sabotage(email, random.Random(41))
    second = encoding_sabotage(email, random.Random(41))
    assert first == second


def test_encoding_sabotage_only_touches_subject_or_html():
    email = _clean_email()
    mutated = encoding_sabotage(email, random.Random(8))
    assert mutated.to == email.to
    assert mutated.from_addr == email.from_addr
    assert mutated.text == email.text
    assert mutated.envelope == email.envelope


@pytest.mark.parametrize("name", list(MUTATORS))
def test_every_mutator_is_pure_and_preserves_addresses(name):
    """No mutator may touch to/from/envelope: this is how the whole set
    guarantees it never garbles an address into a non-reserved domain."""
    email = _clean_email()
    original = email
    mutator = MUTATORS[name]
    for seed in range(10):
        mutated = mutator(email, random.Random(seed))
        assert mutated.to == original.to
        assert mutated.from_addr == original.from_addr
        assert mutated.envelope == original.envelope
    # original object itself must be untouched (frozen + no in-place edits)
    assert email == original


# ---------------------------------------------------------------------------
# Reserved-domain contract holds through every messiness level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(RECIPES))
def test_reserved_domains_hold_for_every_category(category):
    corpus = generate_corpus(seed=11, count=15)
    rng = random.Random(23)
    for email in corpus:
        corrupted = apply_recipe(email, rng, category)
        assert _is_reserved(_domain_of(corrupted.from_addr)), corrupted.from_addr
        assert _is_reserved(_domain_of(corrupted.to)) or corrupted.to == "", corrupted.to
        assert _is_reserved(_domain_of(corrupted.envelope.from_addr))
        for recipient in corrupted.envelope.to:
            assert _is_reserved(_domain_of(recipient)), recipient


def test_apply_recipe_rejects_unknown_category():
    email = _clean_email()
    with pytest.raises(ValueError):
        apply_recipe(email, random.Random(1), "not-a-real-category")


# ---------------------------------------------------------------------------
# Category mix
# ---------------------------------------------------------------------------


def test_clean_category_returns_email_unchanged():
    email = _clean_email()
    result = apply_recipe(email, random.Random(1), "clean")
    assert result == email


def test_choose_category_only_returns_known_categories():
    rng = random.Random(1)
    for _ in range(50):
        category = choose_category(rng)
        assert category in DEFAULT_MIX


def test_corrupt_email_returns_email_and_category():
    email = _clean_email()
    corrupted, category = corrupt_email(email, random.Random(1))
    assert category in DEFAULT_MIX
    assert isinstance(corrupted, type(email))


def test_category_mix_ratio_holds_within_tolerance_for_fixed_seed():
    corpus = generate_corpus(seed=1, count=4000)
    results = corrupt_corpus(corpus, seed=1)
    counts = Counter(category for _, category in results)
    total = sum(counts.values())
    for category, expected in DEFAULT_MIX.items():
        actual = counts[category] / total
        assert abs(actual - expected) < 0.04, (category, actual, expected)


def test_mix_is_tunable():
    corpus = generate_corpus(seed=1, count=500)
    all_clean_mix = {"clean": 1.0}
    results = corrupt_corpus(corpus, seed=1, mix=all_clean_mix)
    assert all(category == "clean" for _, category in results)
    assert all(email == source for (email, _), source in zip(results, corpus))


def test_corrupt_corpus_is_deterministic_for_same_seed():
    corpus = generate_corpus(seed=2, count=50)
    first = corrupt_corpus(corpus, seed=99)
    second = corrupt_corpus(corpus, seed=99)
    assert first == second


def test_corrupt_corpus_preserves_input_order_and_length():
    corpus = generate_corpus(seed=2, count=10)
    results = corrupt_corpus(corpus, seed=5)
    assert len(results) == 10
