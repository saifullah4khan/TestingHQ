"""steady: the transforms and the flakiness maths, as pure functions.

No send path, no readback, no CLI. A family is built, labels are handed to it,
and it reports how many pairs disagreed. Everything here is a function of its
arguments, so the whole tool can be tested without a socket and a report can be
checked against a hand-computed one.

THE CLAIM UNDER TEST. A family is a set of payloads a person would agree mean
the same thing. The tool does not check that; it assumes it, which is what
`fixtures/steady_intents.json` is for. What it measures is whether the
classifier's label held still across the family.

FLIP RATE AND REPEAT INSTABILITY ARE SEPARATE NUMBERS, and the split is the
point of the tool. One is sensitivity to how the input is worded; the other is
the model answering identical bytes differently. A team that sees 8% and does not
know which one it is looking at will go and fix the wrong thing.

A FLIP RATE WITH NO LABELLED PAIRS IS NONE, NOT ZERO. A run where the readback
could not see the label has not shown a stable classifier, and 0.0 would be the
most confident lie this module could tell.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.corrupt import _op_forward_header, _op_quote_chain, _op_signature
from ..blast.payload import InboundEmail

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "steady_intents.json"

#: The field the label is read from. Backlog task 1 would add `category` and
#: `priority` to `Readback`; until it lands `route` is the only one of the three
#: that exists, and this default names it. Switched when task 1 merges.
LABEL_FIELD = "route"
LABEL_FIELDS = ("category", "route", "priority")

TRANSFORM_BASELINE = "baseline"


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """One reviewed intent and its language variants."""

    id: str
    category_hint: str
    note: str
    variants: Dict[str, str]

    def body_for(self, language: str) -> Optional[str]:
        return self.variants.get(language)


def load_intents(path: Optional[Path] = None) -> List[Intent]:
    """The reviewed intents, in file order so a run is reproducible.

    A malformed or empty fixture raises rather than being skipped. A tool that
    ran on half its families and reported a flip rate over those would be
    reporting a number about a different experiment than the one it names.
    """
    source = path or FIXTURE_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    intents = [
        Intent(
            id=entry["id"],
            category_hint=entry["category_hint"],
            note=entry["note"],
            variants=dict(entry["variants"]),
        )
        for entry in raw["intents"]
    ]
    if not intents:
        raise ValueError(f"{source} has no intents")
    return intents


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

#: `(email, rng) -> Optional[InboundEmail]`. None means the transform does not
#: apply to this payload, which is not the same as applying nothing: a variant
#: byte-identical to its own baseline would be reported as a pass the run did
#: not earn.
Transform = Callable[[InboundEmail, random.Random], Optional[InboundEmail]]

_WHITESPACE_RUN = re.compile(r"[ \t]{2,}")

#: Greeting and sign-off pairs that mean the same thing, so a classifier that
#: keys on them rather than on the content is what this finds. A fixed table
#: rather than a generator, because "these mean the same thing" is the claim the
#: whole tool rests on and a generator cannot make it auditable.
_GREETING_SWAPS: Tuple[Tuple[str, str], ...] = (
    ("Hello,", "Hi,"),
    ("Hi,", "Hello,"),
    ("Dear team,", "Hello,"),
    ("Hello team,", "Hi,"),
    ("Good morning,", "Hello,"),
    ("Good afternoon,", "Hi,"),
)
_SIGNOFF_SWAPS: Tuple[Tuple[str, str], ...] = (
    ("Kind regards,", "Best regards,"),
    ("Best regards,", "Regards,"),
    ("Regards,", "Thanks,"),
    ("Many thanks,", "Thanks,"),
)


def _replaced(email: InboundEmail, **changes) -> InboundEmail:
    import dataclasses

    return dataclasses.replace(email, **changes)


def transform_signature(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Add a signature block. Reused from `blast.corrupt`, which is where
    meaning-preserving edits already live."""
    return _op_signature(email, rng)


