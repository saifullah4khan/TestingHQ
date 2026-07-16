"""Composable seeded mutators: Blast's chaos layer.

Each mutator below is a pure function `(InboundEmail, random.Random) ->
InboundEmail`. Mutators never touch their input in place (InboundEmail is
frozen; every mutator returns a new instance via dataclasses.replace) and
never read the wall clock or any global random state; every choice comes
from the `rng` argument the caller supplies, so the same starting email plus
the same rng state always produces the same corrupted email.

The six mutator categories:
    typo               - character-level typo/transposition noise
    homoglyph_mojibake  - look-alike character substitution and classic
                          wrong-encoding-round-trip garbling
    script_mixing       - foreign-script words spliced into the message
    word_salad          - content words replaced with nonsense, greetings
                          and connectors left intact
    structural_noise    - quoted reply chains, inline forwarded headers,
                          broken HTML, signature blocks, and (rarely, as one
                          of its possible operations) blanked or truncated
                          fields
    encoding_sabotage   - the charsets field declares one charset while the
                          content was produced as if encoded in another

Messiness levels (clean, messy-but-valid, multilingual/gibberish,
structurally malformed, degenerate) are recipes over this same mutator set,
not separate code paths: see RECIPES and corrupt_email/corrupt_corpus below.

Reserved-domain contract: no mutator here ever touches `to`, `from_addr`, or
`envelope` (the address-bearing fields). Corruption is confined to subject,
text, html, and charsets/headers. This is a deliberate design choice, not an
incidental one: it makes "a mutator must never garble an address into a
non-reserved domain" trivially true for every mutator and every messiness
level, including degenerate, because address fields simply pass through
untouched. Where a mutator needs to *mention* a synthetic address (quoted
reply chains, forwarded-message headers), it draws one from the same
reserved-domain pool the clean generator uses (see `_random_address`).
"""
from __future__ import annotations

import dataclasses
import random
import re
import string
from typing import Callable, Dict, List, Sequence, Tuple

from .generate import FIRST_NAMES, LAST_NAMES, MONTHS, RESERVED_DOMAINS
from .payload import InboundEmail

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _random_address(rng: random.Random) -> str:
    """A synthetic address on a reserved domain, for mutators that need to
    *mention* an address in body text (quote chains, forwarded headers)
    without touching the email's real to/from/envelope fields."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return f"{first}.{last}{rng.randint(0, 999)}@{rng.choice(RESERVED_DOMAINS)}"


def _random_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES).capitalize()} {rng.choice(LAST_NAMES).capitalize()}"


def _fake_date(rng: random.Random) -> str:
    return f"{rng.choice(MONTHS)} {rng.randint(1, 28)}, {rng.randint(2022, 2025)}"


# Splits text into alternating [word, whitespace-run, word, ...] tokens,
# keeping the whitespace (including newlines) as its own tokens rather than
# discarding it. Splitting on a literal " " alone would merge e.g.
# "team,\n\nThis" into one bogus token and, for a mutator that replaces or
# rewrites whole tokens, destroy the blank line between paragraphs in the
# process. Shared by typo and word_salad below.
_WHITESPACE_RUN = re.compile(r"(\s+)")


# ---------------------------------------------------------------------------
# 1. typo / transposition
# ---------------------------------------------------------------------------


def _typo_word(rng: random.Random, word: str) -> str:
    if len(word) < 3:
        return word
    op = rng.choice(("swap", "duplicate", "drop"))
    idx = rng.randint(0, len(word) - 2)
    if op == "swap":
        chars = list(word)
        chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
        return "".join(chars)
    if op == "duplicate":
        return word[:idx] + word[idx] + word[idx:]
    return word[:idx] + word[idx + 1 :]  # drop


def _mangle_typos(rng: random.Random, text: str) -> str:
    tokens = _WHITESPACE_RUN.split(text)
    word_indices = [i for i, tok in enumerate(tokens) if tok and not tok.isspace()]
    if not word_indices:
        return text
    count = max(1, len(word_indices) // 5)
    chosen = rng.sample(word_indices, k=min(count, len(word_indices)))
    for i in chosen:
        tokens[i] = _typo_word(rng, tokens[i])
    return "".join(tokens)


def typo(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Swap, duplicate, or drop one character in a handful of words, in the
    subject and body text. Keeps html untouched: typos are a
    human-typing-error simulation, not a markup corruption."""
    return dataclasses.replace(
        email,
        subject=_mangle_typos(rng, email.subject),
        text=_mangle_typos(rng, email.text),
    )


