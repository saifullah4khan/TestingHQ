"""sigcheck: the signing schemes and the four variants a check must reject.

THE TRAP THESE TESTS AVOID. A test that computes an expected digest with the
same function it is testing proves the function is consistent with itself. So
every expected value here is computed along an independent path from the same
written specification: an HMAC over a byte string assembled in the test, with
the header names and the concatenation order spelled out rather than imported.
That catches the two things most likely to be wrong, which are the string being
signed and the encoding of the result, and it would not catch a function that
reversed the timestamp and the body.

No published provider vector is pinned, because pinning one means copying a key
out of provider documentation. The property that matters is that the signature
covers exactly the bytes that were sent, and that is asserted directly: a
one-byte change to the body invalidates the signature.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from testinghq.pipeline import sigcheck as tool

SECRET = "test-signing-key"
WRONG = "not-the-secret"
BODY = b'{"event":"processed","email":"alice@example.com","sg_event_id":"abc"}'
TIMESTAMP = 1_700_000_000
NOW = TIMESTAMP + 30
TOLERANCE = 300.0


# ---------------------------------------------------------------------------
# An independent implementation of each scheme's specification
# ---------------------------------------------------------------------------
#
# Written out here rather than imported, deliberately. If it called
# `scheme.sign_headers` the test would be checking the code against itself.


def _expected_mailgun_header(body: bytes, secret: str, timestamp: int) -> str:
    """Mailgun signs `str(timestamp) + body`, hex, as `t=...,v1=...`."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}{body.decode('utf-8')}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _expected_postmark_header(body: bytes, secret: str) -> str:
    """Postmark signs the body alone, base64."""
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")


# ---------------------------------------------------------------------------
# The scheme table
# ---------------------------------------------------------------------------


def test_the_table_has_the_three_named_providers():
    assert tool.known_schemes() == ("mailgun", "postmark", "sendgrid")


def test_an_unknown_scheme_is_refused_rather_than_defaulted():
    """Signing with the wrong algorithm produces a request the provider's real
    receiver would reject, so a default here would report the opposite of the
    truth."""
    with pytest.raises(tool.SigcheckError) as caught:
        tool.get_scheme("carrier-pigeon")
    assert "unknown signature scheme" in str(caught.value)
    assert "mailgun" in str(caught.value)


def test_the_hmac_schemes_take_no_injected_primitives():
    """Their signing string and encoding are the specification, not a choice a
    caller makes per run."""
    with pytest.raises(tool.SigcheckError) as caught:
        tool.get_scheme("postmark", encode=lambda d: "nope")
    assert "cannot be given" in str(caught.value)


# ---------------------------------------------------------------------------
# Mailgun
# ---------------------------------------------------------------------------


def test_mailgun_signs_the_timestamp_then_the_body():
    """The concatenation order is the whole of the scheme, so it is asserted
    against an independently assembled string."""
    scheme = tool.get_scheme("mailgun")
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    assert headers["X-Signature"] == _expected_mailgun_header(BODY, SECRET, TIMESTAMP)
    # Mailgun carries the timestamp inside the signature header, not beside it.
    assert "X-Timestamp" not in headers


def test_mailgun_verifies_its_own_signature():
    scheme = tool.get_scheme("mailgun")
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    result = scheme.verify(BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE)
    assert result.accepted
    assert "valid" in result.reason


def test_mailgun_extracts_the_signature_from_the_compound_header():
    """`X-Signature` is `t=...,v1=...`, so reading the whole value as the
    signature would make every real Mailgun request look invalid."""
    scheme = tool.get_scheme("mailgun")
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    assert "=" in headers["X-Signature"]
    assert scheme.verify(
        BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE
    ).accepted


def test_a_mailgun_header_with_no_v1_part_is_not_accepted():
    scheme = tool.get_scheme("mailgun")
    result = scheme.verify(
        BODY, SECRET, {"X-Signature": f"t={TIMESTAMP}"}, now=NOW, tolerance=TOLERANCE
    )
    assert result.accepted is False
    assert "no recognisable signature" in result.reason


# ---------------------------------------------------------------------------
# Postmark
# ---------------------------------------------------------------------------