def transform_forward(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Wrap it as a forwarded message. Reused from `blast.corrupt`."""
    return _op_forward_header(email, rng)


def transform_quoted_history(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Append a quoted reply chain. Reused from `blast.corrupt`."""
    return _op_quote_chain(email, rng)


def transform_reflow_whitespace(
    email: InboundEmail, rng: random.Random
) -> Optional[InboundEmail]:
    """Re-wrap the body and collapse runs of spaces. Nothing else changes.

    `_op_collapse_body` is deliberately NOT used here, though the brief names it
    for this transform. It replaces the body with a single space, which is
    destruction rather than reflow: a family containing a collapsed body would
    assert that a message with no content means the same as one with a full
    complaint, and every classifier that disagreed would be right to.
    """
    lines = [line.strip() for line in email.text.splitlines() if line.strip()]
    joined = " ".join(lines)
    if len(joined) < 40:
        return None
    width = rng.choice((48, 60, 72))
    wrapped: List[str] = []
    current = ""
    for word in joined.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            wrapped.append(current)
            current = word
        else:
            current = candidate
    if current:
        wrapped.append(current)
    if len(wrapped) < 2:
        return None
    text = _WHITESPACE_RUN.sub(" ", "\n".join(wrapped)) + "\n"
    if text == email.text:
        return None
    return _replaced(email, text=text, html=f"<html><body>{text}</body></html>")


def transform_reword_greeting(
    email: InboundEmail, rng: random.Random
) -> Optional[InboundEmail]:
    """Swap an equivalent greeting or sign-off, or append one.

    Appending counts because the reviewed fixture's bodies are bare complaints
    with no greeting and no sign-off, and a transform that never applies to them
    contributes nothing while still appearing in the report as a transform that
    was tried. It therefore always produces a variant, which is simpler than
    the other four: a transform that can decline has to justify every decline,
    and this one has none.
    """
    del rng
    swapped = _swap_first(email.text, _GREETING_SWAPS)
    if swapped is None:
        swapped = _swap_last(email.text, _SIGNOFF_SWAPS)
    if swapped is None:
        return _append_signoff(email)
    if swapped == email.text:
        return None
    return _replaced(email, text=swapped, html=f"<html><body>{swapped}</body></html>")


def _append_signoff(email: InboundEmail) -> InboundEmail:
    """Add the sign-off the body did not have.

    Unreachable guard removed rather than kept as a safety net: the caller has
    already established there is no sign-off to swap, and a branch that cannot
    run is a branch nobody has tested.
    """
    text = email.text.rstrip("\n") + "\n\nKind regards,\n"
    return _replaced(email, text=text, html=f"<html><body>{text}</body></html>")


def _swap_first(text: str, swaps: Sequence[Tuple[str, str]]) -> Optional[str]:
    for original, replacement in swaps:
        if text.startswith(original):
            return replacement + text[len(original) :]
    return None


def _swap_last(text: str, swaps: Sequence[Tuple[str, str]]) -> Optional[str]:
    for original, replacement in swaps:
        marker = f"\n{original}"
        index = text.rfind(marker)
        if index != -1:
            return text[:index] + f"\n{replacement}" + text[index + len(marker) :]
    return None


TRANSFORMS: Dict[str, Transform] = {
    "add-signature": transform_signature,
    "wrap-forward": transform_forward,
    "add-quoted-history": transform_quoted_history,
    "reflow-whitespace": transform_reflow_whitespace,
    "reword-greeting": transform_reword_greeting,
}
#: Language is NOT a transform. Varying it is what the per-language BASELINES
#: are for, and emitting it as a transform too produced a "language" variant
#: byte-identical to another language's baseline: the same bytes sent twice
#: under two names, which inflated the pair count and made a flip between two
#: copies of one payload possible for no reason. `StabilityReport.by_language`
#: reports that dimension instead.
TRANSFORM_NAMES: Tuple[str, ...] = tuple(sorted(TRANSFORMS))


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One member of a family: a payload, what produced it, and what the
    classifier said."""

    intent_id: str
    language: str
    transform: str
    email: InboundEmail
    label: Optional[str] = None

    def with_label(self, label: Optional[str]) -> "Variant":
        return Variant(
            self.intent_id, self.language, self.transform, self.email, label
        )

    def to_json(self) -> Dict[str, Any]:
        from ..core import report as engine_report

        return {
            "intent_id": self.intent_id,
            "language": self.language,
            "transform": self.transform,
            "label": self.label,
            "subject": self.email.subject,
            "payload_sha256": engine_report.payload_sha256(self.email),
        }


@dataclass(frozen=True)
class Family:
    """Every variant of one intent. A family that disagrees is the finding."""

    intent_id: str
    category_hint: str
    variants: Tuple[Variant, ...]

    def labelled(self, labels: Sequence[Optional[str]]) -> "Family":
        """This family with `labels` attached, position by position.

        Length-checked rather than zipped silently: a short label list would
        leave the tail unlabelled and a family that agreed on nothing would be
        reported as agreeing on most of it.
        """
        if len(labels) != len(self.variants):
            raise ValueError(
                f"family {self.intent_id!r} has {len(self.variants)} variants "
                f"but {len(labels)} labels"
            )
        return Family(
            intent_id=self.intent_id,
            category_hint=self.category_hint,
            variants=tuple(
                variant.with_label(label)
                for variant, label in zip(self.variants, labels)
            ),
        )


def _email_for(intent: Intent, language: str) -> InboundEmail:
    """One payload carrying the intent's complaint in `language`.

    Built rather than generated, because the body has to be the fixture's text
    and a generated envelope would put a different complaint in the subject.
    """
    from ..blast.payload import Envelope, GroundTruth

    body = intent.body_for(language) or ""
    sender = "customer@example.com"
    subject = f"[{intent.category_hint}] {intent.id}"
    return InboundEmail(
        to="intake@example.com",
        from_addr=sender,
        subject=subject,
        text=body + "\n",
        html=f"<html><body>{body}</body></html>",
        envelope=Envelope(to=("intake@example.com",), from_addr=sender),
        ground_truth=GroundTruth(
            from_addr=sender, subject=subject, body_core=body
        ),
        headers={},
        charsets={},
        attachments=(),
    )


def build_families(
    seed: int,
    *,
    intents: Optional[Sequence[Intent]] = None,
    transforms: Optional[Sequence[str]] = None,
    include_baseline: bool = True,
) -> List[Family]:
    """One family per intent: the baseline in each language, plus each transform
    applied to each baseline.

    Deterministic in (seed, intents, transforms) and nothing else. A transform
    that does not apply is left out rather than emitted unchanged, so a family
    never contains a variant identical to its own baseline and reports a pass it
    did not earn.
    """
    chosen = list(intents) if intents is not None else load_intents()
    # A caller may still name a transform this build does not have, so the check
    # stays even though the language entry is gone.
    wanted = list(transforms) if transforms is not None else list(TRANSFORM_NAMES)
    unknown = [name for name in wanted if name not in TRANSFORM_NAMES]
    if unknown:
        raise ValueError(
            f"unknown transform(s) {unknown}; known: {list(TRANSFORM_NAMES)}"
        )
    languages = sorted(set(chosen[0].variants)) if chosen else ["en"]

    families: List[Family] = []
    for intent in chosen:
        variants: List[Variant] = []
        for language in languages:
            if not intent.body_for(language):
                continue
            baseline = _email_for(intent, language)
            if include_baseline:
                variants.append(
                    Variant(intent.id, language, TRANSFORM_BASELINE, baseline)
                )
            for name in wanted:
                rng = random.Random(
                    f"testinghq:steady:{seed}:{intent.id}:{language}:{name}"
                )
                produced = TRANSFORMS[name](baseline, rng)
                if produced is None:
                    continue
                variants.append(Variant(intent.id, language, name, produced))
        if variants:
            families.append(
                Family(
                    intent_id=intent.id,
                    category_hint=intent.category_hint,
                    variants=tuple(variants),
                )
            )
    return families


def build_items(families: Sequence[Family]) -> List[Tuple[InboundEmail, str, str]]:
    """(email, tag, record_id) for `common.send_all`.

    Every payload is tagged so the readback can attribute a label back to one
    variant, which is the only way a flip can be blamed on a transform.
    """
    from .messages import make_tag, stamp

    items = []
    for index, family in enumerate(families):
        for position, variant in enumerate(family.variants):
            tag = make_tag("steady", index, position)
            items.append(
                (stamp(variant.email, tag), tag, f"steady-{index:03d}-{position:03d}")
            )
    return items


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------


def total_pairs(count: int) -> int:
    """Unordered pairs within a family. The denominator of the flip rate."""
    return count * (count - 1) // 2


def _counts(labels: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return counts


def flipped_pairs(labels: Sequence[Optional[str]]) -> int:
    """Pairs whose labels differ.

    A missing label is not a flip. It is a variant the readback could not see,
    and counting it would turn an observation gap into a classifier finding.
    """
    known = [label for label in labels if label is not None]
    if len(known) < 2:
        return 0
    return total_pairs(len(known)) - sum(
        total_pairs(count) for count in _counts(known).values()
    )


def majority_label(labels: Sequence[Optional[str]]) -> Optional[str]:
    """The label most of a family's variants agreed on, ties broken by name so
    the answer does not depend on dict order."""
    known = [label for label in labels if label is not None]
    if not known:
        return None
    return min(_counts(known).items(), key=lambda item: (-item[1], item[0]))[0]


@dataclass(frozen=True)
class FamilyResult:
    """One family, and whether it held together."""

    intent_id: str
    category_hint: str
    variants: int
    labelled: int
    majority: Optional[str]
    dissenters: Tuple[Tuple[str, str], ...] = ()
    pairs: int = 0
    flips: int = 0

    @property
    def consistent(self) -> bool:
        return self.flips == 0

    @property
    def fully_labelled(self) -> bool:
        return self.labelled == self.variants

    def to_json(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "category_hint": self.category_hint,
            "variants": self.variants,
            "labelled": self.labelled,
            "majority": self.majority,
            "dissenters": [list(d) for d in self.dissenters],
            "pairs": self.pairs,
            "flips": self.flips,
            "consistent": self.consistent,
        }


def judge_family(family: Family) -> FamilyResult:
    """Compare a family's labels against each other, not against an answer.

    Stability is a property of the family, so a family that agrees on the wrong
    label is consistent, and this reports it as consistent. `verify` judges the
    label, on a clean corpus.
    """
    labels = [variant.label for variant in family.variants]
    majority = majority_label(labels)
    return FamilyResult(
        intent_id=family.intent_id,
        category_hint=family.category_hint,
        variants=len(family.variants),
        labelled=sum(1 for label in labels if label is not None),
        majority=majority,
        dissenters=tuple(
            (variant.transform, variant.label)
            for variant in family.variants
            if variant.label is not None and variant.label != majority
        ),
        pairs=total_pairs(sum(1 for label in labels if label is not None)),
        flips=flipped_pairs(labels),
    )


def judge(families: Sequence[Family]) -> Tuple[FamilyResult, ...]:
    """Every family, judged. A list of a pure function, so a caller can judge one
    family on its own."""
    return tuple(judge_family(f) for f in families)


@dataclass(frozen=True)
class StabilityReport:
    """The flakiness number, and the pieces it is made of.

    Holds the labelled families rather than only their results, because the
    per-transform breakdown needs each variant's transform name and that is not
    on a result.
    """

    families: Tuple[Family, ...]
    repeats: int = 0
    repeat_labels: Tuple[Optional[str], ...] = ()
    label_field: str = LABEL_FIELD

    @property
    def results(self) -> Tuple[FamilyResult, ...]:
        return judge(self.families)

    @property
    def pairs(self) -> int:
        return sum(result.pairs for result in self.results)

    @property
    def flips(self) -> int:
        return sum(result.flips for result in self.results)

    @property
    def flip_rate(self) -> Optional[float]:
        """None when there was nothing to compare.

        Not 0.0. A run where the readback could not see the label has not shown
        a stable classifier, and zero would be the most confident lie this module
        could tell.
        """
        if self.pairs == 0:
            return None
        return self.flips / self.pairs

    @property
    def inconsistent_families(self) -> Tuple[FamilyResult, ...]:
        return tuple(r for r in self.results if not r.consistent)

    @property
    def unlabelled_families(self) -> Tuple[FamilyResult, ...]:
        """Families where the readback could not see the label, reported apart
        from the consistent ones. A family nobody measured is not a family that
        agreed."""
        return tuple(r for r in self.results if not r.fully_labelled)

    @property
    def repeat_instability(self) -> Optional[float]:
        """Share of repeated identical runs that did not agree.

        One minus the share of the most common label, so zero is a classifier
        that answered the same bytes the same way every time. None rather than
        zero when the repeats did not all come back labelled.
        """
        if self.repeats < 2 or not self.repeat_labels:
            return None
        labelled = [label for label in self.repeat_labels if label is not None]
        if len(labelled) < self.repeats:
            return None
        return 1.0 - (max(_counts(labelled).values()) / self.repeats)

    def by_transform(self) -> Dict[str, Dict[str, Any]]:
        """Dissent rate per transform, worst first.

        The actionable output. "The classifier flips 8% of the time" is a number
        to report; "it flips when a signature is added and not otherwise" is a
        bug to go and look at.

        Measured against the family's majority label rather than pairwise, so a
        transform that is always the odd one out shows up instead of averaging
        itself away against a bigger family.
        """
        buckets: Dict[str, Dict[str, int]] = {}
        for family, result in zip(self.families, self.results):
            for variant in family.variants:
                bucket = buckets.setdefault(
                    variant.transform, {"variants": 0, "dissenters": 0}
                )
                if variant.label is None:
                    continue
                bucket["variants"] += 1
                if variant.label != result.majority:
                    bucket["dissenters"] += 1

        def _rate(bucket: Dict[str, int]) -> float:
            return (
                bucket["dissenters"] / bucket["variants"]
                if bucket["variants"]
                else 0.0
            )

        ordered: Dict[str, Dict[str, Any]] = {}
        for name in sorted(buckets, key=lambda n: (-_rate(buckets[n]), n)):
            bucket = buckets[name]
            bucket["dissent_rate"] = _rate(bucket) if bucket["variants"] else None
            ordered[name] = bucket
        return ordered

    def by_language(self) -> Dict[str, Dict[str, Any]]:
        """Dissent rate per language, worst first.

        The language dimension, reported the same way as the transform one
        because it is the same question: which way of saying this did the
        classifier handle worst? Varying language is what the per-language
        baselines are for, so there is no "language" transform to bucket, and a
        separate breakdown is clearer than overloading the transform one.
        """
        buckets: Dict[str, Dict[str, int]] = {}
        for family, result in zip(self.families, self.results):
            for variant in family.variants:
                bucket = buckets.setdefault(
                    variant.language, {"variants": 0, "dissenters": 0}
                )
                if variant.label is None:
                    continue
                bucket["variants"] += 1
                if variant.label != result.majority:
                    bucket["dissenters"] += 1

        def _rate(bucket: Dict[str, int]) -> float:
            return (
                bucket["dissenters"] / bucket["variants"]
                if bucket["variants"]
                else 0.0
            )

        ordered: Dict[str, Dict[str, Any]] = {}
        for name in sorted(buckets, key=lambda n: (-_rate(buckets[n]), n)):
            bucket = buckets[name]
            bucket["dissent_rate"] = _rate(bucket) if bucket["variants"] else None
            ordered[name] = bucket
        return ordered

    def to_json(self) -> Dict[str, Any]:
        return {
            "label_field": self.label_field,
            "families": [result.to_json() for result in self.results],
            "pairs": self.pairs,
            "flips": self.flips,
            "flip_rate": self.flip_rate,
            "inconsistent_families": [
                result.intent_id for result in self.inconsistent_families
            ],
            "unlabelled_families": [
                result.intent_id for result in self.unlabelled_families
            ],
            "repeats": self.repeats,
            "repeat_instability": self.repeat_instability,
            "by_transform": self.by_transform(),
            "by_language": self.by_language(),
        }


def format_stability(report: StabilityReport) -> str:
    """The report, and the caveat that has to be on it.

    A flip rate of zero means the classifier is STABLE. It says nothing about
    whether the classifier is right, and a reader who takes it as accuracy will
    act on a number that cannot support the decision.
    """
    rate = report.flip_rate
    if rate is None:
        verdict = "NOTHING MEASURED"
    elif rate == 0.0:
        verdict = "STABLE"
    elif rate <= 0.05:
        verdict = "MOSTLY STABLE"
    else:
        verdict = "UNSTABLE"

    lines = [
        f"steady: {verdict}  (flip rate "
        f"{'unavailable' if rate is None else format(rate, '.1%')}, label field "
        f"{report.label_field!r})",
        "",
        f"  families:            {len(report.results)}",
        f"  variants:            {sum(r.variants for r in report.results)}",
        f"  compared pairs:      {report.pairs}",
        f"  flipping pairs:      {report.flips}",
    ]

    if report.repeats:
        instability = report.repeat_instability
        lines.append(
            "  repeat instability: "
            f"{'unavailable' if instability is None else format(instability, '.1%')}"
            f" over {report.repeats} identical runs"
        )

    unlabelled = report.unlabelled_families
    if unlabelled:
        lines.append("")
        lines.append(
            f"  NOT MEASURED ({len(unlabelled)}): the readback could not see "
            "the label, so these families agreed with nothing:"
        )
        for result in unlabelled:
            lines.append(
                f"    {result.intent_id}: {result.labelled}/{result.variants} "
                "variants came back labelled"
            )

    inconsistent = report.inconsistent_families
    if inconsistent:
        lines.append("")
        lines.append(f"  families that disagreed ({len(inconsistent)}):")
        for result in inconsistent:
            lines.append(
                f"    {result.intent_id}: majority {result.majority!r}, "
                f"{result.flips}/{result.pairs} pairs differ"
            )
            for transform, label in result.dissenters:
                lines.append(f"      {transform}: got {label!r}")

    ranked = [
        (name, bucket)
        for name, bucket in report.by_transform().items()
        if bucket["dissenters"]
    ]
    if ranked:
        lines.append("")
        lines.append("  worst transforms:")
        for name, bucket in ranked[:5]:
            lines.append(
                f"    {name}: {bucket['dissenters']}/{bucket['variants']} "
                "variants disagreed with their family"
            )

    by_language = [
        (name, bucket)
        for name, bucket in report.by_language().items()
        if bucket["dissenters"]
    ]
    if by_language:
        lines.append("")
        lines.append("  by language:")
        for name, bucket in by_language:
            lines.append(
                f"    {name}: {bucket['dissenters']}/{bucket['variants']} variants "
                "disagreed with their family"
            )

    lines.append("")
    lines.append(
        "  This measures STABILITY, not correctness. A classifier that is "
        "consistently wrong scores"
    )
    lines.append(
        "  0% here. Use `testinghq verify` to check what the label should have been."
    )
    return "\n".join(lines)
