"""steady: metamorphic stability testing for an AI triage classifier.

A family is one reviewed intent written several ways: a baseline per language
from `fixtures/steady_intents.json`, and each meaning-preserving transform
applied to each baseline. The classifier's label should not move within a
family. The headline is majority dissent: the share of labelled variants whose
label differs from their family's majority, broken down by transform and by
language. Pairwise flip rate is a secondary number, with same-language and
cross-language pairs reported apart.

Stability is not correctness: a classifier that is consistently wrong scores 0%.
A dissent rate with nothing labelled is None, never 0.0.
"""
from __future__ import annotations

import dataclasses
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..blast.payload import Envelope, GroundTruth, InboundEmail
from ..core.targets import resolve_target
from .common import (
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    READBACK_FAILED,
    close_adapter,
    prepare_readback,
    read_back_all,
    readback_verdict,
    require_synthetic,
    send_all,
)
from .messages import DEFAULT_TAG_PREFIX, make_tag, probe_for, stamp
from .toolconfig import ToolConfigError, read_tool_table_if_present

SCHEMA_VERSION = 1
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "steady_intents.json"

#: Used when no field is named. A run that relies on it is refused before send
#: when every family would read the same value from it (see `constant_label_risk`).
LABEL_FIELD = "route"
LABEL_FIELDS = ("category", "route", "priority")

TRANSFORM_BASELINE = "baseline"
DEFAULT_SEED = 0
DEFAULT_COUNT = 6
MAILBOX = "intake@example.com"


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """One reviewed intent: its variants and neutral subjects per language."""

    id: str
    category_hint: str
    note: str
    variants: Dict[str, str]
    subjects: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def body_for(self, language: str) -> Optional[str]:
        return self.variants.get(language)

    def subjects_for(self, language: str) -> Tuple[str, ...]:
        return self.subjects.get(language) or self.subjects.get("en") or ()


@dataclass(frozen=True)
class Fixture:
    intents: Tuple[Intent, ...]
    closings: Dict[str, Tuple[str, ...]] = field(default_factory=dict)


def load_fixture(path: Optional[Path] = None) -> Fixture:
    """The reviewed fixture, or a custom one with the same shape:

        {"schema_version": 2,
         "closings": {"<lang>": ["sign-off", ...]},
         "intents": [{"id": "...", "category_hint": "...", "note": "...",
                      "subjects": {"<lang>": ["subject", ...]},
                      "variants": {"<lang>": "body"}}]}

    Every intent needs a body in at least one language and a subject for it.
    """
    source = Path(path) if path else FIXTURE_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    intents = []
    for entry in raw["intents"]:
        subjects = {
            lang: tuple(values) for lang, values in (entry.get("subjects") or {}).items()
        }
        intent = Intent(
            id=entry["id"],
            category_hint=entry.get("category_hint", ""),
            note=entry.get("note", ""),
            variants=dict(entry["variants"]),
            subjects=subjects,
        )
        missing = [lang for lang in intent.variants if not intent.subjects_for(lang)]
        if missing:
            raise ValueError(
                f"{source}: intent {intent.id!r} has no subject for {missing}; "
                "every intent needs a neutral subject list"
            )
        intents.append(intent)
    if not intents:
        raise ValueError(f"{source} has no intents")
    closings = {lang: tuple(values) for lang, values in (raw.get("closings") or {}).items()}
    return Fixture(intents=tuple(intents), closings=closings)


def load_intents(path: Optional[Path] = None) -> List[Intent]:
    return list(load_fixture(path).intents)


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

_NAMES = (
    "Ayesha Khan", "Bilal Ahmed", "Hira Siddiqui", "Omar Farooq",
    "Sara Malik", "Usman Tariq", "Zainab Raza", "Daniel Hughes",
    "Maryam Qureshi", "Imran Sheikh",
)


@dataclass(frozen=True)
class Sender:
    name: str
    address: str
    alternate: str
    phone: str

    @property
    def display(self) -> str:
        return f"{self.name} <{self.address}>"

    @property
    def alternate_display(self) -> str:
        return f"{self.name} <{self.alternate}>"


def sender_for(index: int) -> Sender:
    name = _NAMES[index % len(_NAMES)]
    local = name.lower().replace(" ", ".")
    return Sender(
        name=name,
        address=f"{local}@example.com",
        alternate=f"{local}@example.net",
        phone=f"+1 555 01{index % 100:02d}",
    )


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransformContext:
    """What a transform may need beyond the payload."""

    language: str = "en"
    closings: Tuple[str, ...] = ("Kind regards,",)
    sender: Sender = field(default_factory=lambda: sender_for(0))


#: `(email, rng, context) -> Optional[InboundEmail]`. None means the transform
#: does not apply, so a variant never duplicates its baseline.
Transform = Callable[[InboundEmail, random.Random, TransformContext], Optional[InboundEmail]]

_WHITESPACE_RUN = re.compile(r"[ \t]{2,}")

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


def _with_text(email: InboundEmail, text: str, **changes) -> InboundEmail:
    return dataclasses.replace(
        email, text=text, html=f"<html><body>{text}</body></html>", **changes
    )


