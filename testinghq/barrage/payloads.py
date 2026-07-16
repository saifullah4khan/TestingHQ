"""Payload source for Barrage.

Barrage is a load generator for an endpoint the operator controls: it
measures throughput, latency, and error behaviour under volume, not parser
correctness under messy input. That is Blast's job. So this module reuses
blast.generate.generate_corpus for realistic-but-VALID bodies and
deliberately never touches blast/corrupt.py: nothing here garbles a payload.
Clean load only.

Reuse, not reimplementation: generate_corpus already produces deterministic,
seeded, reserved-domain-only InboundEmail objects (see
testinghq/blast/generate.py). This module's only job is to turn a seed into
a bounded pool of those objects and let a run of any length draw from that
pool deterministically and reproducibly, without regenerating a
run-length-sized corpus up front.
"""
from __future__ import annotations

from typing import List

from ..blast.generate import generate_corpus
from ..blast.payload import InboundEmail

DEFAULT_POOL_SIZE = 50


def build_payload_pool(seed: int, pool_size: int = DEFAULT_POOL_SIZE) -> List[InboundEmail]:
    """Generate a deterministic pool of `pool_size` well-formed InboundEmail
    objects from `seed`, via blast.generate.generate_corpus. Same seed and
    pool_size always yields the same pool, field for field.
    """
    if pool_size <= 0:
        raise ValueError(f"pool_size must be > 0, got {pool_size!r}")
    return generate_corpus(seed, pool_size)


def payload_for_index(pool: List[InboundEmail], index: int) -> InboundEmail:
    """Deterministically pick a payload for the `index`-th dispatched
    request of a run, by cycling through `pool`. A sustained-load run will
    typically dispatch far more requests than the pool has entries; cycling
    keeps every request's payload reproducible (same index always yields the
    same payload for a given seed and pool_size) without needing a corpus as
    large as the run itself.
    """
    if not pool:
        raise ValueError("pool must not be empty")
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index!r}")
    return pool[index % len(pool)]
