import random

import pytest

from testinghq.blast.attachments import (
    DEFAULT_CLEAN_SIZE,
    GENERATORS,
    clean_attachment,
    degenerate_attachment,
    generate_attachment,
    generate_attachments,
    oversized_attachment,
    zero_byte_attachment,
)


def test_clean_attachment_has_matching_extension_and_content_type():
    from testinghq.blast.attachments import COMMON_TYPES

    attachment = clean_attachment(random.Random(1))
    ext = attachment.filename.rsplit(".", 1)[-1]
    assert (ext, attachment.content_type) in COMMON_TYPES
    assert len(attachment.content) == DEFAULT_CLEAN_SIZE
    assert attachment.filename


def test_clean_attachment_starts_with_magic_bytes_when_known():
    # Force a pdf/png/jpg by trying seeds until we hit one with known magic.
    from testinghq.blast.attachments import MAGIC_BYTES

    for seed in range(50):
        attachment = clean_attachment(random.Random(seed))
        ext = attachment.filename.rsplit(".", 1)[-1]
        if ext in MAGIC_BYTES:
            assert attachment.content.startswith(MAGIC_BYTES[ext])
            return
    pytest.fail("no seed in range produced a magic-byte extension")


def test_zero_byte_attachment_has_no_content():
    attachment = zero_byte_attachment(random.Random(2))
    assert attachment.content == b""
    assert attachment.filename
    assert attachment.content_type


def test_oversized_attachment_is_larger_than_clean_default():
    attachment = oversized_attachment(random.Random(3), size=1_000_000)
    assert len(attachment.content) == 1_000_000
    assert len(attachment.content) > DEFAULT_CLEAN_SIZE


def test_degenerate_attachment_uses_awkward_filename_pool():
    from testinghq.blast.attachments import DEGENERATE_FILENAMES

    attachment = degenerate_attachment(random.Random(4))
    assert attachment.filename in DEGENERATE_FILENAMES


def test_generate_attachment_dispatches_by_kind():
    for kind in GENERATORS:
        attachment = generate_attachment(random.Random(1), kind=kind)
        assert attachment.content_type is not None


def test_generate_attachment_rejects_unknown_kind():
    with pytest.raises(ValueError):
        generate_attachment(random.Random(1), kind="not-a-real-kind")


def test_generate_attachments_returns_requested_count():
    attachments = generate_attachments(random.Random(5), 12)
    assert len(attachments) == 12


def test_generate_attachments_rejects_negative_count():
    with pytest.raises(ValueError):
        generate_attachments(random.Random(5), -1)


def test_generate_attachments_all_clean_when_weighted_so():
    attachments = generate_attachments(random.Random(5), 10, weights={"clean": 1.0})
    for attachment in attachments:
        assert len(attachment.content) == DEFAULT_CLEAN_SIZE


# ---------------------------------------------------------------------------
# Determinism: seeded bytes reproduce exactly, filenames and all.
# ---------------------------------------------------------------------------


def test_clean_attachment_is_deterministic_for_same_seed():
    first = clean_attachment(random.Random(42))
    second = clean_attachment(random.Random(42))
    assert first == second


def test_oversized_attachment_bytes_are_deterministic_for_same_seed():
    first = oversized_attachment(random.Random(42), size=50_000)
    second = oversized_attachment(random.Random(42), size=50_000)
    assert first.content == second.content


def test_degenerate_attachment_is_deterministic_for_same_seed():
    first = degenerate_attachment(random.Random(42))
    second = degenerate_attachment(random.Random(42))
    assert first == second


def test_generate_attachments_is_deterministic_for_same_seed():
    first = generate_attachments(random.Random(7), 20)
    second = generate_attachments(random.Random(7), 20)
    assert first == second


def test_generate_attachments_uses_a_mix_of_kinds_by_default():
    """Over enough draws, the default weights should surface more than just
    the single most common kind, otherwise the "types, sizes, zero-byte,
    oversized, degenerate" spread this module promises is not happening."""
    attachments = generate_attachments(random.Random(123), 200)
    sizes = {len(a.content) for a in attachments}
    assert 0 in sizes  # zero-byte kind showed up
    assert len(sizes) > 2  # more than one distinct size class present
