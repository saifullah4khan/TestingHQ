"""The corpus plan behind `blast generate|fire|replay`: --mix, --catalog,
--attachments, determinism, and headers that agree with the fields."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from email.header import decode_header
from pathlib import Path

import pytest

from testinghq.blast import catalog
from testinghq.blast.attachments import DEFAULT_OVERSIZED_SIZE
from testinghq.blast.corpus import (
    CorpusError,
    CorpusPlan,
    parse_catalog,
    parse_mix,
)
from testinghq.core import report
from testinghq.core.transport import build_request

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# --mix
# ---------------------------------------------------------------------------


def test_mix_accepts_short_names_and_labels():
    mix = parse_mix("clean=20,messy=40,multilingual=20,malformed=15,degenerate=5")
    assert mix == {
        "clean": 0.20,
        "messy_but_valid": 0.40,
        "multilingual_gibberish": 0.20,
        "structurally_malformed": 0.15,
        "degenerate": 0.05,
    }
    assert parse_mix("messy-but-valid=100")["messy_but_valid"] == 1.0


@pytest.mark.parametrize(
    "spec, message",
    [
        ("clean=50,degenerate=40", "add up to 100"),
        ("clean=150", "0 to 100"),
        ("clean=x", "not a number"),
        ("clean", "name=percent"),
        ("bogus=100", "unknown category"),
        ("clean=50,clean=50", "twice"),
    ],
)
def test_a_bad_mix_is_refused_with_the_reason(spec, message):
    with pytest.raises(CorpusError, match=message):
        parse_mix(spec)


def test_a_mix_decides_the_categories():
    plan = CorpusPlan(seed=3, count=40, mix=parse_mix("degenerate=100"))
    assert {label for _email, label in plan.build()} == {report.DEGENERATE}
    assert plan.config()["mix"] == ["degenerate"]


# ---------------------------------------------------------------------------
# --catalog
# ---------------------------------------------------------------------------


def test_the_whole_catalog_is_every_case_once():
    plan = CorpusPlan(seed=0, catalog=parse_catalog("all"))
    assert plan.size == len(catalog.CATALOG)
    assert sorted(plan.cases()) == sorted(catalog.CATALOG)
    labels = [label for _email, label in plan.build()]
    assert labels == [catalog.CATALOG_CATEGORIES[name] for name in plan.cases()]


def test_a_named_catalog_keeps_the_given_order():
    names = parse_catalog("many_recipients,empty_subject")
    assert names == ("many_recipients", "empty_subject")


@pytest.mark.parametrize("spec", ["no_such_case", "empty_subject,empty_subject"])
def test_a_bad_catalog_is_refused(spec):
    with pytest.raises(CorpusError):
        parse_catalog(spec)


# ---------------------------------------------------------------------------
# --attachments and --count
# ---------------------------------------------------------------------------


def _attached(plan):
    return [email for email, _label in plan.build() if email.attachments]


def test_the_default_corpus_carries_seeded_attachments():
    plan = CorpusPlan(seed=0, count=300)
    attached = _attached(plan)
    assert plan.attachments == "seeded"
    assert 60 <= len(attached) <= 140, "roughly a third of payloads"
    for email in attached:
        assert 1 <= len(email.attachments) <= 3
        assert email.headers["Content-Type"].startswith("multipart/mixed")
        for attachment in email.attachments:
            assert 256 <= len(attachment.content) <= 16384


def test_attachments_none_attaches_nothing():
    assert _attached(CorpusPlan(seed=0, count=200, attachments="none")) == []


def test_adversarial_attachments_include_hostile_ones():
    attachments = [
        attachment
        for email in _attached(CorpusPlan(seed=0, count=300, attachments="adversarial"))
        for attachment in email.attachments
    ]
    assert any(len(a.content) == 0 for a in attachments), "a zero-byte file"
    assert any(len(a.content) == DEFAULT_OVERSIZED_SIZE for a in attachments), "an oversized file"
    assert any("‮" in a.filename for a in attachments), "a right-to-left override name"


def test_the_attachment_mode_does_not_move_the_categories():
    def labels(mode):
        return [label for _e, label in CorpusPlan(seed=4, count=80, attachments=mode).build()]

    assert labels("none") == labels("seeded") == labels("adversarial")


def test_a_negative_count_is_refused():
    with pytest.raises(CorpusError, match="--count must be 0 or more"):
        CorpusPlan(seed=0, count=-1)


def test_a_plan_rebuilds_from_its_artifact_config():
    for plan in (
        CorpusPlan(seed=5, count=12, mix=parse_mix("clean=50,degenerate=50"), attachments="adversarial"),
        CorpusPlan(seed=5, catalog=parse_catalog("empty_subject,many_recipients")),
    ):
        rebuilt = CorpusPlan.from_config(plan.seed, plan.config())
        assert _digest(rebuilt) == _digest(plan)


def test_an_artifact_from_before_attachment_modes_rebuilds_without_attachments():
    plan = CorpusPlan.from_config(0, {"mix": ["clean"], "count": 30, "seed": 0})
    assert plan.attachments == "none"


# ---------------------------------------------------------------------------
# Determinism: the same seed is the same bytes, in any process
# ---------------------------------------------------------------------------


def _digest(plan):
    hasher = hashlib.sha256()
    for email, label in plan.build():
        hasher.update(label.encode("utf-8"))
        hasher.update(build_request(email, "http://localhost/inbound").body)
    return hasher.hexdigest()


_DIGEST_SCRIPT = """
import hashlib, sys
from testinghq.blast.corpus import CorpusPlan, parse_catalog
from testinghq.core.transport import build_request
seed = int(sys.argv[1])
plans = [CorpusPlan(seed=seed, count=60, attachments=mode)
         for mode in ("none", "seeded", "adversarial")]