def _date(rng: random.Random) -> str:
    when = datetime(2025, 9, 1, 9, 0, tzinfo=timezone.utc) + timedelta(
        days=rng.randrange(0, 30), minutes=rng.randrange(0, 600)
    )
    return when.strftime("%a, %d %b %Y at %H:%M")


def transform_signature(email: InboundEmail, rng: random.Random, ctx: TransformContext) -> InboundEmail:
    """A signature with the sender's name and a phone number. No role or title,
    which would be a claim about who is asking."""
    del rng
    return _with_text(email, email.text.rstrip("\n") + f"\n\n--\n{ctx.sender.name}\n{ctx.sender.phone}\n")


def transform_forward(email: InboundEmail, rng: random.Random, ctx: TransformContext) -> InboundEmail:
    """The same person forwarding their own message from another address: the
    quoted From is the original author, the outer From is the same name."""
    block = (
        "---------- Forwarded message ---------\n"
        f"From: {email.from_addr}\n"
        f"Date: {_date(rng)}\n"
        f"Subject: {email.subject}\n"
        f"To: {email.to}\n\n"
    )
    outer = ctx.sender.alternate_display
    subject = f"Fwd: {email.subject}"
    return _with_text(
        email, block + email.text,
        from_addr=outer,
        subject=subject,
        envelope=Envelope(to=email.envelope.to, from_addr=ctx.sender.alternate),
        ground_truth=dataclasses.replace(
            email.ground_truth, from_addr=outer, subject=subject
        ),
    )


def transform_quoted_history(email: InboundEmail, rng: random.Random, ctx: TransformContext) -> InboundEmail:
    """The sender's own earlier copy of the message, quoted below it."""
    del ctx
    header = f"On {_date(rng)}, {email.from_addr} wrote:"
    quoted = "\n".join(f"> {line}" for line in (email.text.splitlines() or [""]))
    return _with_text(email, f"{email.text.rstrip()}\n\n{header}\n{quoted}\n")


def transform_reflow_whitespace(
    email: InboundEmail, rng: random.Random, ctx: TransformContext
) -> Optional[InboundEmail]:
    """Re-wrap the body at another width. No word changes."""
    del ctx
    joined = " ".join(line.strip() for line in email.text.splitlines() if line.strip())
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
    return _with_text(email, text)


def transform_reword_greeting(
    email: InboundEmail, rng: random.Random, ctx: TransformContext
) -> Optional[InboundEmail]:
    """Swap an equivalent English greeting or sign-off, or append a closing in
    the body's own language. Declines when the language has no closing."""
    swapped = _swap_first(email.text, _GREETING_SWAPS) if ctx.language == "en" else None
    if swapped is None and ctx.language == "en":
        swapped = _swap_last(email.text, _SIGNOFF_SWAPS)
    if swapped is None:
        if not ctx.closings:
            return None
        closing = rng.choice(ctx.closings)
        swapped = email.text.rstrip("\n") + f"\n\n{closing}\n"
    if swapped == email.text:
        return None
    return _with_text(email, swapped)


def _swap_first(text: str, swaps: Sequence[Tuple[str, str]]) -> Optional[str]:
    for original, replacement in swaps:
        if text.startswith(original):
            return replacement + text[len(original):]
    return None


def _swap_last(text: str, swaps: Sequence[Tuple[str, str]]) -> Optional[str]:
    for original, replacement in swaps:
        marker = f"\n{original}"
        index = text.rfind(marker)
        if index != -1:
            return text[:index] + f"\n{replacement}" + text[index + len(marker):]
    return None


TRANSFORMS: Dict[str, Transform] = {
    "add-signature": transform_signature,
    "wrap-forward": transform_forward,
    "add-quoted-history": transform_quoted_history,
    "reflow-whitespace": transform_reflow_whitespace,
    "reword-greeting": transform_reword_greeting,
}
#: Language is not a transform; the per-language baselines vary it.
TRANSFORM_NAMES: Tuple[str, ...] = tuple(sorted(TRANSFORMS))


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

READ_LABELLED = "labelled"
READ_NULL = "null"
READ_NOT_VISIBLE = "not-visible"
READ_NO_RECORD = "no-record"
READ_NOT_SENT = "not-sent"


@dataclass(frozen=True)
class Variant:
    """One member of a family, and what the classifier said about it."""

    intent_id: str
    language: str
    transform: str
    email: InboundEmail
    label: Optional[str] = None
    read_state: str = READ_NOT_SENT
    read_detail: str = ""

    def with_label(self, label: Optional[str], state: Optional[str] = None,
                   detail: str = "") -> "Variant":
        if state is None:
            state = READ_LABELLED if label is not None else READ_NO_RECORD
        return dataclasses.replace(self, label=label, read_state=state, read_detail=detail)

    def to_json(self) -> Dict[str, Any]:
        from ..core import report as engine_report

        return {
            "intent_id": self.intent_id,
            "language": self.language,
            "transform": self.transform,
            "label": self.label,
            "label_state": self.read_state,
            "label_detail": self.read_detail,
            "subject": self.email.subject,
            "payload_sha256": engine_report.payload_sha256(self.email),
        }


