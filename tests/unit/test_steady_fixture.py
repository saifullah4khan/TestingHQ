"""The reviewed intent fixture `steady` measures against.

The whole point of this file is that the tool does NOT machine-translate. The
claim each entry makes is that a human triager asked "what does this person
want" would give all three languages the same answer, and that claim is only
worth anything if a person checked it. Shipping a translator would make the tool
measure the translator.

Every number `steady` reports rests on this file being believed, so these tests
check the parts of that claim a machine can check, and leave the meaning to the
person reviewing each line.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "testinghq" / "pipeline" / "fixtures" / "steady_intents.json"
)

REQUIRED_LANGUAGES = ("en", "ur", "roman_ur")


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_is_valid_json_with_a_known_schema(fixture):
    assert fixture["schema_version"] == 1
    assert len(fixture["intents"]) >= 8, (
        "a fixture with a handful of intents gives a flip rate with a "
        "meaningless confidence interval"
    )


def test_the_review_instructions_are_in_the_file(fixture):
    """The file has to explain itself to whoever opens it. A reviewer who has
    to read the source to find out what they are being asked to check will not
    check it."""
    for key in ("_about", "_how_to_review", "_why_not_generated", "_edit_this_first"):
        assert key in fixture, key
        assert len(fixture[key]) > 40, f"{key} is too thin to help a reviewer"


def test_every_intent_has_an_id_a_category_and_a_note(fixture):
    for intent in fixture["intents"]:
        assert intent["id"], "an intent with no id cannot be reported on"
        assert intent["category_hint"], (
            f"{intent['id']} has no category_hint; the tool needs one to say "
            f"what a flip away from it would mean"
        )
        assert len(intent["note"]) > 20, (
            f"{intent['id']} has no usable note explaining what makes this "
            f"intent distinct from the others"
        )


def test_every_intent_is_written_in_all_three_languages(fixture):
    for intent in fixture["intents"]:
        variants = intent["variants"]
        for language in REQUIRED_LANGUAGES:
            assert language in variants, f"{intent['id']} is missing {language}"
            text = variants[language].strip()
            assert len(text) > 20, f"{intent['id']}/{language} is too short to be a request"
            assert text, f"{intent['id']}/{language} is empty"


def test_the_languages_are_actually_different_scripts(fixture):
    """Not a formatting check. A copy-paste that left all three variants in
    Latin script would pass every other test here and would make the language
    dimension of the tool meaningless.

    `ur` is required to be MOSTLY Arabic rather than entirely, because real
    Urdu email contains Latin for identifiers: the invoice-name-correction
    variant carries `INV-40221`, as a real one would. Demanding zero Latin
    letters would have pushed the fixture to transliterate its own invoice
    number, which is a worse fixture.
    """
    for intent in fixture["intents"]:
        english = intent["variants"]["en"]
        urdu = intent["variants"]["ur"]
        roman = intent["variants"]["roman_ur"]

        def script_profile(text: str):
            counts = {"arabic": 0, "latin": 0, "other": 0}
            for char in text:
                if "؀" <= char <= "ۿ":
                    counts["arabic"] += 1
                elif char.isalpha() and char.isascii():
                    counts["latin"] += 1
                elif char.isalpha():
                    counts["other"] += 1
            return counts

        en_profile = script_profile(english)
        ur_profile = script_profile(urdu)
        roman_profile = script_profile(roman)

        assert en_profile["arabic"] == 0, f"{intent['id']}: en contains Arabic"
        assert ur_profile["arabic"] > 0, f"{intent['id']}: ur is not in Arabic script"
        assert ur_profile["arabic"] > ur_profile["latin"], (
            f"{intent['id']}: ur is mostly Latin, so it is a transliteration "
            f"wearing the wrong key"
        )
        assert roman_profile["latin"] > 0, f"{intent['id']}: roman_ur has no Latin letters"
        assert roman_profile["arabic"] == 0, f"{intent['id']}: roman_ur contains Arabic"
        del en_profile, roman_profile


def test_no_variant_is_a_translation_of_another_in_the_same_script(fixture):
    """Roman Urdu and English are both Latin script, so the script check above
    cannot separate them. This one can: two identical variants would make a
    flip in that language undetectable by construction."""
    for intent in fixture["intents"]:
        variants = intent["variants"]
        for language in ("en", "roman_ur"):
            for other in ("en", "roman_ur"):
                if language >= other:
                    continue
                left = unicodedata.normalize("NFC", variants[language]).casefold()
                right = unicodedata.normalize("NFC", variants[other]).casefold()
                assert left != right, (
                    f"{intent['id']}: {language} and {other} are the same string"
                )


def test_intent_ids_are_unique_and_kebab_case(fixture):
    """Ids become tags, config keys and report keys, so they have to be usable
    in all three without quoting."""
    seen = set()
    for intent in fixture["intents"]:
        identifier = intent["id"]
        assert identifier not in seen, f"duplicate intent id {identifier!r}"
        seen.add(identifier)
        assert identifier == identifier.lower()
        assert all(c.isalnum() or c == "-" for c in identifier), identifier
        assert not identifier.startswith("-") and not identifier.endswith("-")


def test_category_hints_are_drawn_from_a_small_enough_set_to_be_meaningful(fixture):
    """A category per intent is only useful if the categories are few. Fifty
    of them would make every flip a move to a new category and the report
    useless."""
    categories = {intent["category_hint"] for intent in fixture["intents"]}
    assert len(categories) <= len(fixture["intents"]) / 2, (
        f"{len(categories)} categories across {len(fixture['intents'])} intents; "
        "the labels have to overlap for a flip to mean anything"
    )
    assert len(categories) >= 4, "too few categories to tell a flip from a rerouting"


def test_at_least_two_intents_share_a_category(fixture):
    """The case that makes the tool able to report a flip: a classifier that
    moves one of a pair to a different label has to move the other too, and
    that is only visible when two intents share a label."""
    from collections import Counter

    counts = Counter(intent["category_hint"] for intent in fixture["intents"])
    assert max(counts.values()) >= 2, (
        f"no two intents share a category, so no flip can be checked against a "
        f"peer: {dict(counts)}"
    )


def test_the_file_is_utf8_and_stays_that_way(fixture):
    """Round-tripped, so a reviewer opening it in something that guessed the
    encoding would not see mojibake in a file whose entire job is being read
    carefully."""
    raw = FIXTURE.read_bytes()
    assert raw.decode("utf-8")
    assert json.loads(raw.decode("utf-8")) == fixture
