"""Integration: the real generator, the real mutators, the real wire format.

The other integration file in this directory, test_intake_happy_path.py, was
written when `testinghq/blast/generate.py` did not exist. It says so in its
docstring and hand-builds three clean payloads instead of generating them. Its
own note says what should happen once the generator lands: drive the same
assertions from a seeded corpus rather than a hand-written list. This file is
that, and it covers the two things a hand-built clean list structurally cannot:

1. The mutators. `corrupt.py` is a 23KB pipeline with six mutators and five
   recipes, and every one of those is tested in isolation in
   tests/unit/test_corrupt.py. Nothing tested them together, end to end,
   through a real serialization and a real decode.

2. The category mix. `DEFAULT_MIX` weights are what the CLI's dry-run preview
   reports and what a fire run's `by_category` tally reports. Nothing checked
   that the weights are what the engine actually produces, or that all five
   categories are reachable at all.

Hermetic: no sockets. Payloads are delivered to the in-process fake sink, which
is what "dry run" means throughout Blast.
"""
import pytest
from fake_sink import FakeSink

from testinghq.blast.corrupt import (
    DEFAULT_MIX,
    MUTATORS,
    RECIPES,
    corrupt_corpus,
)
from testinghq.blast.generate import generate_corpus
from testinghq.blast.serialize import to_multipart_parts
from testinghq.core import report

# Wide enough that every category is drawn many times over even at a 5% weight,
# and small enough to stay instant.
SEED = 20260927
COUNT = 400


@pytest.fixture(scope="module")
def corrupted():
    """The corpus the CLI's own _build_corpus would build: clean generation,
    then DEFAULT_MIX corruption, then the engine's label translation."""
    return corrupt_corpus(generate_corpus(SEED, COUNT), SEED, DEFAULT_MIX)


# ---------------------------------------------------------------------------
# Every mutator is reachable, and every recipe is buildable from them
# ---------------------------------------------------------------------------


def test_every_mutator_is_referenced_by_at_least_one_recipe():
    """A mutator nothing reaches is dead code that looks tested. This is the
    cheap structural check that the recipe table and the mutator table have
    not drifted apart."""
    referenced = {name for recipe in RECIPES.values() for name, _lo, _hi in recipe}
    assert referenced == set(MUTATORS), (
        "mutators with no recipe referencing them: "
        f"{sorted(set(MUTATORS) - referenced)}; recipe entries naming a "
        f"mutator that does not exist: {sorted(referenced - set(MUTATORS))}"
    )


def test_a_recipe_only_ever_names_known_mutators():
    """The mirror of the above, so a typo in either direction is caught."""
    for category, recipe in RECIPES.items():
        for name, lo, hi in recipe:
            assert name in MUTATORS, f"{category} names unknown mutator {name!r}"
            assert 0 <= lo <= hi, f"{category}/{name} has an inverted range"


@pytest.mark.parametrize("category", sorted(RECIPES))
def test_each_recipe_can_be_built_in_isolation(category):
    """Force a single recipe over a real corpus, so the whole 400-payload
    default mix is not the only thing exercising any of them."""
    weight = {category: 1.0}
    pairs = corrupt_corpus(generate_corpus(SEED, 60), SEED, weight)

    assert pairs, f"{category} produced no payloads"
    assert {recipe for _email, recipe in pairs} == {category}


def test_clean_recipe_leaves_the_payload_untouched():
    """`clean` is the only recipe with an empty mutator list, so it is the
    control for every other assertion in this file. If a mutator were being
    applied to clean payloads, the round-trip identity below would break, and
    that would be a very bad bug: clean is the category that is supposed to
    represent a well-formed message."""
    clean = generate_corpus(SEED, 25)
    pairs = corrupt_corpus(clean, SEED, {"clean": 1.0})

    for original, (mutated, _category) in zip(clean, pairs):
        assert report.payload_sha256(mutated) == report.payload_sha256(original)


def test_every_non_clean_recipe_actually_changes_something():
    """The mirror: if a recipe is supposed to garble a payload and does not,
    then a fire run is silently testing nothing. Some mutators have ranges
    that can apply zero times, so this is checked across a decent corpus
    rather than payload by payload."""
    for category in sorted(RECIPES):
        if category == "clean":
            continue
        clean = generate_corpus(SEED, 120)
        pairs = corrupt_corpus(clean, SEED, {category: 1.0})
        changed = sum(
            1
            for original, (mutated, _c) in zip(clean, pairs)
            if report.payload_sha256(mutated) != report.payload_sha256(original)
        )
        assert changed > 0, (
            f"recipe {category!r} left every payload byte-identical; the "
            "mutators it names are not being applied"
        )


# ---------------------------------------------------------------------------
# The category mix
# ---------------------------------------------------------------------------


def test_all_five_categories_are_reachable_from_the_default_mix(corrupted):
    labels = {report.category_label(recipe) for _email, recipe in corrupted}
    assert labels == set(report.CATEGORIES), (
        f"the default mix never produced {sorted(set(report.CATEGORIES) - labels)}"
    )