@dataclass(frozen=True)
class Family:
    """Every variant of one intent."""

    intent_id: str
    category_hint: str
    variants: Tuple[Variant, ...]

    def labelled(self, labels: Sequence[Optional[str]]) -> "Family":
        if len(labels) != len(self.variants):
            raise ValueError(
                f"family {self.intent_id!r} has {len(self.variants)} variants "
                f"but {len(labels)} labels"
            )
        return dataclasses.replace(
            self, variants=tuple(v.with_label(label) for v, label in zip(self.variants, labels))
        )


def _email_for(intent: Intent, language: str, sender: Sender, subject_index: int) -> InboundEmail:
    """The baseline payload: the fixture's body, a neutral subject in the same
    language, and the family's sender."""
    body = intent.body_for(language) or ""
    subjects = intent.subjects_for(language)
    subject = subjects[subject_index % len(subjects)]
    return InboundEmail(
        to=MAILBOX,
        from_addr=sender.display,
        subject=subject,
        text=body + "\n",
        html=f"<html><body>{body}</body></html>",
        envelope=Envelope(to=(MAILBOX,), from_addr=sender.address),
        ground_truth=GroundTruth(from_addr=sender.display, subject=subject, body_core=body),
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
    closings: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[Family]:
    """One family per intent: each language's baseline, plus each transform
    applied to each baseline. Deterministic in its arguments."""
    if intents is None or closings is None:
        fixture = load_fixture()
        chosen = list(intents) if intents is not None else list(fixture.intents)
        closings = fixture.closings if closings is None else closings
    else:
        chosen = list(intents)
    wanted = list(transforms) if transforms is not None else list(TRANSFORM_NAMES)
    unknown = [name for name in wanted if name not in TRANSFORM_NAMES]
    if unknown:
        raise ValueError(f"unknown transform(s) {unknown}; known: {list(TRANSFORM_NAMES)}")
    languages = sorted({lang for intent in chosen for lang in intent.variants})

    families: List[Family] = []
    for family_index, intent in enumerate(chosen):
        sender = sender_for(family_index)
        subject_index = random.Random(f"testinghq:steady:{seed}:{intent.id}:subject").randrange(1000)
        variants: List[Variant] = []
        for language in languages:
            if not intent.body_for(language):
                continue
            baseline = _email_for(intent, language, sender, subject_index)
            if include_baseline:
                variants.append(Variant(intent.id, language, TRANSFORM_BASELINE, baseline))
            context = TransformContext(
                language=language,
                closings=tuple((closings or {}).get(language, ())),
                sender=sender,
            )
            for name in wanted:
                rng = random.Random(f"testinghq:steady:{seed}:{intent.id}:{language}:{name}")
                produced = TRANSFORMS[name](baseline, rng, context)
                if produced is not None:
                    variants.append(Variant(intent.id, language, name, produced))
        if variants:
            families.append(Family(intent.id, intent.category_hint, tuple(variants)))
    return families


def load_families(
    seed: int, count: int, transforms: Optional[Sequence[str]] = None,
    intents_path: Optional[str] = None,
) -> List[Family]:
    """The families a run with these settings sends: the first `count` intents
    of the fixture (or of `intents_path`), each with the chosen transforms."""
    fixture = load_fixture(Path(intents_path) if intents_path else None)
    return build_families(
        seed, intents=fixture.intents[:count], transforms=transforms,
        closings=fixture.closings,
    )


def build_items(
    families: Sequence[Family],
    *,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    seed: int = DEFAULT_SEED,
) -> List[Tuple[Any, str, str, Any]]:
    """(email, tag, record_id, probe) per variant, tagged by (prefix, seed, n)."""
    items = []
    counter = 0
    for index, family in enumerate(families):
        for position, variant in enumerate(family.variants):
            tag = make_tag(tag_prefix, seed, counter)
            counter += 1
            record_id = f"steady-{index:03d}-{position:03d}"
            email = stamp(variant.email, tag)
            items.append((email, tag, record_id, probe_for(email, tag, record_id, "")))
    return items


def repeat_items(
    families: Sequence[Family], repeats: int, tag_prefix: str, seed: int
) -> List[Tuple[Any, str, str, Any]]:
    """The first payload of each family, sent `repeats` times with fresh tags."""
    out = []
    for index, family in enumerate(families):
        if not family.variants:
            continue
        source = family.variants[0]
        for attempt in range(repeats):
            tag = make_tag(f"{tag_prefix}-repeat", seed, index * repeats + attempt)
            record_id = f"steady-repeat-{index:03d}-{attempt:03d}"
            email = stamp(source.email, tag)
            out.append((email, tag, record_id, probe_for(email, tag, record_id, "")))
    return out


def constant_label_risk(families: Sequence[Family], label_field: str) -> Optional[str]:
    """The value every family would read from `label_field` when the pipeline
    only echoes the message, or None when the field is not derived from it.
    `route` is the one such field: it reads the recipient, and every family is
    sent to one mailbox."""
    if label_field != "route":
        return None
    recipients = {v.email.envelope.to[0] for f in families for v in f.variants if v.email.envelope.to}
    if len(families) >= 2 and len(recipients) == 1:
        return next(iter(recipients))
    return None


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------


def total_pairs(count: int) -> int:
    return count * (count - 1) // 2


def _counts(labels: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return counts


def flipped_pairs(labels: Sequence[Optional[str]]) -> int:
    """Pairs whose labels differ. A missing label is not a flip."""
    known = [label for label in labels if label is not None]
    if len(known) < 2:
        return 0
    return total_pairs(len(known)) - sum(total_pairs(c) for c in _counts(known).values())


def majority_label(labels: Sequence[Optional[str]]) -> Optional[str]:
    """The most common label, ties broken by name."""
    known = [label for label in labels if label is not None]
    if not known:
        return None
    return min(_counts(known).items(), key=lambda item: (-item[1], item[0]))[0]


def _pair_split(family: Family) -> Tuple[int, int, int, int]:
    """(same-language pairs, their flips, cross-language pairs, their flips)."""
    labelled = [v for v in family.variants if v.label is not None]
    same = same_flips = cross = cross_flips = 0
    for i, left in enumerate(labelled):
        for right in labelled[i + 1:]:
            flip = left.label != right.label
            if left.language == right.language:
                same += 1
                same_flips += flip
            else:
                cross += 1
                cross_flips += flip
    return same, same_flips, cross, cross_flips


@dataclass(frozen=True)
class FamilyResult:
    intent_id: str
    category_hint: str
    variants: int
    labelled: int
    majority: Optional[str]
    dissenters: Tuple[Tuple[str, str, str], ...] = ()
    pairs: int = 0
    flips: int = 0

    @property
    def consistent(self) -> bool:
        return not self.dissenters

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
            "dissenters": [
                {"transform": t, "language": lang, "label": label}
                for t, lang, label in self.dissenters
            ],
            "dissent_rate": (len(self.dissenters) / self.labelled) if self.labelled else None,
            "pairs": self.pairs,
            "flips": self.flips,
            "consistent": self.consistent,
        }


def judge_family(family: Family) -> FamilyResult:
    """Compare a family's labels with each other, not with an answer."""
    labels = [variant.label for variant in family.variants]
    majority = majority_label(labels)
    labelled = sum(1 for label in labels if label is not None)
    return FamilyResult(
        intent_id=family.intent_id,
        category_hint=family.category_hint,
        variants=len(family.variants),
        labelled=labelled,
        majority=majority,
        dissenters=tuple(
            (v.transform, v.language, v.label)
            for v in family.variants
            if v.label is not None and v.label != majority
        ),
        pairs=total_pairs(labelled),
        flips=flipped_pairs(labels),
    )


def judge(families: Sequence[Family]) -> Tuple[FamilyResult, ...]:
    return tuple(judge_family(f) for f in families)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class StabilityReport:
    """The numbers, and the labelled families they are computed from."""

    families: Tuple[Family, ...]
    repeats: int = 0
    #: intent id -> labels of that family's repeated payload.
    repeat_labels: Mapping[str, Tuple[Optional[str], ...]] = field(default_factory=dict)
    label_field: str = LABEL_FIELD

    @property
    def results(self) -> Tuple[FamilyResult, ...]:
        return judge(self.families)

    # -- headline: majority dissent --------------------------------------

    @property
    def labelled_variants(self) -> int:
        return sum(r.labelled for r in self.results)

    @property
    def dissenters(self) -> int:
        return sum(len(r.dissenters) for r in self.results)

    @property
    def dissent_rate(self) -> Optional[float]:
        return _rate(self.dissenters, self.labelled_variants)

    # -- secondary: pairwise ---------------------------------------------

    @property
    def pairs(self) -> int:
        return sum(r.pairs for r in self.results)

    @property
    def flips(self) -> int:
        return sum(r.flips for r in self.results)

    @property
    def flip_rate(self) -> Optional[float]:
        return _rate(self.flips, self.pairs)

    def pair_split(self) -> Dict[str, Dict[str, Any]]:
        same = same_flips = cross = cross_flips = 0
        for family in self.families:
            a, b, c, d = _pair_split(family)
            same, same_flips, cross, cross_flips = same + a, same_flips + b, cross + c, cross_flips + d
        return {
            "same_language": {"pairs": same, "flips": same_flips, "flip_rate": _rate(same_flips, same)},
            "cross_language": {"pairs": cross, "flips": cross_flips, "flip_rate": _rate(cross_flips, cross)},
        }

    # -- families ---------------------------------------------------------

    @property
    def inconsistent_families(self) -> Tuple[FamilyResult, ...]:
        return tuple(r for r in self.results if not r.consistent)

    @property
    def unlabelled_families(self) -> Tuple[FamilyResult, ...]:
        return tuple(r for r in self.results if not r.fully_labelled)

    def read_states(self) -> Dict[str, int]:
        counts = {READ_LABELLED: 0, READ_NULL: 0, READ_NOT_VISIBLE: 0, READ_NO_RECORD: 0}
        for family in self.families:
            for variant in family.variants:
                counts[variant.read_state] = counts.get(variant.read_state, 0) + 1
        return counts

    def constant_label(self) -> Optional[str]:
        """The one label every measured family read, when there are two or more."""
        measured = [r for r in self.results if r.labelled]
        labels = {v.label for f in self.families for v in f.variants if v.label is not None}
        if len(measured) >= 2 and len(labels) == 1:
            return next(iter(labels))
        return None

    def sample_size(self) -> str:
        sizes = sorted({len(f.variants) for f in self.families})
        each = (f"{sizes[0]}" if len(sizes) == 1 else f"{sizes[0]} to {sizes[-1]}") if sizes else "0"
        total = self.labelled_variants
        single = f"{1 / total:.1%}" if total else "undefined"
        return (
            f"{len(self.families)} families, {each} variants each; a single "
            f"dissenting variant is {single} of the headline"
        )

    # -- repeats ----------------------------------------------------------

    def repeat_by_family(self) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for intent_id, labels in self.repeat_labels.items():
            known = [label for label in labels if label is not None]
            if self.repeats < 2 or len(known) < self.repeats:
                out[intent_id] = None
            else:
                out[intent_id] = 1.0 - max(_counts(known).values()) / self.repeats
        return out

    @property
    def repeat_instability(self) -> Optional[float]:
        """Mean over families of one minus the most common label's share.
        None unless every repeat of every family came back labelled."""
        values = list(self.repeat_by_family().values())
        if self.repeats < 2 or not values or any(v is None for v in values):
            return None
        return sum(values) / len(values)

    # -- breakdowns -------------------------------------------------------

    def _breakdown(self, key: Callable[[Variant], str]) -> Dict[str, Dict[str, Any]]:
        buckets: Dict[str, Dict[str, Any]] = {}
        for family, result in zip(self.families, self.results):
            for variant in family.variants:
                bucket = buckets.setdefault(key(variant), {"variants": 0, "dissenters": 0})
                if variant.label is None:
                    continue
                bucket["variants"] += 1
                if variant.label != result.majority:
                    bucket["dissenters"] += 1
        for bucket in buckets.values():
            bucket["dissent_rate"] = _rate(bucket["dissenters"], bucket["variants"])
        return dict(sorted(
            buckets.items(), key=lambda item: (-(item[1]["dissent_rate"] or 0.0), item[0])
        ))

    def by_transform(self) -> Dict[str, Dict[str, Any]]:
        return self._breakdown(lambda v: v.transform)

    def by_language(self) -> Dict[str, Dict[str, Any]]:
        return self._breakdown(lambda v: v.language)

    def to_json(self) -> Dict[str, Any]:
        return {
            "label_field": self.label_field,
            "dissent_rate": self.dissent_rate,
            "dissenters": self.dissenters,
            "labelled_variants": self.labelled_variants,
            "sample_size": self.sample_size(),
            "families": [result.to_json() for result in self.results],
            "pairs": self.pairs,
            "flips": self.flips,
            "flip_rate": self.flip_rate,
            "pairs_by_language": self.pair_split(),
            "inconsistent_families": [r.intent_id for r in self.inconsistent_families],
            "unlabelled_families": [r.intent_id for r in self.unlabelled_families],
            "label_reads": self.read_states(),
            "constant_label": self.constant_label(),
            "repeats": self.repeats,
            "repeat_instability": self.repeat_instability,
            "repeat_by_family": self.repeat_by_family(),
            "by_transform": self.by_transform(),
            "by_language": self.by_language(),
        }


def verdict_for(report: StabilityReport) -> str:
    rate = report.dissent_rate
    if rate is None:
        verdict = "NOTHING MEASURED"
    elif rate == 0.0:
        verdict = "STABLE"
    elif rate <= 0.05:
        verdict = "MOSTLY STABLE"
    else:
        verdict = "UNSTABLE"
    constant = report.constant_label()
    if constant is not None:
        verdict = f"{verdict} (WARNING: every family read the one label {constant!r})"
    return verdict


def _pct(value: Optional[float]) -> str:
    return "unavailable" if value is None else format(value, ".1%")


def format_stability(report: StabilityReport, verdict: Optional[str] = None) -> str:
    """The report, with the caveat that stability is not correctness.
    `verdict` is the run's final verdict when the readback replaced the
    measured one (UNSETTLED, READBACK FAILED); None means `verdict_for`."""
    split = report.pair_split()
    states = report.read_states()
    lines = [
        f"steady: {verdict or verdict_for(report)}  (majority dissent {_pct(report.dissent_rate)}, "
        f"label field {report.label_field!r})",
        "",
        f"  sample:              {report.sample_size()}",
        f"  dissenting variants: {report.dissenters} of {report.labelled_variants} labelled",
        f"  pairwise flip rate:  {_pct(report.flip_rate)} ({report.flips}/{report.pairs} pairs)",
        f"    same language:     {_pct(split['same_language']['flip_rate'])} "
        f"({split['same_language']['flips']}/{split['same_language']['pairs']})",
        f"    cross language:    {_pct(split['cross_language']['flip_rate'])} "
        f"({split['cross_language']['flips']}/{split['cross_language']['pairs']})",
        f"  label reads:         {states[READ_LABELLED]} labelled, {states[READ_NULL]} null, "
        f"{states[READ_NOT_VISIBLE]} not visible, {states[READ_NO_RECORD]} no record",
    ]
    if report.repeats:
        lines.append(
            f"  repeat instability:  {_pct(report.repeat_instability)} over "
            f"{report.repeats} identical sends of one payload per family"
        )
    if report.constant_label() is not None:
        lines.append("")
        lines.append(
            f"  every family read {report.constant_label()!r}. Families are different "
            "intents, so a working classifier gives them different labels; this field"
        )
        lines.append("  probably does not carry the classification. Check --label-field.")

    unlabelled = report.unlabelled_families
    if unlabelled:
        lines.append("")
        lines.append(f"  NOT MEASURED ({len(unlabelled)}): the readback could not see the label:")
        for result in unlabelled:
            lines.append(f"    {result.intent_id}: {result.labelled}/{result.variants} labelled")

    inconsistent = report.inconsistent_families
    if inconsistent:
        lines.append("")
        lines.append(f"  families that disagreed ({len(inconsistent)}):")
        for result in inconsistent:
            lines.append(
                f"    {result.intent_id}: majority {result.majority!r}, "
                f"{len(result.dissenters)}/{result.labelled} variants dissent"
            )
            for transform, language, label in result.dissenters:
                lines.append(f"      {transform} ({language}): got {label!r}")

    for title, table in (("worst transforms", report.by_transform()),
                         ("by language", report.by_language())):
        ranked = [(n, b) for n, b in table.items() if b["dissenters"]]
        if ranked:
            lines.append("")
            lines.append(f"  {title}:")
            for name, bucket in ranked[:6]:
                lines.append(
                    f"    {name}: {bucket['dissenters']}/{bucket['variants']} variants "
                    "disagreed with their family"
                )

    lines.append("")
    lines.append("  This measures STABILITY, not correctness. A classifier that is consistently wrong scores")
    lines.append("  0% here. Use `testinghq verify` to check what the label should have been.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The send path
# ---------------------------------------------------------------------------

DEFAULT_MAX_DISSENT_RATE = 0.10


@dataclass(frozen=True)
class LabelRead:
    """One payload's label and how it was read: labelled, null, not visible
    (the adapter cannot see the field) or no record."""

    label: Optional[str]
    state: str
    detail: str = ""


def read_label(readbacks: Sequence[Any], label_field: str) -> LabelRead:
    """The label for one payload, from the first record. Not visible, null and
    no record are different states and stay different."""
    for record in readbacks:
        if not record.has(label_field):
            return LabelRead(None, READ_NOT_VISIBLE,
                             f"the adapter could not see {label_field!r}")
        value = getattr(record, label_field, None)
        if value is None:
            return LabelRead(None, READ_NULL, f"{label_field!r} was present but null")
        return LabelRead(str(value), READ_LABELLED)
    return LabelRead(None, READ_NO_RECORD, "no record came back for this payload")


CONFIG_SECTION = "steady"


class SteadyConfigError(ToolConfigError):
    """A malformed `[steady]` table. A caller error, never a finding."""


def load_steady_config(path: Optional[str]) -> Dict[str, Any]:
    """The validated `[steady]` table, or {} when the file or table is absent.
    A relative `intents_path` resolves against the config file's directory."""
    try:
        section = dict(read_tool_table_if_present(path, CONFIG_SECTION))
    except ToolConfigError as exc:
        raise SteadyConfigError(str(exc)) from exc
    label = section.get("label_field")
    if label is not None and label not in LABEL_FIELDS:
        raise SteadyConfigError(
            f"[{CONFIG_SECTION}].label_field must be one of {list(LABEL_FIELDS)}, "
            f"got {label!r}"
        )
    intents = section.get("intents_path")
    if intents is not None:
        if not isinstance(intents, str) or not intents:
            raise SteadyConfigError(
                f"[{CONFIG_SECTION}].intents_path must be a path string, got {intents!r}"
            )
        if path and not Path(intents).is_absolute():
            section["intents_path"] = str(Path(path).resolve().parent / intents)
    return section


def run_config(
    seed: int, count: int, tag_prefix: str, target_name: Optional[str], readback: Any,
    label_field: str, transforms: Sequence[str], repeats: int,
    max_dissent_rate: float, max_flip_rate: Optional[float], poll: Dict[str, float],
    intents_path: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "tool": "steady",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "label_field": label_field,
        "transforms": list(transforms),
        "repeats": repeats,
        "max_dissent_rate": max_dissent_rate,
        "max_flip_rate": max_flip_rate,
        "intents_path": intents_path,
        "readback_poll": dict(poll),
        "dry_run": False,
    }


def build_artifact(seed: int, config: Dict[str, Any], families: Sequence[Family],
                   report: StabilityReport, *, aborted: bool = False) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "steady",
        "aborted": aborted,
        "seed": seed,
        "config": config,
        "stability": report.to_json(),
        "families": [
            {
                "intent_id": family.intent_id,
                "category_hint": family.category_hint,
                "variants": [variant.to_json() for variant in family.variants],
            }
            for family in families
        ],
    }


def gate(report: StabilityReport, max_dissent_rate: float,
         max_flip_rate: Optional[float] = None) -> Tuple[bool, str]:
    """Whether the run exits 3, and why. Nothing measured, partly measured, and
    over a threshold all fail."""
    if report.dissent_rate is None:
        return True, (
            f"nothing was measured: every variant came back without a "
            f"{report.label_field!r} label"
        )
    unlabelled = report.unlabelled_families
    if unlabelled:
        return True, (
            f"{len(unlabelled)} of {len(report.results)} families could not be measured: "
            f"the readback could not see {report.label_field!r} for every variant"
        )
    if report.dissent_rate > max_dissent_rate:
        return True, (
            f"majority dissent {report.dissent_rate:.1%} is over the {max_dissent_rate:.1%} gate"
        )
    if max_flip_rate is not None and report.flip_rate is not None and report.flip_rate > max_flip_rate:
        return True, f"pairwise flip rate {report.flip_rate:.1%} is over the {max_flip_rate:.1%} gate"
    return False, f"majority dissent {report.dissent_rate:.1%} is within the {max_dissent_rate:.1%} gate"


def format_run(
    report: StabilityReport, failed: bool, reason: str, max_dissent_rate: float,
    verdict: Optional[str] = None,
) -> str:
    return "\n".join([
        format_stability(report, verdict), "",
        f"  gate ({max_dissent_rate:.1%}): {'FAILED' if failed else 'passed'}",
        f"  {reason}",
    ])


def format_dry_run(
    families: Sequence[Family], readback: Any, label_field: Optional[str],
    transforms: Optional[Sequence[str]] = None,
) -> str:
    """What a real run would send. `readback` None means none is configured,
    which a real run refuses."""
    chosen = list(transforms) if transforms else list(TRANSFORM_NAMES)
    field_name = label_field or LABEL_FIELD
    lines = [
        f"dry-run preview: {len(families)} intent families, "
        f"{sum(len(f.variants) for f in families)} payload(s)",
        "  readback adapter: "
        + ("none configured (a real run refuses)" if readback is None
           else f"kind {readback.kind!r}"),
        f"  label field:      {field_name!r}" + ("" if label_field else " (default)"),
        f"  transforms:       {', '.join(sorted(chosen))}",
    ]
    risk = None if label_field else constant_label_risk(families, LABEL_FIELD)
    if risk is not None:
        lines.append(
            f"  a real run REFUSES: no --label-field was given, and 'route' would read "
            f"{risk!r} for every family"
        )
    kind = getattr(readback, "kind", None)
    if kind == "http":
        lines.append(f"  readback url:     {readback.url}")
    elif kind == "mailbox":
        lines.append(f"  readback path:    {readback.path}")
    lines.append("  per family:")
    for family in families[:6]:
        lines.append(f"    {family.intent_id:26} {len(family.variants)} variants")
    if len(families) > 6:
        lines.append(f"    ... and {len(families) - 6} more")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: Any,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    label_field: Optional[str] = None,
    transforms: Optional[Sequence[str]] = None,
    repeats: int = 1,
    max_dissent_rate: float = DEFAULT_MAX_DISSENT_RATE,
    max_flip_rate: Optional[float] = None,
    intents_path: Optional[str] = None,
    rate: float = 5.0,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    allow_public_target: bool = False,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Send every variant, read every label back, report, gate. Every refusal
    lands before the first send. `label_field=None` means not given.
    `readback` is a `ReadbackConfig`; its adapter is built and preflighted
    before the first send."""
    field_name = label_field or LABEL_FIELD
    if field_name not in LABEL_FIELDS:
        printer(f"refused: --label-field must be one of {list(LABEL_FIELDS)}, got {field_name!r}.")
        return EXIT_REFUSED
    for name, value in (("--max-dissent-rate", max_dissent_rate), ("--max-flip-rate", max_flip_rate)):
        if value is not None and value < 0.0:
            printer(f"refused: {name} must be >= 0, got {value}")
            return EXIT_REFUSED
    if repeats < 1:
        printer(f"refused: --repeats must be >= 1, got {repeats}")
        return EXIT_REFUSED
    if count < 1:
        printer(f"refused: --count must be >= 1, got {count}")
        return EXIT_REFUSED

    try:
        families = load_families(seed, count, transforms, intents_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        printer(f"refused: the intent fixture could not be read: {exc}")
        return EXIT_REFUSED

    if label_field is None:
        risk = constant_label_risk(families, field_name)
        if risk is not None:
            printer(
                f"refused: no --label-field was given, and the default {field_name!r} "
                f"would read {risk!r} for every family, so nothing could move. Name "
                "the field your classifier writes, for example --label-field category, "
                "or set [steady].label_field."
            )
            return EXIT_REFUSED

    items = build_items(families, tag_prefix=tag_prefix, seed=seed)
    repeated = repeat_items(families, repeats, tag_prefix, seed) if repeats > 1 else []
    try:
        require_synthetic([email for email, _t, _r, _p in items + repeated])
        target = resolve_target(target_name, config_path, allow_public_target=allow_public_target)
    except Exception as exc:  # noqa: BLE001 - every pre-send failure is a refusal
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    chosen = list(transforms) if transforms else list(TRANSFORM_NAMES)
    config = run_config(
        seed, count, tag_prefix, target_name, readback, field_name, chosen, repeats,
        max_dissent_rate, max_flip_rate,
        {"quiet_window": quiet_window, "max_wait": max_wait, "poll_interval": poll_interval},
        intents_path,
    )
    config.update(target.artifact_config())

    adapter = None
    sent: List[Any] = []
    try:
        try:
            adapter = prepare_readback(readback, client=readback_client, config_path=config_path)
        except Exception as exc:  # noqa: BLE001 - nothing has been sent yet
            printer(f"refused: {exc}")
            return EXIT_REFUSED
        sent = send_all(items + repeated, target.url, rate, client=client, sleep=sleep,
                        clock=clock, wire_format=target.wire_format)
        outcome = read_back_all(
            adapter, [probe for _e, _t, _r, probe in items + repeated],
            sleep=sleep, clock=clock, quiet_window=quiet_window,
            max_wait=max_wait, poll_interval=poll_interval,
        )
    except KeyboardInterrupt:
        artifact = build_artifact(
            seed, config, families, StabilityReport(tuple(families), label_field=field_name),
            aborted=True,
        )
        artifact["stability"].update({
            "verdict": "ABORTED", "sent": len(sent),
            "gate": {"failed": True, "reason": "the run was interrupted",
                     "max_dissent_rate": max_dissent_rate, "max_flip_rate": max_flip_rate},
        })
        printer("steady: ABORTED" + (", partial artifact written" if out else ""))
        if out:
            Path(out).write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        return EXIT_MISMATCH
    except Exception as exc:  # noqa: BLE001 - a failed readback after send
        printer(f"steady: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    labelled = read_labels(families, items, outcome.readbacks, field_name)
    report = StabilityReport(
        families=labelled,
        repeats=repeats if repeated else 0,
        repeat_labels=_repeat_labels(families, repeated, repeats, outcome.readbacks, field_name),
        label_field=field_name,
    )
    failed, reason = gate(report, max_dissent_rate, max_flip_rate)
    unsettled = outcome.unsettled
    if outcome.failed:
        failed, reason = True, (
            f"{READBACK_FAILED}: every lookup was 404: check the readback url; "
            "nothing was measured"
        )
    elif unsettled:
        failed, reason = True, f"{outcome.state_phrase()}; " + reason
    artifact = build_artifact(seed, config, labelled, report)
    summary = artifact["stability"]
    summary["verdict"], summary["verdict_source"] = readback_verdict(verdict_for(report), outcome)
    summary["readback_headline"] = outcome.headline()
    summary["gate"] = {"failed": failed, "reason": reason,
                       "max_dissent_rate": max_dissent_rate, "max_flip_rate": max_flip_rate}
    summary["sent"] = len(sent)
    summary["unsettled"] = unsettled
    summary["readback"] = outcome.to_json()

    printer(format_run(report, failed, reason, max_dissent_rate, summary["verdict"]))
    if outcome.headline():
        printer(f"  readback: {outcome.headline()}")
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")
    return EXIT_MISMATCH if failed else EXIT_OK


def read_labels(
    families: Sequence[Family],
    items: Sequence[Tuple[Any, str, str, Any]],
    readbacks: Dict[str, List[Any]],
    label_field: str,
) -> Tuple[Family, ...]:
    """Attach each payload's label and read state to its variant."""
    labelled: List[Family] = []
    index = 0
    for family in families:
        variants = []
        for variant in family.variants:
            tag = items[index][1]
            index += 1
            read = read_label(readbacks.get(tag, []), label_field)
            variants.append(variant.with_label(read.label, read.state, read.detail))
        labelled.append(dataclasses.replace(family, variants=tuple(variants)))
    return tuple(labelled)


def _repeat_labels(
    families: Sequence[Family], repeated: Sequence[Tuple[Any, str, str, Any]], repeats: int,
    readbacks: Dict[str, List[Any]], label_field: str,
) -> Dict[str, Tuple[Optional[str], ...]]:
    if not repeated:
        return {}
    out: Dict[str, Tuple[Optional[str], ...]] = {}
    position = 0
    for family in families:
        if not family.variants:
            continue
        chunk = repeated[position: position + repeats]
        position += repeats
        out[family.intent_id] = tuple(
            read_label(readbacks.get(tag, []), label_field).label for _e, tag, _r, _p in chunk
        )
    return out
