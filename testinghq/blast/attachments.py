"""Seeded attachment generation for Blast.

Produces Attachment objects (blast/payload.py) across a spectrum: ordinary
small files of common types, zero-byte files, oversized files, and
degenerate ones (awkward filenames, empty or nonsense content types,
extension-vs-content mismatches). Every byte comes from the caller's
random.Random, via Random.randbytes, never from os.urandom or any other
unseeded source, so a fixed seed always reproduces byte-identical attachment
content, filenames included.
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence, Tuple

from .payload import Attachment, InboundEmail, find_header

# (extension, content type) pairs used for "ordinary" attachments.
COMMON_TYPES: Tuple[Tuple[str, str], ...] = (
    ("txt", "text/plain"),
    ("csv", "text/csv"),
    ("pdf", "application/pdf"),
    ("png", "image/png"),
    ("jpg", "image/jpeg"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("json", "application/json"),
    ("html", "text/html"),
)

FILE_STEMS: Tuple[str, ...] = (
    "invoice", "receipt", "report", "photo", "scan", "attachment",
    "document", "signature", "form", "export",
)

# Minimal, real magic-byte headers so a "normal" generated attachment at
# least looks like the type it claims to be before seeded filler bytes.
MAGIC_BYTES: Dict[str, bytes] = {
    "pdf": b"%PDF-1.4\n",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff\xe0",
}

DEFAULT_CLEAN_SIZE = 2048
DEFAULT_OVERSIZED_SIZE = 5 * 1024 * 1024  # 5 MiB


def _seeded_bytes(rng: random.Random, size: int) -> bytes:
    """`size` bytes drawn from `rng`. Uses Random.randbytes (stdlib,
    Python 3.9+) rather than a per-byte loop so large sizes stay fast, while
    remaining fully determined by the rng's state."""
    if size <= 0:
        return b""
    return rng.randbytes(size)


def _filename(rng: random.Random, ext: str) -> str:
    stem = rng.choice(FILE_STEMS)
    suffix = rng.randint(0, 9999)
    return f"{stem}{suffix}.{ext}" if suffix else f"{stem}.{ext}"


def clean_attachment(rng: random.Random, size: int = DEFAULT_CLEAN_SIZE) -> Attachment:
    """An ordinary, well-formed attachment: a real extension, a matching
    content type, and a body that starts with the right magic bytes when the
    format has one, seeded filler bytes after that."""
    ext, content_type = rng.choice(COMMON_TYPES)
    header = MAGIC_BYTES.get(ext, b"")
    filler = _seeded_bytes(rng, max(0, size - len(header)))
    return Attachment(
        filename=_filename(rng, ext), content_type=content_type, content=header + filler
    )


def zero_byte_attachment(rng: random.Random) -> Attachment:
    """A zero-length attachment: a real filename and content type, but no
    bytes at all. A common real-world edge case (interrupted upload, empty
    export)."""
    ext, content_type = rng.choice(COMMON_TYPES)
    return Attachment(filename=_filename(rng, ext), content_type=content_type, content=b"")


def oversized_attachment(
    rng: random.Random, size: int = DEFAULT_OVERSIZED_SIZE
) -> Attachment:
    """A large attachment (default 5 MiB) to probe size-limit handling.
    Still fully seeded and therefore reproducible; callers that want faster
    tests should pass a smaller `size`."""
    ext, content_type = rng.choice(COMMON_TYPES)
    return Attachment(
        filename=_filename(rng, ext), content_type=content_type, content=_seeded_bytes(rng, size)
    )


#: U+202E RIGHT-TO-LEFT OVERRIDE: `invoice<RLO>fdp.exe` displays as
#: `invoiceexe.pdf` while the real extension is `.exe`.
RLO = "\u202e"

DEGENERATE_FILENAMES: Tuple[str, ...] = (
    "",
    " ",
    "...",
    "no_extension",
    "a" * 255 + ".txt",
    "../../etc/passwd.txt",
    "CON.txt",
    "file.tar.gz.exe",
    ".hidden",
    f"invoice{RLO}fdp.exe",
    "résumé final (2).pdf",
    "<script>alert(1)</script>.html",
    "report.pdf     .exe",
)

DEGENERATE_CONTENT_TYPES: Tuple[str, ...] = (
    "",
    "application/octet-stream",
    "x-testinghq/unknown",
    "text/plain",
)

DEGENERATE_SIZES: Tuple[int, ...] = (0, 1, 16, 256)


