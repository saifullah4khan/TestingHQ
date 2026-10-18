"""The reviewed intent fixture `steady` measures against.

The tool does not machine-translate: each entry claims a person read all three
languages and would triage them the same way. These tests check the parts of
that claim a machine can check, through the loader the tool itself uses.
"""
from __future__ import annotations

from collections import Counter

from testinghq.pipeline import steady

REQUIRED_LANGUAGES = ("en", "ur", "roman_ur")


def _script(text: str) -> Counter:
    counts: Counter = Counter()
    for char in text:
        if "؀" <= char <= "ۿ":
            counts["arabic"] += 1
        elif char.isalpha() and char.isascii():
            counts["latin"] += 1
    return counts


def test_every_intent_loads_in_every_language_with_subjects_and_closings():
    fixture = steady.load_fixture()
    assert set(fixture.closings) == set(REQUIRED_LANGUAGES)
    for intent in fixture.intents:
        assert set(intent.variants) == set(REQUIRED_LANGUAGES), intent.id
        for language in REQUIRED_LANGUAGES:
            assert intent.variants[language].strip(), (intent.id, language)
            assert intent.subjects.get(language), (intent.id, language)


def test_the_languages_are_actually_different_scripts():
    """`ur` is mostly Arabic script (an invoice number may stay Latin);
    `roman_ur` and `en` have none."""
    fixture = steady.load_fixture()
    for intent in fixture.intents:
        for language in REQUIRED_LANGUAGES:
            texts = [intent.variants[language], *intent.subjects[language]]
            if language == "ur":
                texts += list(fixture.closings["ur"])
            for text in texts:
                profile = _script(text)
                if language == "ur":
                    assert profile["arabic"] > profile["latin"], (intent.id, text)
                else:
                    assert profile["arabic"] == 0, (intent.id, language, text)


def test_ids_are_unique_and_categories_are_shared():
    """A flip is only checkable against a peer meant to get the same label, so
    at least two intents share a category hint."""
    intents = steady.load_fixture().intents
    ids = [intent.id for intent in intents]
    assert len(ids) == len(set(ids))
    counts = Counter(intent.category_hint for intent in intents)
    assert max(counts.values()) >= 2