plans.append(CorpusPlan(seed=seed, catalog=parse_catalog("all")))
hasher = hashlib.sha256()
for plan in plans:
    for email, label in plan.build():
        hasher.update(label.encode("utf-8"))
        hasher.update(build_request(email, "http://localhost/inbound").body)
print(hasher.hexdigest())
"""


def _digest_in_a_fresh_process(seed, hash_seed):
    env = dict(os.environ, PYTHONHASHSEED=str(hash_seed))
    result = subprocess.run(
        [sys.executable, "-c", _DIGEST_SCRIPT, str(seed)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, check=True,
    )
    return result.stdout.strip()


def test_the_same_seed_is_a_byte_identical_corpus_across_processes():
    """String hashing is randomized per process, so a set or dict order that
    leaked into the corpus would show up here and nowhere else."""
    first = _digest_in_a_fresh_process(11, hash_seed=1)
    second = _digest_in_a_fresh_process(11, hash_seed=2)
    assert first == second
    assert first != _digest_in_a_fresh_process(12, hash_seed=1)


def test_the_same_seed_is_a_byte_identical_corpus_in_process():
    plan = CorpusPlan(seed=8, count=50, attachments="adversarial")
    assert _digest(plan) == _digest(CorpusPlan(seed=8, count=50, attachments="adversarial"))


# ---------------------------------------------------------------------------
# Headers agree with the fields, after every mutation
# ---------------------------------------------------------------------------


def _decoded(value):
    """A header as a receiver decodes it, RFC 2047 words included."""
    pieces = []
    for piece, charset in decode_header(value):
        if isinstance(piece, bytes):
            pieces.append(piece.decode(charset or "ascii", "replace"))
        else:
            pieces.append(piece)
    return "".join(pieces)


_HEADER_PLANS = [
    pytest.param(CorpusPlan(seed=seed, count=200, attachments=mode), id=f"mix-{mode}-{seed}")
    for seed in (0, 7)
    for mode in ("none", "seeded", "adversarial")
] + [pytest.param(CorpusPlan(seed=seed, catalog=parse_catalog("all")), id=f"catalog-{seed}") for seed in (0, 3)]


@pytest.mark.parametrize("plan", _HEADER_PLANS)
def test_subject_to_and_from_headers_match_their_fields(plan):
    """SendGrid derives both from the same message, so they never disagree on
    the wire. A sabotaged subject decodes to the same replacement characters
    in both places."""
    for email, label in plan.build():
        for attr, header in (("subject", "Subject"), ("to", "To"), ("from_addr", "From")):
            value = getattr(email, attr)
            assert header in email.headers, (label, header)
            got = email.headers[header]
            assert got == value or _decoded(got) == value, (label, header, got[:80], value[:80])
