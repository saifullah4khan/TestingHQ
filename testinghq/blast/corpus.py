"""The corpus plan: what `blast generate`, `fire` and `replay` build from a seed.

A CorpusPlan names everything that decides which payloads a run contains: the
seed, the count, the category mix, the attachment mode, or instead of all of
those a list of catalog cases. `build()` turns it into payloads and `config()`
records it in the run artifact, so `from_config()` can rebuild the identical
corpus later.

Draw order is fixed. The clean corpus comes from `random.Random(seed)`, the
categories and mutations from a second `random.Random(seed)` (blast/corrupt.py),
and the attachments from `random.Random("attachments:<seed>")`. Changing the
attachment mode therefore never changes which category a payload got.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import catalog as catalog_module
from .attachments import ATTACHMENT_MODES, attach, corpus_attachments
from .corrupt import DEFAULT_MIX, RECIPES, corrupt_corpus
from .generate import generate_corpus
from .payload import InboundEmail

DEFAULT_COUNT = 100
DEFAULT_ATTACHMENTS = "seeded"

#: What an artifact written before attachment modes existed was built with.
LEGACY_ATTACHMENTS = "none"

#: Category labels as the artifact writes them, keyed by recipe name.
_LABELS: Dict[str, str] = {name: name.replace("_", "-") for name in RECIPES}

#: Names `--mix` accepts for each recipe, besides the recipe name and label.
_MIX_ALIASES: Dict[str, str] = {
    "messy": "messy_but_valid",
    "multilingual": "multilingual_gibberish",
    "gibberish": "multilingual_gibberish",
    "malformed": "structurally_malformed",
}


class CorpusError(ValueError):
    """A corpus option that cannot be honoured. The message names the fix."""


def label(recipe: str) -> str:
    """The artifact's hyphenated category label for a recipe name."""
    return _LABELS.get(recipe, recipe)


def _recipe_for(name: str) -> Optional[str]:
    key = name.strip().lower()
    if key in _MIX_ALIASES:
        return _MIX_ALIASES[key]
    key = key.replace("-", "_")
    return key if key in RECIPES else None


def parse_mix(spec: str) -> Dict[str, float]:
    """Parse `clean=20,messy=40,...` into recipe name -> weight (a fraction).

    Values are percentages and must add up to 100. A category left out gets
    0. Raises CorpusError naming the problem.
    """
    weights = {name: 0.0 for name in RECIPES}
    seen = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            raise CorpusError(f"--mix entry {item!r} is not name=percent")
        recipe = _recipe_for(name)
        if recipe is None:
            raise CorpusError(
                f"--mix names an unknown category {name.strip()!r}; choose from "
                f"{sorted(set(_LABELS.values()) | set(_MIX_ALIASES))}"
            )
        if recipe in seen:
            raise CorpusError(f"--mix names {label(recipe)!r} twice")
        seen.add(recipe)
        try:
            percent = float(value)
        except ValueError:
            raise CorpusError(f"--mix value for {name.strip()!r} is not a number: {value!r}") from None
        if not 0 <= percent <= 100:
            raise CorpusError(f"--mix value for {name.strip()!r} must be 0 to 100, got {value}")
        weights[recipe] = percent
    total = sum(weights.values())
    if abs(total - 100) > 1e-9:
        raise CorpusError(f"--mix percentages must add up to 100, got {total:g}")
    return {name: percent / 100 for name, percent in weights.items()}


def parse_catalog(spec: str) -> Tuple[str, ...]:
    """`--catalog` with no value (or `all`) is every case in name order;
    otherwise a comma-separated list of case names, kept in the given order."""
    if spec.strip() in ("", "all"):
        return tuple(sorted(catalog_module.CATALOG))
    names = tuple(name.strip() for name in spec.split(",") if name.strip())
    unknown = [name for name in names if name not in catalog_module.CATALOG]
    if unknown:
        raise CorpusError(
            f"--catalog names unknown case(s) {unknown}; choose from "
            f"{sorted(catalog_module.CATALOG)}"
        )
    if len(set(names)) != len(names):
        raise CorpusError("--catalog names a case twice")
    return names


@dataclass(frozen=True)
class CorpusPlan:
    """Everything that decides which payloads a run contains."""

    seed: int = 0
    count: int = DEFAULT_COUNT
    mix: Optional[Mapping[str, float]] = None
    attachments: str = DEFAULT_ATTACHMENTS
    catalog: Optional[Tuple[str, ...]] = None

    def __post_init__(self):
        if self.count < 0:
            raise CorpusError(f"--count must be 0 or more, got {self.count}")
        if self.attachments not in ATTACHMENT_MODES:
            raise CorpusError(
                f"unknown --attachments mode {self.attachments!r}; choose from "
                f"{list(ATTACHMENT_MODES)}"
            )

    @property
    def active_mix(self) -> Mapping[str, float]:
        return self.mix if self.mix is not None else DEFAULT_MIX

    @property
    def size(self) -> int:
        """How many payloads `build()` returns."""
        return len(self.catalog) if self.catalog is not None else self.count

    def build(self) -> List[Tuple[InboundEmail, str]]:
        """`(email, category label)` pairs, deterministic for the plan."""
        if self.catalog is not None:
            return [
                (catalog_module.build(name, self.seed), catalog_module.CATALOG_CATEGORIES[name])
                for name in self.catalog
            ]
        corpus = generate_corpus(self.seed, self.count)
        corrupted = corrupt_corpus(corpus, self.seed, dict(self.active_mix))
        rng = random.Random(f"attachments:{self.seed}")
        return [
            (attach(email, corpus_attachments(rng, self.attachments)), label(recipe))
            for email, recipe in corrupted
        ]

    def cases(self) -> Optional[Tuple[str, ...]]:
        """The catalog case name of each payload, or None for a mix corpus."""
        return self.catalog

    def config(self) -> Dict[str, Any]:
        """The artifact `config` keys that let `from_config` rebuild this."""
        if self.catalog is not None:
            return {
                "mix": [],
                "count": len(self.catalog),
                "seed": self.seed,
                "catalog": list(self.catalog),
            }
        mix = self.active_mix
        return {
            "mix": [label(name) for name, weight in mix.items() if weight > 0],
            "mix_percent": {label(name): round(weight * 100, 6) for name, weight in mix.items()},
            "attachments": self.attachments,
            "count": self.count,
            "seed": self.seed,
        }

    @classmethod
    def from_config(cls, seed: int, config: Mapping[str, Any]) -> "CorpusPlan":
        """Rebuild the plan an artifact's `config` records. An artifact from
        before mixes and attachment modes were recorded gets the default mix
        and no attachments, which is what it was built with."""
        catalog = config.get("catalog")
        if catalog is not None:
            return cls(seed=seed, count=len(catalog), catalog=tuple(catalog))
        mix = None
        percents = config.get("mix_percent")
        if isinstance(percents, Mapping):
            mix = parse_mix(",".join(f"{name}={value}" for name, value in percents.items()))
        return cls(
            seed=seed,
            count=config.get("count", 0),
            mix=mix,
            attachments=config.get("attachments", LEGACY_ATTACHMENTS),
        )


def mix_labels(mix: Optional[Mapping[str, float]] = None) -> List[str]:
    """Labels of the categories with a non-zero weight in `mix`."""
    active = mix if mix is not None else DEFAULT_MIX
    return [label(name) for name, weight in active.items() if weight > 0]

