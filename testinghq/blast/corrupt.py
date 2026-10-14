"""Composable seeded mutators: Blast's chaos layer.

Each mutator is a pure function `(InboundEmail, random.Random) -> InboundEmail`.
It returns a new instance through `InboundEmail.with_changes`, so the Subject,
To and From headers follow the fields, and every choice comes from the `rng`
argument, so the same input and rng state give the same output.

The six mutators:
    typo               character-level typo and transposition noise
    homoglyph_mojibake look-alike substitution and wrong-decode garbling
    script_mixing      foreign-script words spliced into the message
    word_salad         content words replaced with nonsense
    structural_noise   Gmail, Outlook and Apple Mail quote and forward
                       blocks, broken HTML, signatures, and (rarely)
                       blanked or truncated fields
    encoding_sabotage  real cp1252, latin-1 or Shift_JIS bytes on the wire
                       under a charsets entry that says UTF-8

The messiness categories (clean, messy-but-valid, multilingual/gibberish,
structurally malformed, degenerate) are recipes over these mutators: see
RECIPES and corrupt_corpus.

Reserved-domain contract: no mutator touches `to`, `from_addr` or `envelope`.
Where a mutator mentions an address (a quoted reply header), it draws one from
the reserved pool in blast/generate.py.
"""
from __future__ import annotations

import datetime
import random
import re
import string
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .generate import (
    FIRST_NAMES,
    LAST_NAMES,
    MONTH_NAMES,
    MONTHS,
    RESERVED_DOMAINS,
    WEEKDAY_NAMES,
    WEEKDAYS,
)
from .payload import InboundEmail, encoded_word, set_header

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _random_address(rng: random.Random) -> str:
    """A synthetic address on a reserved domain, for text that mentions one."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return f"{first}.{last}{rng.randint(0, 999)}@{rng.choice(RESERVED_DOMAINS)}"


def _random_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES).capitalize()} {rng.choice(LAST_NAMES).capitalize()}"


def _fake_date(rng: random.Random) -> str:
    return f"{rng.choice(MONTHS)} {rng.randint(1, 28)}, {rng.randint(2022, 2025)}"


def _quote_moment(rng: random.Random) -> datetime.datetime:
    return datetime.datetime(
        rng.randint(2022, 2025), rng.randint(1, 12), rng.randint(1, 28),
        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
    )


def _clock12(when: datetime.datetime) -> str:
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def gmail_attribution(when: datetime.datetime, name: str, address: str) -> str:
    """`On Tue, Mar 5, 2024 at 10:12 AM Name <addr> wrote:`"""
    return (
        f"On {WEEKDAYS[when.weekday()]}, {MONTHS[when.month - 1]} {when.day}, "
        f"{when.year} at {_clock12(when)} {name} <{address}> wrote:"
    )


def outlook_sent(when: datetime.datetime) -> str:
    """`Tuesday, March 5, 2024 10:12 AM`"""
    return (
        f"{WEEKDAY_NAMES[when.weekday()]}, {MONTH_NAMES[when.month - 1]} "
        f"{when.day}, {when.year} {_clock12(when)}"
    )


def outlook_block(
    rule: str, name: str, address: str, when: datetime.datetime, to: str, subject: str
) -> str:
    """An Outlook header block. `rule` is "-----Original Message-----" for
    older clients or a line of underscores for current ones."""
    return (
        f"{rule}\nFrom: {name} <{address}>\nSent: {outlook_sent(when)}\n"
        f"To: {to}\nSubject: {subject}\n"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def append_html(html: str, fragment: str) -> str:
    """Insert `fragment` before `</body>` when there is one, else append."""
    index = html.rfind("</body>")
    if index == -1:
        return html + fragment
    return html[:index] + fragment + html[index:]


def prepend_html(html: str, fragment: str) -> str:
    """Insert `fragment` just after `<body ...>` when there is one, else prepend."""
    match = re.search(r"<body[^>]*>", html)
    if not match:
        return fragment + html
    return html[:match.end()] + fragment + html[match.end():]


def _quoted(text: str, prefix: str = "> ") -> str:
    return "\n".join(
        (prefix + line) if line else prefix.rstrip() for line in (text.splitlines() or [""])
    )


# Splits text into alternating [word, whitespace-run, word, ...] tokens so
# paragraph breaks survive a mutator that rewrites whole tokens.
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
    subject and body text. html is left alone."""
    return email.with_changes(
        subject=_mangle_typos(rng, email.subject),
        text=_mangle_typos(rng, email.text),
    )


# ---------------------------------------------------------------------------
# 2. homoglyph / mojibake
# ---------------------------------------------------------------------------