def test_the_observed_distribution_tracks_the_configured_weights(corrupted):
    """A weight table that does not match what the engine produces means the
    dry-run preview, which prints the tally, is lying to the operator.

    Deliberately loose. These are random draws, so an exact assertion would be
    a flaky test rather than a strict one. The tolerance is wide enough that
    only a real weighting failure trips it, and narrow enough that an inverted
    or ignored weight does.
    """
    count = len(corrupted)
    observed = {}
    for _email, recipe in corrupted:
        label = report.category_label(recipe)
        observed[label] = observed.get(label, 0) + 1

    for recipe, weight in DEFAULT_MIX.items():
        label = report.category_label(recipe)
        expected = weight * count
        assert observed[label] == pytest.approx(expected, abs=count * 0.06), (
            f"{label}: configured weight {weight} implies about {expected:.0f} "
            f"payloads, observed {observed[label]}"
        )


def test_every_corpus_pair_survives_the_real_wire_format(corrupted):
    """The point of the whole pipeline: whatever the mutators did, the fake
    sink can still be handed the bytes and hand something back. A mutator that
    produced an unserializable payload would raise here rather than at a
    customer's endpoint.

    A degenerate payload is allowed to decode to nonsense; what is not allowed
    is for the decode to raise.
    """
    sink = FakeSink()
    for email, _recipe in corrupted:
        sink.receive_parts(to_multipart_parts(email))

    assert len(sink.received) == len(corrupted)


def test_clean_payloads_still_match_their_ground_truth_after_a_round_trip():
    """The property the product actually sells: a clean payload's decoded
    fields equal what the generator said they should be. Driven from a seeded
    corpus this time rather than a hand-built list, so it covers whatever the
    generator produces rather than three examples someone chose.

    Note the two assertions that are containment rather than equality, both
    deliberate, and both places where the hand-built list in
    test_intake_happy_path.py asserted something that could not fail.

    The generator puts the RFC 5322 display-name form in the header
    (`Dave Chen <dave.chen9@example.net>`) and the bare addr-spec in ground
    truth, because extracting that addr-spec is the parser's job and is the
    entire point of carrying ground truth. It also puts a full message in
    `text`, greeting and sign-off included, and only the substantive sentence
    in `ground_truth.body_core`. The name says core for a reason: a parser is
    not expected to reproduce the boilerplate.

    The hand-built list used bare addresses and set `body_core` equal to
    `text`, so its equalities held only for a shape the real generator never
    emits. Those assertions had never been exercised against real data.
    """
    clean = generate_corpus(SEED, 50)
    sink = FakeSink()
    for email in clean:
        sink.receive_parts(to_multipart_parts(email))

    for email, response in zip(clean, sink.received):
        decoded = response.decoded
        assert email.ground_truth.from_addr in decoded.from_addr, (
            "the header must carry the addr-spec that ground truth names"
        )
        assert email.ground_truth.subject == decoded.subject
        assert email.ground_truth.body_core in decoded.text, (
            "the decoded body must contain the core the ground truth names"
        )
        assert email.ground_truth.body_core != email.text, (
            "body_core is the core, not the whole message; if they are equal "
            "this test's containment assertion has stopped testing anything"
        )
        assert decoded.to == email.to
        assert decoded.html == email.html
        assert decoded.attachment_count == len(email.attachments)


def test_the_envelope_carries_the_bare_addr_spec_and_the_header_the_display_form():
    """The distinction the assertion above rests on, pinned directly so it
    cannot be quietly collapsed. If a future generator change put the bare
    address in the header, the ground-truth contract would stop testing
    anything a real parser has to do, and nothing else here would notice."""
    email = generate_corpus(SEED, 1)[0]

    assert email.envelope.from_addr == email.ground_truth.from_addr
    assert email.from_addr.endswith(f"<{email.ground_truth.from_addr}>")
    assert " " in email.from_addr.split("<")[0], (
        "the header's display-name part is what makes this a parser's job"
    )


def test_attachment_count_matches_after_the_round_trip():
    """`encoding_sabotage` rewrites charsets, so the attachment-count field
    in particular is worth checking separately: it is the one field whose
    serialization is a count rather than the attachment data itself."""
    pairs = corrupt_corpus(generate_corpus(SEED, 150), SEED, DEFAULT_MIX)
    sink = FakeSink()
    for email, _recipe in pairs:
        sink.receive_parts(to_multipart_parts(email))

    for (email, _recipe), response in zip(pairs, sink.received):
        assert response.decoded.attachment_count == len(email.attachments)


# ---------------------------------------------------------------------------
# Determinism, end to end through the corruptor
# ---------------------------------------------------------------------------


def test_the_same_seed_reproduces_the_same_bytes(corrupted):
    again = corrupt_corpus(generate_corpus(SEED, COUNT), SEED, DEFAULT_MIX)
    assert [report.payload_sha256(e) for e, _ in corrupted] == [
        report.payload_sha256(e) for e, _ in again
    ]


def test_a_different_seed_produces_different_bytes(corrupted):
    other = corrupt_corpus(generate_corpus(SEED + 1, COUNT), SEED + 1, DEFAULT_MIX)
    assert [report.payload_sha256(e) for e, _ in corrupted] != [
        report.payload_sha256(e) for e, _ in other
    ]
