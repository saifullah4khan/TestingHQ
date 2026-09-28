"""steady: the transforms and the flakiness maths, as pure functions.

No socket and no pipeline. Every test here is a function of its arguments, so
the maths can be checked against hand-computed values and a report can be
asserted on exactly.

Two things are tested hardest here, because they are the two ways this tool
would produce a confident wrong number:

  a flip rate with nothing to compare must be None, not 0.0
  a variant the readback could not see must not be counted as a flip, and must
  not let its family report as agreed
"""
from __future__ import annotations

import random

import pytest

from testinghq.core import guardrails
from testinghq.pipeline import steady


def _intents() -> list:
    return steady.load_intents()


def _intents_subset(count: int) -> list:
    return steady.load_intents()[:count]


# ---------------------------------------------------------------------------
# The transforms
# ---------------------------------------------------------------------------


def test_every_transform_name_is_in_the_table():
    assert set(steady.TRANSFORM_NAMES) == {
        "add-quoted-history", "add-signature",
        "reflow-whitespace", "reword-greeting", "wrap-forward",
    }
    assert "language" not in steady.TRANSFORM_NAMES, (
        "varying language is what the per-language baselines are for. As a "
        "transform it produced a variant byte-identical to another language's "
        "baseline: the same bytes sent twice, which inflated the pair count"
    )


def test_the_three_reused_transforms_come_from_blast_corrupt():
    """Reuse, not a second implementation. Two copies of a meaning-preserving
    edit would drift, and a drifted edit stops being meaning-preserving."""
    from testinghq.blast import corrupt

    email = _email_with("A complaint body long enough to be worth transforming. " * 2)
    for mine, theirs in (
        (steady.transform_signature, corrupt._op_signature),
        (steady.transform_forward, corrupt._op_forward_header),
        (steady.transform_quoted_history, corrupt._op_quote_chain),
    ):
        for seed in (1, 2):
            assert (
                mine(email, random.Random(seed)).text
                == theirs(email, random.Random(seed)).text
            ), mine.__name__


def test_collapse_body_is_not_used_as_a_transform():
    """It replaces the body with a single space. A family containing that would
    assert that a message with no content means the same as one with a full
    complaint, and every classifier that disagreed would be right to."""
    from testinghq.blast import corrupt

    assert corrupt._op_collapse_body not in steady.TRANSFORMS.values()
    email = _email_with("This is a long enough complaint to be worth rewrapping "
                        "onto several lines for the test.")
    collapsed = corrupt._op_collapse_body(email, random.Random(1))
    assert collapsed.text.strip() == "", "which is why it is not a transform"


def _email_with(text: str, subject: str = "Hello, support team.\n\nKind regards,\nA"):
    from testinghq.blast.payload import Envelope, GroundTruth, InboundEmail

    return InboundEmail(
        to="intake@example.com",
        from_addr="customer@example.com",
        subject="A complaint",
        text=text,
        html=f"<html><body>{text}</body></html>",
        envelope=Envelope(to=("intake@example.com",), from_addr="customer@example.com"),
        ground_truth=GroundTruth(
            from_addr="customer@example.com", subject="A complaint", body_core=text
        ),
    )


def test_reflow_rewraps_without_changing_the_words():
    long_text = " ".join(["word%d" % i for i in range(60)]) + "\n"
    email = _email_with(long_text)
    reflowed = steady.transform_reflow_whitespace(email, random.Random(1))
    assert reflowed is not None
    assert len(reflowed.text.splitlines()) > 1
    assert reflowed.text.split() == email.text.split(), "no word may change"
    assert reflowed.subject == email.subject, "the subject is not touched"


def test_reflow_declines_rather_than_returning_the_input_unchanged():
    """A transform that silently does nothing appears in the report as a variant
    that agreed, which is a pass the run did not earn."""
    email = _email_with("Too short.\n")
    assert steady.transform_reflow_whitespace(email, random.Random(1)) is None


def test_reflow_returns_none_when_it_would_change_nothing():
    email = _email_with(" ".join(["ab"] * 40) + "\n")
    # Whatever width is chosen, the text is one long line either way, so either
    # the result differs or the transform declines. Both are correct; emitting
    # an identical payload is not.
    produced = steady.transform_reflow_whitespace(email, random.Random(1))
    assert produced is None or produced.text != email.text


def test_reword_swaps_a_greeting():
    email = _email_with("Hello, the kettle is broken.\n\nKind regards,\nA")
    reworded = steady.transform_reword_greeting(email, random.Random(1))
    assert reworded is not None
    assert reworded.text.startswith("Hi,")
    assert "the kettle is broken" in reworded.text, "only the greeting changes"