# ---------------------------------------------------------------------------
# 2. homoglyph / mojibake
# ---------------------------------------------------------------------------

# Latin -> visually-similar Cyrillic homoglyph, lower-case keys.
HOMOGLYPH_MAP: Dict[str, str] = {
    "a": "а",  # а
    "e": "е",  # е
    "o": "о",  # о
    "p": "р",  # р
    "c": "с",  # с
    "x": "х",  # х
    "y": "у",  # у
    "i": "і",  # і
}

# (actual encoding, declared/decoded-as encoding) pairs used to simulate a
# classic wrong-encoding round trip. Always distinct.
MOJIBAKE_CODEC_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("utf-8", "latin-1"),
    ("utf-8", "cp1252"),
    ("latin-1", "utf-8"),
)


def _apply_homoglyphs(rng: random.Random, text: str, rate: float = 0.2) -> str:
    chars = list(text)
    for i, ch in enumerate(chars):
        lower = ch.lower()
        replacement = HOMOGLYPH_MAP.get(lower)
        if replacement and rng.random() < rate:
            chars[i] = replacement.upper() if ch.isupper() else replacement
    return "".join(chars)


def _mojibake_round_trip(text: str, actual_codec: str, declared_codec: str) -> str:
    raw = text.encode(actual_codec, errors="replace")
    return raw.decode(declared_codec, errors="replace")


