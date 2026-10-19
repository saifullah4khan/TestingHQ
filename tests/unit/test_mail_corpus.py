"""`blast --corpus`: real mail files read, rewritten, tagged, never leaked.

The sample corpus in tests/fixtures/corpus is written by hand. Its addresses
are on made-up but ordinary-looking domains, none of them reserved, so every
test here exercises the rewrite rather than passing because the input was
already synthetic.
"""
from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from testinghq.blast import mailcorpus
from testinghq.blast.corpus import CorpusError
from testinghq.core import guardrails
from testinghq.core.formats import format_names, get_format
from testinghq.core.report import payload_sha256
from testinghq.pipeline import messages

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"

#: Every domain the sample corpus uses. None is reserved.
REAL_DOMAINS = (
    "lindenbrook-freight.com", "orchardlane-supplies.com", "kestrelpoint.co.uk",
    "bluewhistle-studio.io", "mapleridge-insurance.com", "vandermeer-hydraulik.de",
    "petitpont-atelier.fr", "harbourview-clinic.ca", "tidewater-logistics.org",
    "sunvale-telecom.net",
)

MAIL_FILES = [
    "005-raw-utf8-headers.box",
    "007-forwarded.box",
    "inbox/001-plain-ascii.eml",
    "inbox/002-alternative-cc-reply-to.eml",
    "inbox/003-attachments.eml",
    "inbox/004-encoded-headers.box",
    "inbox/006-reply-thread.eml",
    "nested/008-html-only.eml",
    "nested/009-degenerate.box",
]


def _plan(**kwargs):
    kwargs.setdefault("tag_prefix", "hq-test")
    return mailcorpus.MailCorpusPlan(str(CORPUS), **kwargs)


def _entry(plan, source):
    return next(entry for entry in plan.entries if entry.source == source)


def _everything_readable(body: bytes) -> str:
    """The body as raw text, plus, when it parses as a message, every header
    and every part decoded, embedded messages included. A leak hidden by
    base64 or quoted-printable shows up here."""
    chunks = [body.decode("latin-1")]
    message = message_from_bytes(body, policy=policy.default)
    for part in message.walk():
        chunks.extend(str(value) for _name, value in part.items())
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            chunks.append(payload.decode("utf-8", "replace"))
            if part.get_content_type() == "message/rfc822":
                chunks.append(_everything_readable(payload))
    return "\n".join(chunks)


def _leaks(text: str):
    lowered = text.lower()
    found = [domain for domain in REAL_DOMAINS if "@" + domain in lowered]
    if "@[203.0.113.9]" in lowered:
        found.append("@[203.0.113.9]")
    return found


# ---------------------------------------------------------------------------
# The directory
# ---------------------------------------------------------------------------


def test_every_mail_file_is_read_in_name_order_and_the_rest_counted():
    plan = _plan()
    assert [entry.source for entry in plan.entries] == MAIL_FILES
    assert [entry.index for entry in plan.entries] == list(range(len(MAIL_FILES)))
    assert plan.ignored == 1


def test_count_caps_the_files_read():
    plan = _plan(count=3)
    assert [entry.source for entry in plan.entries] == MAIL_FILES[:3]
    assert plan.config()["count"] == 3
    assert plan.ignored == 1


def test_the_extension_match_ignores_case(tmp_path):
    raw = (CORPUS / "inbox" / "001-plain-ascii.eml").read_bytes()
    (tmp_path / "A.EML").write_bytes(raw)
    (tmp_path / "b.Box").write_bytes(raw)
    (tmp_path / "c.msg").write_bytes(raw)
    plan = mailcorpus.MailCorpusPlan(str(tmp_path), tag_prefix="hq-test")
    assert [entry.source for entry in plan.entries] == ["A.EML", "b.Box"]
    assert plan.ignored == 1


@pytest.mark.parametrize("count", [0, -1])
def test_a_count_that_selects_nothing_is_refused(count):
    with pytest.raises(CorpusError, match="--count"):
        _plan(count=count)