def test_reword_swaps_a_signoff_when_there_is_no_greeting_to_swap():
    email = _email_with("The kettle is broken.\n\nKind regards,\nA")
    reworded = steady.transform_reword_greeting(email, random.Random(1))
    assert reworded is not None
    assert reworded.text.endswith("Best regards,\nA")
    assert "The kettle is broken" in reworded.text


def test_reword_appends_a_signoff_when_there_is_no_greeting_to_swap():
    """The reviewed fixture's bodies are bare complaints, so a transform that
    only swapped an existing greeting would never apply to the text the tool
    actually sends, while still appearing in the report as a transform that was
    tried."""
    email = _email_with("The kettle is broken.\n")
    reworded = steady.transform_reword_greeting(email, random.Random(1))
    assert reworded is not None
    assert reworded.text.rstrip().endswith("Kind regards,")
    assert "The kettle is broken" in reworded.text


def test_reword_always_produces_a_variant():
    """It swaps a greeting, swaps a sign-off, or appends one, so it never
    declines. Simpler than the other four transforms, and a transform that can
    decline has to justify every decline."""
    for body in (
        "Hello, the kettle is broken.\n",
        "A complaint with no opener.\n\nRegards,\nA\n",
        "A bare complaint with neither.\n",
    ):
        email = _email_with(body)
        produced = steady.transform_reword_greeting(email, random.Random(1))
        assert produced is not None, body
        assert produced.text != email.text, body


def test_every_transform_keeps_the_addresses_synthetic():
    for name, transform in steady.TRANSFORMS.items():
        email = _email_with("A reasonably long complaint body for the transform. " * 3)
        produced = transform(email, random.Random(2))
        assert produced is not None, name
        guardrails.require_synthetic_content(
            [
                produced.to,
                produced.from_addr,
                produced.envelope.from_addr,
                *produced.envelope.to,
            ]
        )


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


def test_a_family_holds_the_baseline_in_every_language():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    baselines = [v for v in family.variants if v.transform == "baseline"]
    assert len(baselines) == 3
    assert {v.language for v in baselines} == {"en", "ur", "roman_ur"}


def test_a_family_covers_every_transform_at_least_once():
    families = steady.build_families(1, intents=_intents_subset(1))
    used = {v.transform for v in families[0].variants}
    for name in steady.TRANSFORM_NAMES:
        assert name in used, name


def test_no_variant_is_identical_to_its_own_baseline():
    """The property that stops a family claiming agreement it did not earn."""
    from testinghq.core import report as engine_report

    for family in steady.build_families(5):
        for variant in family.variants:
            if variant.transform == "baseline":
                continue
            baseline = next(
                (
                    b
                    for b in family.variants
                    if b.transform == "baseline" and b.language == variant.language
                ),
                None,
            )
            if baseline is None:
                continue
            assert (
                engine_report.payload_sha256(variant.email)
                != engine_report.payload_sha256(baseline.email)
            ), f"{family.intent_id}/{variant.language}/{variant.transform} is a copy"


def test_families_are_deterministic():
    first = steady.build_families(7, intents=_intents_subset(3))
    second = steady.build_families(7, intents=_intents_subset(3))
    assert [
        [(v.transform, v.language) for v in f.variants] for f in first
    ] == [[(v.transform, v.language) for v in f.variants] for f in second]


def test_a_different_seed_changes_the_text_but_not_the_transform_set():
    """The seed picks a rewrap width, not which transforms apply. `reflow-whitespace`
    may legitimately decline at a width that reproduces its input, so the set is
    compared over the transforms that always apply."""
    always = {"add-quoted-history", "add-signature", "reword-greeting", "wrap-forward"}
    first = steady.build_families(1, intents=_intents_subset(3))
    second = steady.build_families(99, intents=_intents_subset(3))

    def applied(families):
        return [
            {(v.transform, v.language) for v in f.variants if v.transform in always}
            for f in families
        ]

    assert applied(first) == applied(second)
    # And reflow does appear across a spread of seeds, so it is not silently
    # declining every time.
    applied_reflow = set()
    for seed in range(8):
        family = steady.build_families(seed, intents=_intents_subset(1))[0]
        applied_reflow.add(
            sum(1 for v in family.variants if v.transform == "reflow-whitespace")
        )
    assert any(applied_reflow), (
        "reflow-whitespace never applied across eight seeds"
    )