def homoglyph_mojibake(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Substitute look-alike characters and/or run a wrong-encoding round
    trip on subject/text/html. Unlike encoding_sabotage, this does not touch
    the charsets field: it simulates content that already arrived garbled,
    independent of what the charsets metadata claims."""
    mode = rng.choice(("homoglyph", "mojibake", "both"))
    subject, text, html = email.subject, email.text, email.html

    if mode in ("homoglyph", "both"):
        subject = _apply_homoglyphs(rng, subject)
        text = _apply_homoglyphs(rng, text)

    if mode in ("mojibake", "both"):
        actual_codec, declared_codec = rng.choice(MOJIBAKE_CODEC_PAIRS)
        text = _mojibake_round_trip(text, actual_codec, declared_codec)
        html = _mojibake_round_trip(html, actual_codec, declared_codec)

    return dataclasses.replace(email, subject=subject, text=text, html=html)


# ---------------------------------------------------------------------------
# 3. script mixing
# ---------------------------------------------------------------------------

SCRIPT_WORD_POOLS: Tuple[Tuple[str, ...], ...] = (
    ("привет", "спасибо", "документ"),  # Cyrillic: hello, thanks, document
    ("γειά", "ευχαριστώ", "έγγραφο"),  # Greek: hi, thanks, document
    ("你好", "谢谢", "文件"),  # CJK: hello, thanks, document
    ("مرحبا", "شكرا", "مستند"),  # Arabic: hello, thanks, document
)


def script_mixing(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Splice a few words from another script into subject/text/html,
    simulating a multilingual or copy-pasted-from-elsewhere message."""
    pool = rng.choice(SCRIPT_WORD_POOLS)
    k = min(len(pool), rng.randint(1, 2))
    insertions = rng.sample(pool, k=k)
    snippet = " ".join(insertions)

    text = f"{email.text}\n\n{snippet}"
    html = f"{email.html}<p>{snippet}</p>"
    subject = f"{email.subject} {rng.choice(pool)}" if rng.random() < 0.5 else email.subject

    return dataclasses.replace(email, subject=subject, text=text, html=html)


# ---------------------------------------------------------------------------
# 4. word salad
# ---------------------------------------------------------------------------

CONNECTORS = frozenset(
    {
        "hi", "hello", "hey", "dear", "team", "good", "morning",
        "thanks", "best", "regards", "cheers", "sincerely",
        "the", "a", "an", "and", "or", "but", "to", "from", "of", "in",
        "on", "at", "for", "is", "are", "was", "were", "this", "that",
        "these", "those", "please", "let", "me", "know", "if", "you",
        "your", "have", "has", "had", "any", "questions", "i", "we", "it",
        "with", "about", "message", "reply", "can", "will", "be",
    }
)

GIBBERISH_SYLLABLES: Tuple[str, ...] = (
    "zar", "fen", "blo", "tik", "mur", "vex", "dol", "qui", "nal", "bex",
    "wor", "plin", "kesh", "num", "flor",
)


def _salad_word(rng: random.Random, word: str) -> str:
    core = word.strip(string.punctuation)
    if not core or core.lower() in CONNECTORS or len(core) <= 3:
        return word
    prefix_len = len(word) - len(word.lstrip(string.punctuation))
    prefix, suffix = word[:prefix_len], word[prefix_len + len(core) :]
    syllables = rng.randint(2, 3)
    gibberish = "".join(rng.choice(GIBBERISH_SYLLABLES) for _ in range(syllables))
    if core[:1].isupper():
        gibberish = gibberish.capitalize()
    return f"{prefix}{gibberish}{suffix}"


def _salad(rng: random.Random, text: str) -> str:
    """Tokenize on runs of whitespace, keeping the whitespace itself
    (including newlines) as separate tokens passed through unchanged, so
    paragraph breaks survive. Splitting on a literal " " alone would merge
    e.g. "team,\\n\\nThis" into one bogus token and destroy the blank line
    between paragraphs; splitting on `\\s+` with a capturing group avoids
    that."""
    tokens = _WHITESPACE_RUN.split(text)
    return "".join(
        token if token == "" or token.isspace() else _salad_word(rng, token)
        for token in tokens
    )


def word_salad(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Replace content words (anything not a short connector/greeting word)
    with nonsense syllables, in subject and body text. html is left alone
    so the message keeps *a* readable structure even when its words don't
    mean anything."""
    return dataclasses.replace(
        email,
        subject=_salad(rng, email.subject),
        text=_salad(rng, email.text),
    )


# ---------------------------------------------------------------------------
# 5. structural noise
# ---------------------------------------------------------------------------

BROKEN_HTML_FRAGMENTS: Tuple[str, ...] = (
    "<div><span>unclosed tag",
    "<table><tr><td>row without closing tags",
    "<b><i>mismatched nesting</b></i>",
    "<p>stray angle bracket < in text</p>",
    '<img src="missing.png">',
)

SIGNATURE_TEMPLATES: Tuple[str, ...] = (
    "\n--\nSent from my iPhone",
    "\n--\nGet Outlook for Android",
    "\n\nRegards,\n{name}\n{title}",
)

SIGNATURE_TITLES: Tuple[str, ...] = (
    "Account Manager", "Support Lead", "Operations", "Sales",
)


def _op_quote_chain(email: InboundEmail, rng: random.Random) -> InboundEmail:
    quoted_name = _random_name(rng)
    quoted_addr = _random_address(rng)
    header_line = f"On {_fake_date(rng)}, {quoted_name} <{quoted_addr}> wrote:"
    quoted_lines = "\n".join(f"> {line}" for line in (email.text.splitlines() or [""]))
    text = f"{email.text}\n\n{header_line}\n{quoted_lines}\n"
    html = f"{email.html}<blockquote><p>{header_line}</p><p>{email.text}</p></blockquote>"
    return dataclasses.replace(email, text=text, html=html)


def _op_forward_header(email: InboundEmail, rng: random.Random) -> InboundEmail:
    fwd_name = _random_name(rng)
    fwd_addr = _random_address(rng)
    block = (
        "---------- Forwarded message ---------\n"
        f"From: {fwd_name} <{fwd_addr}>\n"
        f"Date: {_fake_date(rng)}\n"
        f"Subject: {email.subject}\n"
        f"To: {email.to}\n\n"
    )
    text = block + email.text
    html = f"<pre>{block}</pre>" + email.html
    return dataclasses.replace(email, text=text, html=html)


def _op_broken_html(email: InboundEmail, rng: random.Random) -> InboundEmail:
    fragment = rng.choice(BROKEN_HTML_FRAGMENTS)
    return dataclasses.replace(email, html=email.html + fragment)


def _op_signature(email: InboundEmail, rng: random.Random) -> InboundEmail:
    name = _random_name(rng)
    title = rng.choice(SIGNATURE_TITLES)
    block = rng.choice(SIGNATURE_TEMPLATES).format(name=name, title=title)
    return dataclasses.replace(
        email, text=email.text + block, html=email.html + f"<p>{block}</p>"
    )


def _op_blank_subject(email: InboundEmail, rng: random.Random) -> InboundEmail:
    return dataclasses.replace(email, subject="")


def _op_collapse_body(email: InboundEmail, rng: random.Random) -> InboundEmail:
    return dataclasses.replace(email, text=" ", html="<html></html>")


def _op_truncate_body(email: InboundEmail, rng: random.Random) -> InboundEmail:
    cut = rng.randint(0, min(20, len(email.text)))
    return dataclasses.replace(email, text=email.text[:cut], html=email.html[:cut])


# (operation, relative weight). The blank/collapse/truncate ops are rare on
# any single call; degenerate-level output relies on structural_noise being
# called several times (see RECIPES) so the odds of hitting one compound.
STRUCTURAL_OPS: Tuple[Tuple[Callable[[InboundEmail, random.Random], InboundEmail], int], ...] = (
    (_op_quote_chain, 3),
    (_op_forward_header, 3),
    (_op_signature, 3),
    (_op_broken_html, 2),
    (_op_truncate_body, 1),
    (_op_blank_subject, 1),
    (_op_collapse_body, 1),
)


def structural_noise(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Apply one randomly-weighted structural operation: a quoted reply
    chain, an inline forwarded-message header block, a broken-HTML
    fragment, a signature block, or (rarely) blanking/truncating a field."""
    ops = [op for op, _ in STRUCTURAL_OPS]
    weights = [w for _, w in STRUCTURAL_OPS]
    op = rng.choices(ops, weights=weights, k=1)[0]
    return op(email, rng)


# ---------------------------------------------------------------------------
# 6. encoding sabotage
# ---------------------------------------------------------------------------

# codec name -> the charset label a real client would declare for it.
CHARSET_LABELS: Dict[str, str] = {
    "utf-8": "UTF-8",
    "latin-1": "ISO-8859-1",
    "cp1252": "windows-1252",
    "shift_jis": "Shift_JIS",
    "euc-kr": "EUC-KR",
}

# (actual codec used to produce the bytes, declared codec named in charsets).
# Always distinct: that mismatch is the entire point of this mutator.
SABOTAGE_CODEC_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("utf-8", "latin-1"),
    ("utf-8", "cp1252"),
    ("utf-8", "shift_jis"),
    ("latin-1", "utf-8"),
    ("cp1252", "utf-8"),
    ("shift_jis", "utf-8"),
)

# Fields this mutator is allowed to touch. Restricted to subject/html: both
# are documented charsets keys (payload.py) and neither carries an address,
# so sabotaging them can never violate the reserved-domain contract. `to`
# and `from` are also charsets keys in principle, but they carry addresses;
# leaving them alone keeps every mutator's "never touch addresses" guarantee
# unconditional rather than dependent on this mutator's field choice.
SABOTAGE_FIELDS: Tuple[str, ...] = ("subject", "html")

# Below 0x80, UTF-8, Latin-1, Windows-1252, and Shift_JIS all agree byte for
# byte, so round-tripping pure-ASCII text through any SABOTAGE_CODEC_PAIRS
# pair is a silent no-op: real corruption only shows up once the content has
# at least one character outside that range. A realistic message plausibly
# contains one (an accented name, a curly quote, ...), so when the target
# field is pure ASCII this mutator splices in one such word first, the same
# way a genuinely accented name or word would appear in real content, rather
# than silently declaring a mismatched charset over text nothing could
# actually mis-decode.
EXTENDED_LATIN_SAMPLES: Tuple[str, ...] = (
    "café", "naïve", "Müller", "façade", "résumé", "jalapeño", "über", "señor",
)


def _ensure_non_ascii(rng: random.Random, text: str) -> str:
    if any(ord(ch) > 127 for ch in text):
        return text
    marker = rng.choice(EXTENDED_LATIN_SAMPLES)
    return f"{text} {marker}" if text else marker


def encoding_sabotage(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Declare one charset in the charsets field while the field's content
    was produced as if encoded in another. Concretely: take the field's
    current text, encode it as `actual_codec`, decode those bytes back as
    `declared_codec` (producing the mojibake a receiver would see if it
    trusted a wrong declaration), store that as the field's new content,
    and set charsets[field] to `declared_codec`'s label. Any consumer that
    trusts the charsets field and tries to "fix" the encoding using it will
    make things worse, not better; a consumer that ignores charsets and just
    reads UTF-8 sees exactly the mojibake baked into the string. Either way
    the true content is unrecoverable from `declared_codec` alone, which is
    the whole point: this is the highest-value mutator in the set because
    real inbound-parse pipelines routinely trust a declared charset that
    does not match reality.
    """
    field = rng.choice(SABOTAGE_FIELDS)
    actual_codec, declared_codec = rng.choice(SABOTAGE_CODEC_PAIRS)

    original = _ensure_non_ascii(rng, getattr(email, field))
    raw = original.encode(actual_codec, errors="replace")
    garbled = raw.decode(declared_codec, errors="replace")

    charsets = dict(email.charsets)
    charsets[field] = CHARSET_LABELS[declared_codec]

    return dataclasses.replace(email, **{field: garbled, "charsets": charsets})


# ---------------------------------------------------------------------------
# Mutator registry and messiness-level recipes
# ---------------------------------------------------------------------------

MutatorFn = Callable[[InboundEmail, random.Random], InboundEmail]

MUTATORS: Dict[str, MutatorFn] = {
    "typo": typo,
    "homoglyph_mojibake": homoglyph_mojibake,
    "script_mixing": script_mixing,
    "word_salad": word_salad,
    "structural_noise": structural_noise,
    "encoding_sabotage": encoding_sabotage,
}

# category name -> tuple of (mutator name, min applications, max applications
# inclusive). The five messiness categories are recipes over MUTATORS, not
# separate code paths: every one of them is built purely by choosing how
# many times to call which of the six functions above.
RECIPES: Dict[str, Tuple[Tuple[str, int, int], ...]] = {
    "clean": (),
    "messy_but_valid": (
        ("typo", 1, 3),
        ("structural_noise", 0, 1),
    ),
    "multilingual_gibberish": (
        ("script_mixing", 1, 2),
        ("homoglyph_mojibake", 1, 2),
    ),
    "structurally_malformed": (
        ("structural_noise", 1, 3),
        ("encoding_sabotage", 0, 1),
        ("word_salad", 0, 1),
    ),
    "degenerate": (
        ("structural_noise", 3, 5),
        ("word_salad", 1, 2),
        ("encoding_sabotage", 0, 1),
    ),
}

# Default category mix. Order matters only for readability; corrupt_email
# passes this straight to random.Random.choices, which normalizes weights
# itself, so it need not sum to 1.0 (though it does here, for clarity).
DEFAULT_MIX: Dict[str, float] = {
    "clean": 0.20,
    "messy_but_valid": 0.40,
    "multilingual_gibberish": 0.20,
    "structurally_malformed": 0.15,
    "degenerate": 0.05,
}


def apply_mutators(
    email: InboundEmail, rng: random.Random, plan: Sequence[Tuple[str, int]]
) -> InboundEmail:
    """Apply a sequence of (mutator_name, times) pairs, in order, `times`
    calls each. Unknown mutator names raise KeyError immediately."""
    for name, times in plan:
        mutator = MUTATORS[name]
        for _ in range(times):
            email = mutator(email, rng)
    return email


def apply_recipe(email: InboundEmail, rng: random.Random, category: str) -> InboundEmail:
    """Draw application counts for `category`'s recipe from `rng` and apply
    them. Raises ValueError for an unknown category."""
    if category not in RECIPES:
        raise ValueError(
            f"unknown messiness category: {category!r}; choose from {sorted(RECIPES)}"
        )
    plan: List[Tuple[str, int]] = []
    for name, low, high in RECIPES[category]:
        times = rng.randint(low, high)
        if times:
            plan.append((name, times))
    return apply_mutators(email, rng, plan)


def choose_category(rng: random.Random, mix: Dict[str, float] = None) -> str:
    """Pick one messiness category, weighted by `mix` (default DEFAULT_MIX).
    `mix` need not cover every category in RECIPES and need not sum to 1;
    random.Random.choices normalizes whatever weights it is given."""
    active_mix = mix if mix is not None else DEFAULT_MIX
    names = list(active_mix.keys())
    weights = list(active_mix.values())
    return rng.choices(names, weights=weights, k=1)[0]


def corrupt_email(
    email: InboundEmail, rng: random.Random, mix: Dict[str, float] = None
) -> Tuple[InboundEmail, str]:
    """Pick a messiness category per `mix` and apply its recipe. Returns
    `(corrupted_email, category_name)` so callers can track the actual
    category distribution produced over a corpus."""
    category = choose_category(rng, mix)
    return apply_recipe(email, rng, category), category


def corrupt_corpus(
    corpus: Sequence[InboundEmail], seed: int, mix: Dict[str, float] = None
) -> List[Tuple[InboundEmail, str]]:
    """Apply corrupt_email to every InboundEmail in `corpus`, in order, all
    draws coming from a single `random.Random(seed)`. Deterministic given a
    deterministic `corpus` (e.g. from blast.generate.generate_corpus) and a
    fixed seed and mix: the same inputs always yield the same list of
    (email, category) pairs.
    """
    rng = random.Random(seed)
    return [corrupt_email(email, rng, mix) for email in corpus]