def test_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(CorpusError, match="not a directory"):
        mailcorpus.MailCorpusPlan(str(tmp_path / "absent"))


def test_a_directory_with_no_mail_is_refused(tmp_path):
    (tmp_path / "readme.txt").write_text("not mail", encoding="utf-8")
    with pytest.raises(CorpusError, match=r"no \.eml or \.box files \(1 other"):
        mailcorpus.MailCorpusPlan(str(tmp_path))


def test_the_config_records_what_rebuilds_the_plan():
    plan = _plan(seed=7)
    config = plan.config()
    assert config == {
        "corpus_dir": str(CORPUS), "corpus_ignored": 1, "count": len(MAIL_FILES),
        "seed": 7, "tag_prefix": "hq-test",
    }
    rebuilt = mailcorpus.MailCorpusPlan.from_config(7, config)
    assert [payload_sha256(e) for e, _ in rebuilt.build()] == [
        payload_sha256(e) for e, _ in plan.build()
    ]


# ---------------------------------------------------------------------------
# Unparseable files are entries, never exceptions
# ---------------------------------------------------------------------------


def test_a_file_with_no_header_is_unparseable_with_its_reason():
    entry = _entry(_plan(), "nested/009-degenerate.box")
    assert entry.email is None
    assert "no header fields" in entry.error


def test_an_empty_file_is_unparseable(tmp_path):
    (tmp_path / "empty.eml").write_bytes(b"")
    (tmp_path / "blank.box").write_bytes(b"\r\n\r\n  \n")
    plan = mailcorpus.MailCorpusPlan(str(tmp_path))
    assert [(e.source, e.error) for e in plan.entries] == [
        ("blank.box", "the file is empty"), ("empty.eml", "the file is empty"),
    ]
    assert plan.build() == []


def test_a_parser_failure_is_recorded_not_raised(monkeypatch):
    def explode(data):
        raise LookupError("unknown encoding: x-made-up")

    monkeypatch.setattr(mailcorpus, "parse_message", explode)
    entry = mailcorpus.load_entry(0, "x.eml", b"From: a@b.test\n\nhi\n", 0, "hq")
    assert entry.email is None
    assert entry.error == "LookupError: unknown encoding: x-made-up"