def degenerate_attachment(rng: random.Random) -> Attachment:
    """An attachment built to be awkward: an empty, path-traversal-shaped,
    reserved-device-name-shaped, or extremely long filename; an empty or
    nonsense content type; content that does not necessarily match either.
    Still fully seeded."""
    filename = rng.choice(DEGENERATE_FILENAMES)
    content_type = rng.choice(DEGENERATE_CONTENT_TYPES)
    size = rng.choice(DEGENERATE_SIZES)
    return Attachment(filename=filename, content_type=content_type, content=_seeded_bytes(rng, size))


def rtl_override_attachment(rng: random.Random) -> Attachment:
    """A file named so it displays as a PDF while ending in `.exe`, with a PE
    header under a PDF content type."""
    stem = rng.choice(FILE_STEMS)
    return Attachment(
        filename=f"{stem}{RLO}fdp.exe",
        content_type="application/pdf",
        content=b"MZ" + _seeded_bytes(rng, 254),
    )


GENERATORS = {
    "clean": clean_attachment,
    "zero_byte": zero_byte_attachment,
    "oversized": oversized_attachment,
    "degenerate": degenerate_attachment,
    "rtl_override": rtl_override_attachment,
}

DEFAULT_KIND_WEIGHTS: Dict[str, float] = {
    "clean": 0.70,
    "zero_byte": 0.10,
    "oversized": 0.05,
    "degenerate": 0.15,
}

#: Kinds drawn for `--attachments adversarial`.
ADVERSARIAL_KIND_WEIGHTS: Dict[str, float] = {
    "clean": 0.50,
    "zero_byte": 0.10,
    "oversized": 0.05,
    "degenerate": 0.25,
    "rtl_override": 0.10,
}

#: The `--attachments` modes.
ATTACHMENT_MODES: Tuple[str, ...] = ("none", "seeded", "adversarial")

#: Share of payloads that get attachments in the seeded and adversarial modes.
ATTACHED_SHARE = 1 / 3


def generate_attachment(rng: random.Random, kind: str = "clean") -> Attachment:
    """Generate one attachment of the given `kind` (see GENERATORS)."""
    try:
        generator = GENERATORS[kind]
    except KeyError:
        raise ValueError(
            f"unknown attachment kind: {kind!r}; choose from {sorted(GENERATORS)}"
        ) from None
    return generator(rng)


def generate_attachments(
    rng: random.Random, count: int, weights: Optional[Dict[str, float]] = None
) -> List[Attachment]:
    """Generate `count` attachments, each of a kind drawn (weighted) from
    GENERATORS. Default weights favor ordinary clean attachments, with
    zero-byte, oversized, and degenerate as rarer edge cases. All draws
    (kind choice and content) come from `rng`, so this is fully
    reproducible for a given rng state and `count`."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    active_weights = weights if weights is not None else DEFAULT_KIND_WEIGHTS
    kinds = list(GENERATORS.keys())
    kind_weights = [active_weights.get(kind, 0.0) for kind in kinds]
    return [
        generate_attachment(rng, rng.choices(kinds, weights=kind_weights, k=1)[0])
        for _ in range(count)
    ]


def corpus_attachments(rng: random.Random, mode: str) -> Tuple[Attachment, ...]:
    """Attachments for one corpus payload under `mode`.

    `none` attaches nothing. `seeded` gives about a third of payloads one to
    three ordinary attachments of 256 bytes to 16 KiB. `adversarial` attaches
    to the same payloads, drawing kinds from ADVERSARIAL_KIND_WEIGHTS: hostile
    filenames, zero-byte, 5 MiB and right-to-left override files.
    """
    if mode not in ATTACHMENT_MODES:
        raise ValueError(f"unknown attachments mode: {mode!r}; choose from {list(ATTACHMENT_MODES)}")
    if mode == "none" or rng.random() >= ATTACHED_SHARE:
        return ()
    count = rng.randint(1, 3)
    if mode == "seeded":
        return tuple(clean_attachment(rng, rng.randint(256, 16384)) for _ in range(count))
    return tuple(generate_attachments(rng, count, ADVERSARIAL_KIND_WEIGHTS))


def attach(email: InboundEmail, attachments: Sequence[Attachment]) -> InboundEmail:
    """`email` with `attachments` added to any it has, its Content-Type changed
    from multipart/alternative to multipart/mixed when there are any."""
    attachments = tuple(attachments)
    if not attachments:
        return email.with_changes(attachments=())
    headers = dict(email.headers)
    key = find_header(headers, "Content-Type")
    if key is not None:
        headers[key] = headers[key].replace("multipart/alternative", "multipart/mixed", 1)
    return email.with_changes(attachments=email.attachments + attachments, headers=headers)