# Latin -> visually-similar Cyrillic homoglyph, lower-case keys.
HOMOGLYPH_MAP: Dict[str, str] = {
    "a": "а",
    "e": "е",
    "o": "о",
    "p": "р",
    "c": "с",
    "x": "х",
    "y": "у",
    "i": "і",
}

# (actual encoding, decoded-as encoding) pairs for a wrong-decode round trip.
MOJIBAKE_CODEC_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("utf-8", "latin-1"),
    ("utf-8", "cp1252"),
    ("latin-1", "utf-8"),
)


def _apply_homoglyphs(rng: random.Random, text: str, rate: float = 0.2) -> str:
    chars = list(text)
    for i, ch in enumerate(chars):
        replacement = HOMOGLYPH_MAP.get(ch.lower())
        if replacement and rng.random() < rate:
            chars[i] = replacement.upper() if ch.isupper() else replacement
    return "".join(chars)


def _mojibake_round_trip(text: str, actual_codec: str, declared_codec: str) -> str:
    raw = text.encode(actual_codec, errors="replace")
    return raw.decode(declared_codec, errors="replace")


def homoglyph_mojibake(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Substitute look-alike characters and/or run a wrong-decode round trip
    on subject/text/html. Content that already arrived garbled, sent as valid
    UTF-8; charsets is not touched."""
    mode = rng.choice(("homoglyph", "mojibake", "both"))
    subject, text, html = email.subject, email.text, email.html

    if mode in ("homoglyph", "both"):
        subject = _apply_homoglyphs(rng, subject)
        text = _apply_homoglyphs(rng, text)

    if mode in ("mojibake", "both"):
        actual_codec, declared_codec = rng.choice(MOJIBAKE_CODEC_PAIRS)
        text = _mojibake_round_trip(text, actual_codec, declared_codec)
        html = _mojibake_round_trip(html, actual_codec, declared_codec)

    return email.with_changes(subject=subject, text=text, html=html)


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
    """Splice a few words from another script into subject/text/html."""
    pool = rng.choice(SCRIPT_WORD_POOLS)
    k = min(len(pool), rng.randint(1, 2))
    snippet = " ".join(rng.sample(pool, k=k))

    text = f"{email.text}\n\n{snippet}"
    html = append_html(email.html, f"<p>{snippet}</p>")
    subject = f"{email.subject} {rng.choice(pool)}" if rng.random() < 0.5 else email.subject

    return email.with_changes(subject=subject, text=text, html=html)


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
    tokens = _WHITESPACE_RUN.split(text)
    return "".join(
        token if token == "" or token.isspace() else _salad_word(rng, token)
        for token in tokens
    )


def word_salad(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Replace content words with nonsense syllables in subject and text.
    html is left alone so the message keeps a readable structure."""
    return email.with_changes(
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

GMAIL_QUOTE_STYLE = "margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex"
OUTLOOK_RULE_STYLE = "border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in"


def _text_html(text: str) -> str:
    return _escape(text).replace("\n", "<br>")


def _op_quote_chain(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """A Gmail reply quote below the message."""
    quoted_name = _random_name(rng)
    quoted_addr = _random_address(rng)
    header_line = f"On {_fake_date(rng)}, {quoted_name} <{quoted_addr}> wrote:"
    text = f"{email.text}\n\n{header_line}\n{_quoted(email.text)}\n"
    html = append_html(
        email.html,
        f'<div class="gmail_quote"><div class="gmail_attr">{_escape(header_line)}<br></div>'
        f'<blockquote class="gmail_quote" style="{GMAIL_QUOTE_STYLE}">'
        f"{_text_html(email.text)}</blockquote></div>",
    )
    return email.with_changes(text=text, html=html)


def _op_forward_header(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """A Gmail forwarded-message block above the message."""
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
    html = prepend_html(email.html, f"<pre>{_escape(block)}</pre>")
    return email.with_changes(text=text, html=html)


def _op_outlook_quote(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """An Outlook reply: the original below a header block, not quoted."""
    rule = rng.choice(("-----Original Message-----", "_" * 32))
    block = outlook_block(
        rule, _random_name(rng), _random_address(rng), _quote_moment(rng),
        email.to, f"RE: {email.subject}",
    )
    text = f"{email.text}\n\n{block}\n{email.text}"
    html = append_html(
        email.html,
        f'<div style="{OUTLOOK_RULE_STYLE}"><p class="MsoNormal">'
        f"{_text_html(block)}</p></div><p class=\"MsoNormal\">{_text_html(email.text)}</p>",
    )
    return email.with_changes(text=text, html=html)


def _op_outlook_forward(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """An Outlook forward: `FW:` header block above the forwarded text."""
    block = outlook_block(
        "_" * 32, _random_name(rng), _random_address(rng), _quote_moment(rng),
        email.to, f"FW: {email.subject}",
    )
    text = f"{block}\n{email.text}"
    html = prepend_html(
        email.html,
        f'<div style="{OUTLOOK_RULE_STYLE}"><p class="MsoNormal">{_text_html(block)}</p></div>',
    )
    return email.with_changes(text=text, html=html)


def _op_apple_quote(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """An Apple Mail reply: `On Mar 5, 2024, at 10:12 AM, Name <addr> wrote:`."""
    when = _quote_moment(rng)
    line = (
        f"On {MONTHS[when.month - 1]} {when.day}, {when.year}, at {_clock12(when)}, "
        f"{_random_name(rng)} <{_random_address(rng)}> wrote:"
    )
    text = f"{email.text}\n\n{line}\n\n{_quoted(email.text)}\n"
    html = append_html(
        email.html,
        f'<div><br><blockquote type="cite"><div>{_escape(line)}</div><br>'
        f"<div>{_text_html(email.text)}</div></blockquote></div>",
    )
    return email.with_changes(text=text, html=html)


def _op_apple_forward(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """An Apple Mail forward: `Begin forwarded message:` above the text."""
    when = _quote_moment(rng)
    block = (
        "Begin forwarded message:\n\n"
        f"From: {_random_name(rng)} <{_random_address(rng)}>\n"
        f"Subject: {email.subject}\n"
        f"Date: {MONTH_NAMES[when.month - 1]} {when.day}, {when.year} at "
        f"{_clock12(when)} GMT\n"
        f"To: {email.to}\n\n"
    )
    text = block + email.text
    html = prepend_html(
        email.html, f'<blockquote type="cite"><div>{_text_html(block)}</div></blockquote>'
    )
    return email.with_changes(text=text, html=html)


def _op_broken_html(email: InboundEmail, rng: random.Random) -> InboundEmail:
    return email.with_changes(html=append_html(email.html, rng.choice(BROKEN_HTML_FRAGMENTS)))


def _op_signature(email: InboundEmail, rng: random.Random) -> InboundEmail:
    name = _random_name(rng)
    title = rng.choice(SIGNATURE_TITLES)
    block = rng.choice(SIGNATURE_TEMPLATES).format(name=name, title=title)
    return email.with_changes(
        text=email.text + block, html=append_html(email.html, f"<p>{_text_html(block)}</p>")
    )


def _op_blank_subject(email: InboundEmail, rng: random.Random) -> InboundEmail:
    return email.with_changes(subject="")


def _op_collapse_body(email: InboundEmail, rng: random.Random) -> InboundEmail:
    return email.with_changes(text=" ", html="<html></html>")


def _op_truncate_body(email: InboundEmail, rng: random.Random) -> InboundEmail:
    cut = rng.randint(0, min(20, len(email.text)))
    return email.with_changes(text=email.text[:cut], html=email.html[:cut])


# (operation, relative weight). The blank/collapse/truncate ops are rare on
# any single call; degenerate output calls structural_noise several times.
STRUCTURAL_OPS: Tuple[Tuple[Callable[[InboundEmail, random.Random], InboundEmail], int], ...] = (
    (_op_quote_chain, 3),
    (_op_forward_header, 2),
    (_op_outlook_quote, 2),
    (_op_outlook_forward, 1),
    (_op_apple_quote, 2),
    (_op_apple_forward, 1),
    (_op_signature, 3),
    (_op_broken_html, 2),
    (_op_truncate_body, 1),
    (_op_blank_subject, 1),
    (_op_collapse_body, 1),
)


def structural_noise(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Apply one weighted structural operation: a Gmail, Outlook or Apple Mail
    quote or forward block, a broken-HTML fragment, a signature, or (rarely)
    blanking or truncating a field."""
    ops = [op for op, _ in STRUCTURAL_OPS]
    weights = [w for _, w in STRUCTURAL_OPS]
    op = rng.choices(ops, weights=weights, k=1)[0]
    return op(email, rng)


# ---------------------------------------------------------------------------
# 6. encoding sabotage
# ---------------------------------------------------------------------------

# codec name -> the charset label a client would declare for it.
CHARSET_LABELS: Dict[str, str] = {
    "utf-8": "UTF-8",
    "latin-1": "ISO-8859-1",
    "cp1252": "windows-1252",
    "shift_jis": "Shift_JIS",
    "euc-kr": "EUC-KR",
}

# (codec the bytes are really in, codec the charsets entry claims). The bytes
# go on the wire as they are, so every pair is a real mismatch a receiver
# trusting `charsets` will mis-decode.
SABOTAGE_CODEC_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("latin-1", "utf-8"),
    ("cp1252", "utf-8"),
    ("shift_jis", "utf-8"),
)

# Fields this mutator may touch. None of them carries an address.
SABOTAGE_FIELDS: Tuple[str, ...] = ("subject", "text", "html")

# Below 0x80 every codec above agrees with UTF-8, so pure-ASCII content cannot
# be mis-decoded. A field without a character the actual codec encodes above
# 0x7F gets one spliced in, the way an accented name or a curly quote appears.
NON_ASCII_SAMPLES: Dict[str, Tuple[str, ...]] = {
    "latin-1": ("café", "naïve", "Müller", "façade", "résumé", "jalapeño", "über", "señor"),
    "cp1252": ("“quoted”", "€40", "café", "Müller", "it’s", "résumé"),
    "shift_jis": ("お問い合わせ", "請求書", "ありがとうございます", "注文番号"),
}

#: Kept for callers that imported the old name.
EXTENDED_LATIN_SAMPLES = NON_ASCII_SAMPLES["latin-1"]


def _has_high_bytes(text: str, codec: str) -> bool:
    return any(b > 0x7F for b in text.encode(codec, errors="ignore"))


def _ensure_high_bytes(rng: random.Random, text: str, codec: str) -> str:
    if _has_high_bytes(text, codec):
        return text
    marker = rng.choice(NON_ASCII_SAMPLES[codec])
    if text.endswith("</body></html>"):
        return append_html(text, f"<p>{marker}</p>")
    return f"{text} {marker}" if text else marker


def sabotage_field(
    email: InboundEmail, rng: random.Random, field: Optional[str] = None
) -> InboundEmail:
    """Encoding sabotage on one field (`field`, or one drawn from `rng`)."""
    if field is None:
        field = rng.choice(SABOTAGE_FIELDS)
    actual_codec, declared_codec = rng.choice(SABOTAGE_CODEC_PAIRS)
    label = CHARSET_LABELS[declared_codec]

    original = _ensure_high_bytes(rng, getattr(email, field), actual_codec)
    raw = original.encode(actual_codec, errors="replace")
    seen = raw.decode(declared_codec, errors="replace")

    charsets = dict(email.charsets)
    charsets[field] = label
    raw_fields = dict(email.raw_fields)
    raw_fields[field] = raw
    changes = {field: seen, "charsets": charsets, "raw_fields": raw_fields}
    if field == "subject":
        headers = dict(email.headers)
        set_header(headers, "Subject", encoded_word(raw, label))
        changes["headers"] = headers
    return email.with_changes(**changes)


def encoding_sabotage(email: InboundEmail, rng: random.Random) -> InboundEmail:
    """Send one of subject, text or html as real latin-1, cp1252 or Shift_JIS
    bytes while `charsets` declares UTF-8.

    The bytes go in `raw_fields`, so they reach the wire unchanged. The str
    field holds what a receiver that trusted the declaration would decode,
    and a sabotaged subject's header is an encoded word with the same lying
    label over the same bytes. A pipeline that trusts `charsets` stores
    replacement characters; one that sniffs the bytes recovers the text.
    """
    return sabotage_field(email, rng)


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

# category -> (mutator name, min applications, max applications inclusive).
# encoding_sabotage runs last, so no later mutator rewrites the raw bytes it
# put on the wire.
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
        ("word_salad", 0, 1),
        ("encoding_sabotage", 0, 1),
    ),
    "degenerate": (
        ("structural_noise", 3, 5),
        ("word_salad", 1, 2),
        ("encoding_sabotage", 0, 1),
    ),
}

# Default category mix, as relative weights.
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
    """Apply a sequence of (mutator_name, times) pairs, in order. Unknown
    mutator names raise KeyError."""
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


def choose_category(rng: random.Random, mix: Optional[Dict[str, float]] = None) -> str:
    """Pick one category, weighted by `mix` (default DEFAULT_MIX)."""
    active_mix = mix if mix is not None else DEFAULT_MIX
    names = list(active_mix.keys())
    weights = list(active_mix.values())
    return rng.choices(names, weights=weights, k=1)[0]


def corrupt_email(
    email: InboundEmail, rng: random.Random, mix: Optional[Dict[str, float]] = None
) -> Tuple[InboundEmail, str]:
    """Pick a category per `mix` and apply its recipe. Returns
    `(corrupted_email, category_name)`."""
    category = choose_category(rng, mix)
    return apply_recipe(email, rng, category), category


def corrupt_corpus(
    corpus: Sequence[InboundEmail], seed: int, mix: Optional[Dict[str, float]] = None
) -> List[Tuple[InboundEmail, str]]:
    """Apply corrupt_email to every email in `corpus`, in order, with all
    draws from one `random.Random(seed)`. Deterministic for a fixed corpus,
    seed and mix."""
    rng = random.Random(seed)
    return [corrupt_email(email, rng, mix) for email in corpus]