def test_an_unreadable_file_is_recorded_not_raised(tmp_path, monkeypatch):
    (tmp_path / "locked.eml").write_bytes(b"From: a@b.test\n\nhi\n")
    real = Path.read_bytes

    def read_bytes(self):
        if self.name == "locked.eml":
            raise PermissionError("permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    entry = mailcorpus.MailCorpusPlan(str(tmp_path)).entries[0]
    assert entry.email is None and "could not read" in entry.error


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def test_an_address_maps_to_one_synthetic_address_whatever_its_case():
    first = mailcorpus.rewrite_address("Marta.Kowalczyk@Lindenbrook-Freight.com")
    assert first == mailcorpus.rewrite_address("marta.kowalczyk@lindenbrook-freight.com")
    assert first.startswith("marta-kowalczyk-")
    assert first.endswith("@corpus.example.test")
    assert guardrails.is_synthetic_address(first)
    assert first != mailcorpus.rewrite_address("marta.kowalczyk@lindenbrook-freight.de")


def test_an_already_synthetic_address_is_left_alone():
    assert mailcorpus.rewrite_address("desk@helpdesk.test") == "desk@helpdesk.test"


@pytest.mark.parametrize("text", [
    "write to jane@kestrelpoint.co.uk today",
    "<a href=\"mailto:jane@kestrelpoint.co.uk?subject=x\">",
    "'jane@kestrelpoint.co.uk'",
    '"ops desk"@kestrelpoint.co.uk',
    "noc@[203.0.113.9]",
    "élodie@petitpont-atelier.fr",
    "root@mailhost",
])
def test_anything_shaped_like_an_address_is_rewritten(text):
    rewritten = mailcorpus.rewrite_text(text)
    assert mailcorpus.unsafe_addresses(rewritten) == []
    assert "kestrelpoint" not in rewritten and "petitpont" not in rewritten
    assert "203.0.113.9" not in rewritten and "mailhost" not in rewritten


def test_prose_around_an_address_survives_the_rewrite():
    rewritten = mailcorpus.rewrite_text("Write to 'jane@kestrelpoint.co.uk'.")
    assert rewritten.startswith("Write to 'jane-")
    assert rewritten.endswith("@corpus.example.test'.")


@pytest.mark.parametrize("format_name", format_names())
def test_no_corpus_address_reaches_the_wire_in_any_format(format_name):
    wire = get_format(format_name, "corpusboundary")
    plan = _plan()
    assert plan.build(), "the sample corpus must produce payloads"
    for email, _label in plan.build():
        body = wire.encode(email).body
        assert _leaks(_everything_readable(body)) == [], format_name


def test_the_sample_corpus_really_does_carry_real_addresses():
    """The leak test above means something only if the input leaks."""
    raw = "".join(
        _everything_readable((CORPUS / name).read_bytes()) for name in MAIL_FILES
    )
    assert sorted(set(_leaks(raw)) - {"@[203.0.113.9]"}) == sorted(REAL_DOMAINS)
    assert "@[203.0.113.9]" in _leaks(raw)


def test_every_corpus_payload_passes_the_send_guardrail():
    for email, _label in _plan().build():
        guardrails.require_synthetic_content(mailcorpus.address_fields(email))


def test_every_address_header_is_rebuilt_on_the_synthetic_domain():
    email = _entry(_plan(), "inbox/002-alternative-cc-reply-to.eml").email
    for name in ("From", "To", "Cc", "Reply-To", "Sender"):
        value = email.headers[name]
        assert "@corpus.example.test" in value, name
        assert "kestrelpoint" not in value and "bluewhistle" not in value, name
    assert email.headers["Cc"].startswith('"Doe, Jane" <jane-doe-')
    assert email.from_addr.startswith("Tomas Lindqvist <tomas-")


def test_the_envelope_is_rewritten_too():
    email = _entry(_plan(), "inbox/001-plain-ascii.eml").email
    assert email.envelope.from_addr == mailcorpus.rewrite_address(
        "marta.kowalczyk@lindenbrook-freight.com"
    )
    assert email.envelope.to == (mailcorpus.rewrite_address("support@orchardlane-supplies.com"),)
    assert email.headers["Delivered-To"] == email.envelope.to[0]
    # A host name is not an address and stays; the address in `for <...>` goes.
    assert "by mx.orchardlane-supplies.com with ESMTPS" in email.headers["Received"]
    assert "@orchardlane" not in email.headers["Received"]
    assert "for <support-" in email.headers["Received"]


def test_a_group_to_and_a_null_return_path_fall_back_to_cc_and_stay_null():
    email = _entry(_plan(), "nested/008-html-only.eml").email
    assert email.to == ""
    assert email.headers["Return-Path"] == "<>"
    assert email.envelope.from_addr == email.headers["Disposition-Notification-To"]
    assert email.envelope.to == (
        mailcorpus.rewrite_address('"ops desk"@sunvale-telecom.net'),
        mailcorpus.rewrite_address("noc@[203.0.113.9]"),
    )


def test_a_file_with_no_recipient_gets_the_default_one(tmp_path):
    (tmp_path / "x.eml").write_bytes(b"Subject: hello\n\nno addresses at all\n")
    email = mailcorpus.MailCorpusPlan(str(tmp_path)).entries[0].email
    assert email.envelope.to == (mailcorpus.DEFAULT_RECIPIENT,)
    assert email.envelope.from_addr == ""


# ---------------------------------------------------------------------------
# What survives: non-ASCII headers, bodies, attachments
# ---------------------------------------------------------------------------


def test_encoded_word_headers_are_decoded_and_re_encoded():
    email = _entry(_plan(), "inbox/004-encoded-headers.box").email
    assert email.from_addr.startswith("Jürgen Müller <juergen-mueller-")
    assert email.subject.startswith("Rückfrage zur Rechnung Nr. 5521")
    assert email.headers["Subject"].isascii() and email.headers["From"].isascii()
    assert "Bitte prüfen Sie das." in email.text


def test_raw_utf8_headers_are_read_as_utf8():
    email = _entry(_plan(), "005-raw-utf8-headers.box").email
    assert email.from_addr.startswith("Élodie Château <elodie-chateau-")
    assert email.subject.startswith("Commande 3390 : colis abîmé à la livraison")
    assert email.headers["Sender"].isascii()
    assert email.headers["Return-Path"].startswith("<bounce-77-")


def test_a_multipart_message_keeps_both_bodies_and_its_attachments():
    email = _entry(_plan(), "inbox/003-attachments.eml").email
    assert email.text.startswith("Please find the signed claim form attached")
    pdf, csv = email.attachments
    assert (pdf.filename, pdf.content_type) == ("claim-4471.pdf", "application/pdf")
    assert pdf.content.startswith(b"%PDF-1.4")
    assert (csv.filename, csv.content_type) == ("contacts.csv", "text/csv")
    assert b"Anna Berg,anna-berg-" in csv.content
    assert b"mapleridge" not in csv.content

    alternative = _entry(_plan(), "inbox/002-alternative-cc-reply-to.eml").email
    assert "corrected invoice to accounts-" in alternative.text
    assert 'href="mailto:accounts-' in alternative.html


def test_a_forwarded_message_travels_as_an_attachment_with_its_addresses_rewritten():
    email = _entry(_plan(), "007-forwarded.box").email
    (forwarded,) = email.attachments
    assert forwarded.content_type == "message/rfc822"
    assert forwarded.filename == "Shipment 2207 delayed.eml"
    assert b"Subject: Shipment 2207 delayed" in forwarded.content
    assert b"tidewater" not in forwarded.content and b"bluewhistle" not in forwarded.content


def test_threading_headers_are_kept_with_their_ids_rewritten():
    email = _entry(_plan(), "inbox/006-reply-thread.eml").email
    assert email.headers["In-Reply-To"].startswith("<ticket-6610-3-")
    assert email.headers["References"].count("@corpus.example.test") == 2
    assert "'orders-" in email.text


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


def test_each_payload_is_tagged_like_the_pipeline_tags_its_own():
    plan = _plan(seed=3)
    for entry in plan.entries:
        if entry.email is None:
            continue
        tag = messages.make_tag("hq-test", 3, entry.index)
        marker = messages.tag_marker(tag)
        email = entry.email
        assert email.headers[messages.TAG_HEADER] == tag
        assert email.headers["Message-ID"] == f"<{tag}@hq.example.test>"
        assert email.subject.endswith(f" {marker}")
        assert email.ground_truth.subject == email.subject
        assert email.text.rstrip().endswith(marker)
        assert email.html.endswith(f"<p>{marker}</p>")
        assert messages.find_tag(email.subject) == tag


def test_the_original_message_id_and_any_old_tag_are_replaced(tmp_path):
    (tmp_path / "x.eml").write_bytes(
        b"From: a@kestrelpoint.co.uk\nTo: b@kestrelpoint.co.uk\n"
        b"Message-Id: <orig@kestrelpoint.co.uk>\nX-TestingHQ-Tag: stale\n"
        b"Subject: s\n\nbody\n"
    )
    email = mailcorpus.MailCorpusPlan(str(tmp_path), tag_prefix="hq-x").entries[0].email
    names = [name.casefold() for name in email.headers]
    assert names.count("message-id") == 1 and names.count("x-testinghq-tag") == 1
    assert email.headers[messages.TAG_HEADER] == "hq-x-0-0000"


def test_a_corpus_build_is_deterministic():
    first = [payload_sha256(e) for e, _ in _plan(seed=5).build()]
    second = [payload_sha256(e) for e, _ in _plan(seed=5).build()]
    other = [payload_sha256(e) for e, _ in _plan(seed=5, tag_prefix="hq-other").build()]
    assert first == second
    assert all(a != b for a, b in zip(first, other))