def test_an_unknown_transform_is_refused():
    with pytest.raises(ValueError) as caught:
        steady.build_families(1, transforms=["translate-at-runtime"])
    assert "unknown transform" in str(caught.value)


def test_a_transform_subset_is_honoured():
    families = steady.build_families(
        1, intents=_intents_subset(1), transforms=["add-signature"]
    )
    assert {v.transform for v in families[0].variants} == {"baseline", "add-signature"}


def test_the_baseline_can_be_left_out():
    families = steady.build_families(
        1, intents=_intents_subset(1), transforms=["add-signature"],
        include_baseline=False,
    )
    assert "baseline" not in {v.transform for v in families[0].variants}


def test_every_payload_in_every_family_is_synthetic():
    for family in steady.build_families(4, intents=_intents_subset(3)):
        for variant in family.variants:
            email = variant.email
            guardrails.require_synthetic_content(
                [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]
            )


def test_build_items_tags_every_variant():
    from testinghq.pipeline.messages import TAG_HEADER

    families = steady.build_families(2, intents=_intents_subset(2))
    items = steady.build_items(families)
    expected = sum(len(f.variants) for f in families)
    assert len(items) == expected
    tags = [tag for _e, tag, _r, _p in items]
    assert len(set(tags)) == len(tags), "a readback needs a unique tag per variant"
    for email, tag, record_id, _probe in items:
        assert email.headers[TAG_HEADER] == tag
        assert record_id.startswith("steady-")


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------


def test_pair_counts():
    assert steady.total_pairs(0) == 0
    assert steady.total_pairs(1) == 0
    assert steady.total_pairs(2) == 1
    assert steady.total_pairs(3) == 3
    assert steady.total_pairs(4) == 6


def test_all_agreeing_flips_nothing():
    assert steady.flipped_pairs(["a", "a", "a", "a"]) == 0


def test_two_labels_among_four_flip_nine_of_six_pairs():
    """Hand-computed: 4 variants, 3 and 1. Same-label pairs are C(3,2) + C(1,2)
    = 3, and C(4,2) = 6, so 3 pairs differ."""
    assert steady.flipped_pairs(["a", "a", "a", "b"]) == 3


def test_a_three_way_split_flips_every_pair():
    assert steady.flipped_pairs(["a", "b", "c"]) == 3


def test_an_unlabelled_variant_is_not_a_flip():
    """It is a variant the readback could not see. Counting it as a flip would
    turn an observation gap into a classifier finding."""
    assert steady.flipped_pairs(["a", "a", None]) == 0
    assert steady.flipped_pairs(["a", "b", None]) == 1


def test_no_families_is_no_flip_rate_rather_than_zero():
    """The single most important property here. A run that measured nothing has
    not shown a stable classifier."""
    report = steady.StabilityReport(())
    assert report.flip_rate is None
    assert "NOTHING MEASURED" in steady.format_stability(report)


def test_an_unlabelled_family_does_not_count_as_agreeing():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    unlabelled = family.labelled([None] * len(family.variants))
    report = steady.StabilityReport((unlabelled,))
    result = report.results[0]
    assert result.consistent is True, "nothing disagreed, because nothing was compared"
    assert result.fully_labelled is False
    assert report.unlabelled_families, "and it must not be reported as measured"
    assert report.flip_rate is None


def test_a_partially_labelled_family_excludes_the_gaps():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    labels = [None] * len(family.variants)
    labels[0] = labels[1] = "billing"
    report = steady.StabilityReport((family.labelled(labels),))
    assert report.pairs == 1
    assert report.flip_rate == 0.0
    assert report.unlabelled_families


def test_labelling_with_the_wrong_number_of_labels_is_refused():
    """Silently zipping a short list would leave the tail unlabelled and a family
    that agreed on nothing would report as agreeing on most of it."""
    family = steady.build_families(1, intents=_intents_subset(1))[0]
    with pytest.raises(ValueError) as caught:
        family.labelled(["billing"] * 2)
    assert "labels" in str(caught.value)


def test_the_majority_label_is_chosen_and_ties_are_broken_by_name():
    assert steady.majority_label(["a", "a", "b"]) == "a"
    assert steady.majority_label(["a", "b"]) == "a", "a tie must not depend on order"
    assert steady.majority_label(["b", "a"]) == "a"
    assert steady.majority_label([None, None]) is None
    assert steady.majority_label(["a", None]) == "a"


