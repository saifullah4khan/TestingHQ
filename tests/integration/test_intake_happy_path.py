"""Happy-path integration test: clean InboundEmail payloads, dry-run posted
to the fake sink, decoded fields checked against per-payload ground truth.

Scope note: this file hand-builds a small fixed set of clean InboundEmail
payloads rather than generating them. It was written when
testinghq/blast/generate.py did not exist, and its own note said what should
happen once the generator landed: drive the same assertions from a seeded
corpus. That is now done, in test_corpus_generation.py, which also covers the
mutators and the category mix.

This file is kept rather than deleted, because it is worth having both: the
hand-built payloads are hand-picked to be exactly the shapes an
implementation is most likely to get wrong (an attachment, a payload with
none, a long unicode body), and reading them is easier than reverse
engineering a seed. But note that its assertions are weaker than they look,
and test_corpus_generation.py explains why in detail: the hand-built list used
bare addresses on both sides and set ground_truth.body_core equal to text,
whereas the real generator puts an RFC 5322 display name in the header and
only the substantive sentence in body_core. So the two equalities below held
only for a shape the generator never emits, and had never been exercised
against real generated data. They are kept because they still check the round
trip faithfully for a payload shaped this way; they are not evidence about
the generator.

Hermetic: no network. "Dry-run" here means what it means throughout Blast:
the payload is built and delivered to a controlled destination (the fake
sink) and nothing goes over a socket.
"""
from fake_sink import FakeSink

from testinghq.blast.payload import Attachment, Envelope, GroundTruth, InboundEmail
from testinghq.blast.serialize import to_multipart_parts


def _clean_corpus():
    """A small fixed set of well-formed InboundEmail payloads standing in
    for a generated clean corpus. Each carries ground truth matching its own
    content exactly, as the clean generator is expected to do."""
    return [
        InboundEmail(
            to="support@example.com",
            from_addr="alice@example.net",
            subject="Order question",
            text="Hi, when will my order ship?",
            html="<p>Hi, when will my order ship?</p>",
            envelope=Envelope(to=("support@example.com",), from_addr="alice@example.net"),
            ground_truth=GroundTruth(
                from_addr="alice@example.net",
                subject="Order question",
                body_core="Hi, when will my order ship?",
            ),
            headers={"Subject": "Order question"},
            charsets={"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "html": "UTF-8"},
        ),
        InboundEmail(
            to="support@example.com",
            from_addr="bob@example.org",
            subject="Refund status",
            text="Following up on my refund request from last week.",
            html="<p>Following up on my refund request from last week.</p>",
            envelope=Envelope(to=("support@example.com",), from_addr="bob@example.org"),
            ground_truth=GroundTruth(
                from_addr="bob@example.org",
                subject="Refund status",
                body_core="Following up on my refund request from last week.",
            ),
            headers={"Subject": "Refund status"},
            charsets={"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "html": "UTF-8"},
        ),
        InboundEmail(
            to="support@example.com",
            from_addr="carol@example.com",
            subject="Invoice attached",
            text="Please find the invoice attached.",
            html="<p>Please find the invoice attached.</p>",
            envelope=Envelope(to=("support@example.com",), from_addr="carol@example.com"),
            ground_truth=GroundTruth(
                from_addr="carol@example.com",
                subject="Invoice attached",
                body_core="Please find the invoice attached.",
            ),
            headers={"Subject": "Invoice attached"},
            charsets={"to": "UTF-8", "from": "UTF-8", "subject": "UTF-8", "html": "UTF-8"},
            attachments=(
                Attachment(
                    filename="invoice.pdf",
                    content_type="application/pdf",
                    content=b"%PDF-1.4 fake invoice bytes",
                ),
            ),
        ),
    ]


def test_every_clean_payload_arrives_at_the_sink():
    corpus = _clean_corpus()
    sink = FakeSink()

    for email in corpus:
        sink.receive_parts(to_multipart_parts(email))

    assert len(sink.received) == len(corpus)


def test_decoded_fields_match_ground_truth_for_every_payload():
    corpus = _clean_corpus()
    sink = FakeSink()

    for email in corpus:
        sink.receive_parts(to_multipart_parts(email))

    for email, response in zip(corpus, sink.received):
        decoded = response.decoded
        assert decoded.from_addr == email.ground_truth.from_addr
        assert decoded.subject == email.ground_truth.subject
        assert decoded.text == email.ground_truth.body_core
        # Fields beyond ground truth should also survive the round trip
        # intact, since Blast needs the full payload recorded, not just the
        # three ground-truth fields.
        assert decoded.to == email.to
        assert decoded.html == email.html
        assert decoded.envelope == {
            "to": list(email.envelope.to),
            "from": email.envelope.from_addr,
        }


def test_attachment_survives_the_round_trip():
    corpus = _clean_corpus()
    email_with_attachment = corpus[2]
    sink = FakeSink()

    response = sink.receive_parts(to_multipart_parts(email_with_attachment))

    assert response.decoded.attachment_count == 1
    decoded_attachment = response.decoded.attachments[0]
    original_attachment = email_with_attachment.attachments[0]
    assert decoded_attachment.filename == original_attachment.filename
    assert decoded_attachment.content_type == original_attachment.content_type
    assert decoded_attachment.content == original_attachment.content


def test_payloads_without_attachments_decode_with_zero_count():
    corpus = _clean_corpus()
    sink = FakeSink()

    response = sink.receive_parts(to_multipart_parts(corpus[0]))

    assert response.decoded.attachment_count == 0
    assert response.decoded.attachments == ()


def test_fake_sink_module_has_no_networking_imports():
    """Readable guarantee for future readers extending this file: the fake
    sink stays in-process. If someone later adds a real socket/HTTP import
    to fake_sink.py, this test fails loudly instead of tests quietly
    becoming non-hermetic."""
    import fake_sink

    with open(fake_sink.__file__) as handle:
        lines = handle.readlines()
    import_lines = [line for line in lines if line.startswith(("import ", "from "))]
    forbidden_modules = ("socket", "urllib", "http.client", "http.server", "requests")
    for line in import_lines:
        for forbidden in forbidden_modules:
            assert forbidden not in line, f"networking import found: {line!r}"
