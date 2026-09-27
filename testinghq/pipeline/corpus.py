"""The corpus the pipeline tools verify, and the tags that make it findable.

A payload is only verifiable if the system that received it can be found again
later, and the only handle guaranteed to survive an arbitrary intake pipeline is
one the pipeline was not expecting: a unique token carried in a header, in the
body, and in the Message-ID. `build_tagged_corpus` is where those tokens are
made and stamped.

Determinism is the whole contract here. Every tag is a pure function of
(prefix, seed, index), and the per-payload attachment rng is seeded from
(seed, index) rather than drawn from one stream, so payload 7 is the same
whether it was generated alone or as part of a corpus of forty. A shared stream
would make a payload depend on how many were generated before it, which is the
kind of coupling that makes a replay stop reproducing a run.

ATTACHMENTS, and why this module builds its own corpus. `blast.generate`
attaches nothing, so a corpus taken from it can never exercise an attachment
check, and verify's attachment check is one of the six things it exists to run.
A deterministic subset of payloads here carries seeded attachments, so the check
is reachable. The rate is below 1.0 on purpose: a corpus where every payload
has a file can only ever produce "the attachment check passed", and never the
"nothing to check" skip that a real system with no files produces.
"""
from __future__ import annotations

import random
from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

from ..blast.attachments import generate_attachment
from ..blast.generate import generate_corpus
from ..blast.payload import InboundEmail
from ..core import report
from .messages import DEFAULT_TAG_PREFIX, Probe, make_tag, probe_for, stamp_corpus

DEFAULT_RATE = 5.0
DEFAULT_SEED = 0
DEFAULT_ATTACHMENT_RATE = 0.35

#: Only ordinary and zero-byte attachments. An oversized or
#: path-traversal-shaped file being dropped is a pipeline enforcing a limit,
#: which is a policy question rather than a parse bug.
_VERIFIED_ATTACHMENT_KINDS = ("clean", "clean", "zero_byte")


def build_clean_corpus(
    seed: int, count: int, attachment_rate: float = DEFAULT_ATTACHMENT_RATE
) -> List[InboundEmail]:
    """`generate_corpus`, with a deterministic subset carrying attachments."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if not 0.0 <= attachment_rate <= 1.0:
        raise ValueError(
            f"attachment_rate must be between 0 and 1, got {attachment_rate}"
        )

    corpus = generate_corpus(seed, count)
    with_files: List[InboundEmail] = []
    for index, email in enumerate(corpus):
        rng = random.Random(f"testinghq:attachments:{seed}:{index}")
        if rng.random() < attachment_rate:
            kind = rng.choice(_VERIFIED_ATTACHMENT_KINDS)
            with_files.append(
                replace(
                    email,
                    attachments=tuple(
                        generate_attachment(rng, kind)
                        for _ in range(rng.randint(1, 2))
                    ),
                )
            )
        else:
            with_files.append(email)
    return with_files


def build_tagged_corpus(
    seed: int,
    count: int,
    tag_prefix: str,
    attachment_rate: float = DEFAULT_ATTACHMENT_RATE,
) -> List[Tuple[InboundEmail, str, str]]:
    """The corpus with each payload stamped with its own tag, as
    (email, tag, record_id)."""
    return stamp_corpus(
        build_clean_corpus(seed, count, attachment_rate), seed, tag_prefix
    )


def build_items(
    corpus: Sequence[Tuple[InboundEmail, str, str]]
) -> List[Tuple[InboundEmail, str, str, Probe]]:
    """Pair each payload with the probe an adapter will be handed.

    The payload hash is computed once, here, and carried in the probe, so the
    identity the report publishes and the identity the adapter was asked about
    cannot be two different calculations of the same thing.
    """
    return [
        (
            email,
            tag,
            record_id,
            probe_for(email, tag, record_id, report.payload_sha256(email)),
        )
        for email, tag, record_id in corpus
    ]


__all__ = [
    "DEFAULT_ATTACHMENT_RATE",
    "DEFAULT_RATE",
    "DEFAULT_SEED",
    "DEFAULT_TAG_PREFIX",
    "build_clean_corpus",
    "build_items",
    "build_tagged_corpus",
    "make_tag",
    "probe_for",
    "record_id_for",
    "stamp_corpus",
]