def test_a_family_that_agrees_on_the_wrong_label_is_consistent():
    """Stability is a property of the family, not of the answer. `verify` judges
    the answer."""
    family = steady.build_families(1, intents=_intents_subset(1))[0]
    labelled = family.labelled(["nonsense"] * len(family.variants))
    result = steady.judge_family(labelled)
    assert result.consistent is True
    assert result.majority == "nonsense"


def test_a_flipping_family_names_its_dissenters_and_their_transforms():
    family = steady.build_families(1, intents=_intents_subset(1))[0]
    labels = [family.category_hint] * len(family.variants)
    labels[1] = "wrong"
    result = steady.judge_family(family.labelled(labels))
    assert result.consistent is False
    assert result.majority == family.category_hint
    assert result.dissenters == ((family.variants[1].transform, "wrong"),)
    assert result.flips > 0


# ---------------------------------------------------------------------------
# Repeat instability, kept apart from the flip rate
# ---------------------------------------------------------------------------


def test_identical_repeats_that_agree_are_perfectly_stable():
    report = steady.StabilityReport((), repeats=5, repeat_labels=("a",) * 5)
    assert report.repeat_instability == 0.0


def test_repeats_that_disagree_are_measured_against_the_most_common():
    report = steady.StabilityReport((), repeats=4, repeat_labels=("a", "a", "a", "b"))
    assert report.repeat_instability == pytest.approx(0.25)


def test_repeats_are_none_when_they_were_not_all_labelled():
    report = steady.StabilityReport((), repeats=3, repeat_labels=("a", "a", None))
    assert report.repeat_instability is None, (
        "an unlabelled repeat is a gap, not agreement"
    )


def test_repeats_are_none_when_there_were_fewer_than_two():
    assert steady.StabilityReport((), repeats=1).repeat_instability is None
    assert steady.StabilityReport((), repeats=3).repeat_instability is None


def test_the_two_numbers_are_reported_separately():
    """A team that sees 8% and does not know which number it is looking at will
    fix the wrong thing."""
    families = steady.build_families(1, intents=_intents_subset(1))
    labels = [families[0].category_hint] * len(families[0].variants)
    labels[0] = "wrong"
    report = steady.StabilityReport(
        (families[0].labelled(labels),), repeats=4,
        repeat_labels=("a", "a", "a", "a"),
    )
    assert report.flip_rate > 0
    assert report.repeat_instability == 0.0
    text = steady.format_stability(report)
    assert "repeat instability" in text
    assert "STABILITY, not correctness" in text


# ---------------------------------------------------------------------------
# The per-transform breakdown
# ---------------------------------------------------------------------------


def test_the_per_transform_breakdown_names_the_worst_first():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    labels = [family.category_hint] * len(family.variants)
    for index, variant in enumerate(family.variants):
        if variant.transform == "add-signature":
            labels[index] = "wrong"
    report = steady.StabilityReport((family.labelled(labels),))
    by_transform = report.by_transform()
    assert by_transform["add-signature"]["dissenters"] >= 1
    assert by_transform["add-signature"]["dissent_rate"] > 0
    rates = [
        bucket["dissent_rate"] or 0.0 for bucket in by_transform.values()
    ]
    assert rates == sorted(rates, reverse=True), "worst first"
    assert "add-signature" in steady.format_stability(report)


def test_a_transform_with_no_dissent_reports_zero_rather_than_none():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    report = steady.StabilityReport(
        (family.labelled([family.category_hint] * len(family.variants)),)
    )
    for bucket in report.by_transform().values():
        assert bucket["dissent_rate"] == 0.0
        assert bucket["dissenters"] == 0


def test_the_report_carries_the_caveat():
    """A reader who takes 0% as accuracy will act on a number that cannot
    support the decision."""
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    report = steady.StabilityReport(
        (family.labelled([family.category_hint] * len(family.variants)),)
    )
    text = steady.format_stability(report)
    assert "STABLE" in text
    assert "STABILITY, not correctness" in text
    assert "consistently wrong" in text
    assert "testinghq verify" in text


def test_a_report_serializes_with_every_number_it_prints():
    families = steady.build_families(1, intents=_intents_subset(1))
    family = families[0]
    labels = [family.category_hint] * len(family.variants)
    labels[0] = "wrong"
    payload = steady.StabilityReport((family.labelled(labels),)).to_json()
    for key in (
        "label_field", "families", "pairs", "flips", "flip_rate",
        "inconsistent_families", "unlabelled_families", "repeats",
        "repeat_instability", "by_transform",
    ):
        assert key in payload, key
    assert payload["flip_rate"] > 0
    assert payload["inconsistent_families"] == [family.intent_id]