def test_postmark_signs_the_body_alone():
    scheme = tool.get_scheme("postmark")
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    assert headers["X-Postmark-Signature"] == _expected_postmark_header(BODY, SECRET)
    assert "X-Timestamp" not in headers, "Postmark has no timestamp header"


def test_postmark_verifies_its_own_signature():
    scheme = tool.get_scheme("postmark")
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    assert scheme.verify(
        BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE
    ).accepted


def test_a_postmark_signature_is_base64_not_hex():
    """The two encodings differ in a way a wrong guess would hide, so it is
    pinned against an independent computation rather than derived from the
    implementation."""
    signature = tool.get_scheme("postmark").sign_headers(BODY, SECRET, TIMESTAMP)[
        "X-Postmark-Signature"
    ]
    assert signature == _expected_postmark_header(BODY, SECRET)
    base64.b64decode(signature, validate=True)
    try:
        bytes.fromhex(signature)
    except ValueError:
        pass
    else:
        raise AssertionError("a base64 HMAC happened to also be valid hex")


# ---------------------------------------------------------------------------
# The property that matters: the signature covers the exact bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mailgun", "postmark"])
def test_a_one_byte_change_to_the_body_invalidates_the_signature(name):
    scheme = tool.get_scheme(name)
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    tampered = BODY.replace(b"alice", b"alicf")
    assert tampered != BODY
    result = scheme.verify(tampered, SECRET, headers, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert "does not match" in result.reason


@pytest.mark.parametrize("name", ["mailgun", "postmark"])
def test_a_different_secret_is_rejected(name):
    scheme = tool.get_scheme(name)
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    result = scheme.verify(BODY, WRONG, headers, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert "does not match" in result.reason


@pytest.mark.parametrize("name", ["mailgun", "postmark"])
def test_a_missing_signature_header_is_rejected(name):
    scheme = tool.get_scheme(name)
    result = scheme.verify(BODY, SECRET, {}, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert "nothing was verified" in result.reason


@pytest.mark.parametrize("name", ["mailgun", "postmark"])
def test_a_malformed_signature_is_rejected_not_raised(name):
    """A junk header is a finding about the endpoint, not an exception in the
    tool: a receiver that crashes on a bad signature is itself a finding."""
    scheme = tool.get_scheme(name)
    result = scheme.verify(
        BODY, SECRET, {scheme.signature_header: "not-a-signature!!"},
        now=NOW, tolerance=TOLERANCE,
    )
    assert result.accepted is False


# ---------------------------------------------------------------------------
# The replay window
# ---------------------------------------------------------------------------


def test_a_stale_timestamp_is_rejected_and_says_so():
    """The reason string has to distinguish this from an invalid signature,
    because they are different findings: one is an open endpoint, the other is
    a replay window that is too wide or a clock that is wrong."""
    scheme = tool.get_scheme("mailgun")
    old = TIMESTAMP - int(TOLERANCE) - 1
    headers = scheme.sign_headers(BODY, SECRET, old)
    result = scheme.verify(BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert result.stale is True
    assert "replayed" in result.reason
    assert "outside the 300s window" in result.reason


def test_a_timestamp_exactly_on_the_window_edge_is_accepted():
    """Inside or outside, not "and then some": an implementation using `>=` would
    reject a request the provider considers fresh, which is a false finding."""
    scheme = tool.get_scheme("mailgun")
    edge = NOW - int(TOLERANCE)
    headers = scheme.sign_headers(BODY, SECRET, edge)
    assert scheme.verify(
        BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE
    ).accepted


def test_a_timestamp_in_the_future_is_also_stale():
    """A clock running fast on the receiver must not become a way to replay."""
    scheme = tool.get_scheme("mailgun")
    ahead = NOW + int(TOLERANCE) + 1
    headers = scheme.sign_headers(BODY, SECRET, ahead)
    result = scheme.verify(BODY, SECRET, headers, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert result.stale is True


def test_an_unparseable_timestamp_is_stale_not_a_crash():
    scheme = tool.get_scheme("mailgun")
    result = scheme.verify(
        BODY, SECRET, {"X-Signature": "t=yesterday,v1=00"},
        now=NOW, tolerance=TOLERANCE,
    )
    assert result.accepted is False
    assert result.stale is True


# ---------------------------------------------------------------------------
# The four variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mailgun"])
def test_the_four_variants_are_built_from_one_body_and_timestamp(name):
    scheme = tool.get_scheme(name)
    variants = tool.build_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    assert [v.name for v in variants] == list(tool.VARIANTS)
    assert [v.expected_accepted for v in variants] == [True, False, False, False]
    assert variants[2].headers == {}, "the unsigned variant carries nothing"
    for variant in (variants[1], variants[3]):
        assert variant.headers, "a signed variant has to look like a real request"


@pytest.mark.parametrize("name", ["mailgun"])
def test_only_the_correct_variant_verifies(name):
    """The property the tool is for. One scheme deciding this is not enough
    evidence, and a scheme that accepted everything would still pass a test
    that only checked the correct one."""
    scheme = tool.get_scheme(name)
    outcomes = tool.check_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    assert [o.accepted for o in outcomes] == [True, False, False, False]
    assert all(o.correct for o in outcomes)


def test_a_scheme_with_no_timestamp_refuses_the_replay_variant_at_both_levels():
    """Its replayed request would be byte-identical to the correct one, so the
    variant would report that it had tested replay while testing nothing. That
    scheme's replay protection is the transport's, and saying so is the honest
    answer.

    Both levels refuse, because `check_variants` builds them: a helper that
    built the four and a wrapper that caught it would leave the caller holding a
    half-run it has to know to special-case.
    """
    scheme = tool.get_scheme("postmark")
    for build in (
        lambda: tool.build_variants(
            scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
        ),
        lambda: tool.check_variants(
            scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
        ),
    ):
        with pytest.raises(tool.SigcheckError) as caught:
            build()
        message = str(caught.value)
        assert "byte-identical" in message
        assert "transport" in message


def test_a_scheme_with_no_timestamp_answers_the_other_three_correctly():
    """Verified one at a time, because the four-variant helper refuses. The
    three it can express all come out right, which is what the refusal costs
    and it is not a refusal of the tool."""
    scheme = tool.get_scheme("postmark")
    correct = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    wrong = scheme.sign_headers(BODY, WRONG, TIMESTAMP)

    assert scheme.verify(
        BODY, SECRET, correct, now=NOW, tolerance=TOLERANCE
    ).accepted
    assert not scheme.verify(
        BODY, SECRET, wrong, now=NOW, tolerance=TOLERANCE
    ).accepted
    assert not scheme.verify(
        BODY, SECRET, {}, now=NOW, tolerance=TOLERANCE
    ).accepted


def test_the_variants_are_deterministic():
    """Two builds from the same inputs must be byte-identical, or a run cannot
    be replayed."""
    scheme = tool.get_scheme("mailgun")
    first = tool.build_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    second = tool.build_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    assert [v.headers for v in first] == [v.headers for v in second]


def test_a_receiver_that_accepts_everything_would_be_caught():
    """The negative control. A scheme that returned accepted=True for all four
    would fail here, and a tool that only checked the correct variant would
    not."""
    class _Open:
        name = "open"
        #: The stub has to claim a timestamp, or the four-variant helper
        #: refuses to build it and this negative control would be testing the
        #: refusal instead of the open receiver.
        carries_timestamp = True
        timestamp_header = "X-Timestamp"

        def sign_headers(self, body, secret, timestamp):
            return {"X-Signature": "whatever"}

        def verify(self, body, secret, headers, *, now, tolerance=tool.DEFAULT_TOLERANCE):
            return tool.VerifyResult(True, "accepted")

    outcomes = tool.check_variants(
        _Open(), BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    assert [o.correct for o in outcomes] == [True, False, False, False]
    assert outcomes[1].correct is False


def test_an_outcome_serializes_for_the_report():
    scheme = tool.get_scheme("mailgun")
    outcome = tool.check_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )[1]
    payload = outcome.to_json()
    assert payload["variant"] == tool.VARIANT_WRONG_SECRET
    assert payload["expected_accepted"] is False
    assert payload["accepted"] is False
    assert payload["verdict"] == "OK", "a correct verifier gets this variant right"
    assert payload["reason"]


# ---------------------------------------------------------------------------
# The asymmetric scheme
# ---------------------------------------------------------------------------


def test_sendgrid_names_its_own_headers():
    """The header names are the whole contract with a provider, and getting one
    wrong produces a request their receiver ignores."""
    scheme = tool.get_scheme("sendgrid")
    assert scheme.signature_header == "X-Twilio-Email-Event-Webhook-Signature"
    assert scheme.timestamp_header == "X-Twilio-Email-Event-Webhook-Timestamp"


def test_sendgrid_signing_without_a_primitive_refuses_and_says_what_to_pass():
    """A refusal that names the alternative, rather than a verdict nobody
    computed."""
    scheme = tool.get_scheme("sendgrid")
    with pytest.raises(tool.SigcheckError) as caught:
        scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    message = str(caught.value)
    assert "signer=" in message
    assert "mailgun" in message
    assert "no runtime dependencies" in message


def test_sendgrid_verifying_without_a_primitive_refuses():
    scheme = tool.get_scheme("sendgrid")
    with pytest.raises(tool.SigcheckError) as caught:
        scheme.verify(
            BODY, SECRET,
            {scheme.signature_header: "AAAA", scheme.timestamp_header: str(TIMESTAMP)},
            now=NOW, tolerance=TOLERANCE,
        )
    assert "verifier=" in str(caught.value)


def test_sendgrid_reports_a_missing_header_before_asking_for_a_primitive():
    """Ordering matters: a request with no signature is rejected on its own
    merits, and should not need an asymmetric primitive to say so."""
    scheme = tool.get_scheme("sendgrid")
    result = scheme.verify(BODY, SECRET, {}, now=NOW, tolerance=TOLERANCE)
    assert result.accepted is False
    assert "nothing was verified" in result.reason


def test_sendgrid_checks_the_timestamp_before_verifying():
    """A stale request must be rejected on staleness whatever its signature,
    and the reason has to say so rather than blaming the key."""
    scheme = tool.get_scheme("sendgrid", verifier=lambda b, s, k: True)
    result = scheme.verify(
        BODY, SECRET,
        {
            scheme.signature_header: base64.b64encode(b"sig").decode(),
            scheme.timestamp_header: str(NOW - 10_000),
        },
        now=NOW, tolerance=TOLERANCE,
    )
    assert result.accepted is False
    assert result.stale is True
    assert "window" in result.reason


def test_sendgrid_rejects_a_non_base64_signature():
    scheme = tool.get_scheme("sendgrid", verifier=lambda b, s, k: True)
    result = scheme.verify(
        BODY, SECRET,
        {scheme.signature_header: "not base64 !!", scheme.timestamp_header: str(NOW)},
        now=NOW, tolerance=TOLERANCE,
    )
    assert result.accepted is False
    assert "base64" in result.reason


def test_sendgrid_with_an_injected_primitive_answers_all_four_variants():
    """The whole tool, with the primitive stood in for. This is the part that
    would catch a wrong signing string or encoding, and it is why the contract
    is implemented separately from the primitive."""
    import hashlib as _h

    def signer(body, key):
        return _h.sha256(_h.sha256(body + str(key).encode()).digest()).digest()

    def verifier(body, signature, key):
        return hmac.compare_digest(signature, signer(body, key))

    scheme = tool.get_scheme("sendgrid", signer=signer, verifier=verifier)
    outcomes = tool.check_variants(
        scheme, BODY, SECRET, timestamp=TIMESTAMP, now=NOW, tolerance=TOLERANCE
    )
    assert [o.accepted for o in outcomes] == [True, False, False, False]
    assert all(o.correct for o in outcomes)


def test_the_asymmetric_signature_is_covered_by_a_tamper_check_too():
    """The injected primitive makes the byte coverage testable here, which is
    the property that matters and the one a published vector would not add."""
    def signer(body, key):
        return hashlib.sha256(body + str(key).encode()).digest()

    scheme = tool.get_scheme(
        "sendgrid",
        signer=signer,
        verifier=lambda b, s, k: hmac.compare_digest(s, signer(b, k)),
    )
    headers = scheme.sign_headers(BODY, SECRET, TIMESTAMP)
    tampered = scheme.verify(
        BODY + b" ", SECRET, headers, now=NOW, tolerance=TOLERANCE
    )
    assert tampered.accepted is False
