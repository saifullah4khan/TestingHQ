"""sigcheck's schemes, checked against each provider's documented construction.

The Mailgun vector below was computed with `openssl dgst -sha256 -hmac`, an
implementation independent of this module, over the construction Mailgun
documents (HMAC-SHA256 of timestamp + token, hex). A test that only checked this
module against itself could not tell a wrong construction from a right one.

The SendGrid tests use a stand-in primitive, because ECDSA is injected. They pin
the contract: the header names, and that the signed message is timestamp + body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from testinghq.pipeline import sigcheck as tool

NOW = 1_700_000_000
BODY = b"--boundary\r\nContent-Disposition: form-data; name=\"subject\"\r\n\r\nhi\r\n--boundary--\r\n"
MAILGUN_KEY = "key-3ax6xnjp29jd6fds4gc373sgvjxteol0"
BASIC = "hook-user:hook-pass"


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_table_has_the_three_schemes():
    assert tool.known_schemes() == ("basic-auth", "mailgun", "sendgrid")


def test_postmark_is_not_a_signing_scheme():
    """Postmark does not sign webhooks. A 'postmark' HMAC scheme would test
    for a signature Postmark never sends; its real mechanism is basic-auth."""
    with pytest.raises(tool.SigcheckError):
        tool.get_scheme("postmark")


def test_an_unknown_scheme_is_refused_and_lists_the_valid_ones():
    with pytest.raises(tool.SigcheckError) as excinfo:
        tool.get_scheme("mailgrun")
    for name in tool.known_schemes():
        assert name in str(excinfo.value)


def test_only_sendgrid_takes_injected_primitives():
    with pytest.raises(tool.SigcheckError):
        tool.get_scheme("mailgun", signer=lambda m, k: b"")
    with pytest.raises(tool.SigcheckError):
        tool.get_scheme("basic-auth", verifier=lambda m, s, k: True)


# ---------------------------------------------------------------------------
# mailgun
# ---------------------------------------------------------------------------


def test_mailgun_matches_an_independently_computed_vector():
    """HMAC-SHA256(key, "1529006854" + token), hex, computed with openssl."""
    signature = tool.mailgun_signature(
        MAILGUN_KEY, "1529006854", "a8ce0edb2dd8301dee6c2405235584e45aa91d1e9f979f3de0"
    )
    assert signature == "b63c0701c4f4b614f272106a1b367c5c3369bcdca664ae73ebd52787e45eef07"


def test_mailgun_signs_timestamp_and_token_not_the_body():
    """The mistake this replaced: signing timestamp + body. Mailgun's signature
    does not cover the body at all."""
    token = "a8ce0edb2dd8301dee6c2405235584e45aa91d1e9f979f3de0"
    assert tool.mailgun_signature(MAILGUN_KEY, "1529006854", token) != hmac.new(
        MAILGUN_KEY.encode(), b"1529006854" + BODY, hashlib.sha256
    ).hexdigest()


def test_mailgun_sends_body_fields_not_headers():
    signed = tool.get_scheme("mailgun").sign(BODY, MAILGUN_KEY, NOW)
    assert signed.headers == {}
    assert set(signed.fields) == {"timestamp", "token", "signature"}
    assert signed.fields["timestamp"] == str(NOW)
    assert signed.fields["signature"] == tool.mailgun_signature(
        MAILGUN_KEY, str(NOW), signed.fields["token"]
    )


def test_mailgun_token_is_fifty_characters_and_deterministic():
    assert len(tool.mailgun_token(NOW)) == 50
    assert tool.mailgun_token(NOW) == tool.mailgun_token(NOW)
    assert tool.mailgun_token(NOW) != tool.mailgun_token(NOW + 1)


def test_mailgun_verifies_its_own_signature():
    scheme = tool.get_scheme("mailgun")
    result = scheme.verify(BODY, MAILGUN_KEY, scheme.sign(BODY, MAILGUN_KEY, NOW), now=NOW)
    assert result.accepted, result.reason


def test_mailgun_signature_does_not_protect_the_body():
    """Stated as a test so nobody reads a green run as body integrity. A
    tampered body still verifies, because Mailgun does not sign it."""
    scheme = tool.get_scheme("mailgun")
    signed = scheme.sign(BODY, MAILGUN_KEY, NOW)
    assert scheme.verify(BODY + b"tampered", MAILGUN_KEY, signed, now=NOW).accepted


def test_mailgun_rejects_a_different_key():
    scheme = tool.get_scheme("mailgun")
    signed = scheme.sign(BODY, "some-other-key", NOW)
    result = scheme.verify(BODY, MAILGUN_KEY, signed, now=NOW)
    assert not result.accepted and not result.stale


def test_mailgun_rejects_missing_fields():
    result = tool.get_scheme("mailgun").verify(BODY, MAILGUN_KEY, tool.Signed(), now=NOW)
    assert not result.accepted
    assert "timestamp" in result.reason


def test_mailgun_rejects_a_stale_timestamp_and_says_so():
    scheme = tool.get_scheme("mailgun")
    signed = scheme.sign(BODY, MAILGUN_KEY, NOW - 3600)
    result = scheme.verify(BODY, MAILGUN_KEY, signed, now=NOW)
    assert not result.accepted and result.stale


def test_a_timestamp_in_the_future_is_also_stale():
    scheme = tool.get_scheme("mailgun")
    result = scheme.verify(BODY, MAILGUN_KEY, scheme.sign(BODY, MAILGUN_KEY, NOW + 3600), now=NOW)
    assert result.stale


def test_a_timestamp_on_the_window_edge_is_accepted():
    scheme = tool.get_scheme("mailgun")
    edge = NOW - int(tool.DEFAULT_TOLERANCE)
    assert scheme.verify(BODY, MAILGUN_KEY, scheme.sign(BODY, MAILGUN_KEY, edge), now=NOW).accepted


def test_an_unparseable_mailgun_timestamp_is_stale_not_a_crash():
    signed = tool.Signed(fields={"timestamp": "soon", "token": "t", "signature": "00"})
    result = tool.get_scheme("mailgun").verify(BODY, MAILGUN_KEY, signed, now=NOW)
    assert not result.accepted and result.stale


# ---------------------------------------------------------------------------
# basic-auth
# ---------------------------------------------------------------------------


def test_basic_auth_sends_a_standard_authorization_header():
    signed = tool.get_scheme("basic-auth").sign(BODY, BASIC, NOW)
    assert signed.headers == {
        "Authorization": "Basic " + base64.b64encode(BASIC.encode()).decode()
    }


def test_basic_auth_rejects_wrong_or_missing_credentials():
    scheme = tool.get_scheme("basic-auth")
    wrong = scheme.sign(BODY, "hook-user:nope", NOW)
    assert not scheme.verify(BODY, BASIC, wrong, now=NOW).accepted
    assert not scheme.verify(BODY, BASIC, tool.Signed(), now=NOW).accepted
    assert scheme.verify(BODY, BASIC, scheme.sign(BODY, BASIC, NOW), now=NOW).accepted


def test_basic_auth_needs_user_and_password():
    with pytest.raises(tool.SigcheckError):
        tool.get_scheme("basic-auth").sign(BODY, "no-colon", NOW)


def test_basic_auth_has_no_replay_variant():
    """A replay would be byte-identical to the live request, so building one
    would test nothing while reporting that it had."""
    names = [v.name for v in tool.build_variants(
        tool.get_scheme("basic-auth"), BODY, BASIC, timestamp=NOW, now=NOW)]
    assert names == [tool.VARIANT_CORRECT, tool.VARIANT_WRONG_SECRET, tool.VARIANT_UNSIGNED]


# ---------------------------------------------------------------------------
# sendgrid (contract, with a stand-in primitive)
# ---------------------------------------------------------------------------


def _stand_in(key=b"k"):
    """HMAC standing in for ECDSA. It checks the contract (which bytes are
    signed, which headers carry the result), not the curve."""
    seen = []

    def signer(message, secret):
        seen.append(message)
        return hmac.new(secret, message, hashlib.sha256).digest()

    def verifier(message, signature, secret):
        return hmac.compare_digest(signer(message, secret), signature)

    return signer, verifier, seen


def test_sendgrid_uses_the_documented_headers():
    signer, verifier, _ = _stand_in()
    signed = tool.get_scheme("sendgrid", signer=signer, verifier=verifier).sign(BODY, b"k", NOW)
    assert set(signed.headers) == {
        "X-Twilio-Email-Event-Webhook-Signature",
        "X-Twilio-Email-Event-Webhook-Timestamp",
    }
    assert signed.fields == {}


def test_sendgrid_signs_timestamp_then_raw_body():
    signer, verifier, seen = _stand_in()
    tool.get_scheme("sendgrid", signer=signer, verifier=verifier).sign(BODY, b"k", NOW)
    assert seen[0] == str(NOW).encode() + BODY


def test_sendgrid_without_a_primitive_refuses_and_says_what_to_pass():
    with pytest.raises(tool.SigcheckError) as excinfo:
        tool.get_scheme("sendgrid").sign(BODY, b"k", NOW)
    assert "signer=" in str(excinfo.value)


def test_sendgrid_reports_a_missing_header_before_asking_for_a_primitive():
    result = tool.get_scheme("sendgrid").verify(BODY, b"k", tool.Signed(), now=NOW)
    assert not result.accepted


def test_sendgrid_rejects_a_non_base64_signature():
    signer, verifier, _ = _stand_in()
    signed = tool.Signed(headers={
        tool.SENDGRID_SIGNATURE_HEADER: "not base64!!",
        tool.SENDGRID_TIMESTAMP_HEADER: str(NOW),
    })
    result = tool.get_scheme("sendgrid", verifier=verifier).verify(BODY, b"k", signed, now=NOW)
    assert not result.accepted


def test_sendgrid_detects_a_tampered_body():
    """Unlike Mailgun, SendGrid's signature covers the body."""
    signer, verifier, _ = _stand_in()
    scheme = tool.get_scheme("sendgrid", signer=signer, verifier=verifier)
    signed = scheme.sign(BODY, b"k", NOW)
    assert scheme.verify(BODY, b"k", signed, now=NOW).accepted
    assert not scheme.verify(BODY + b"x", b"k", signed, now=NOW).accepted


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def _schemes():
    signer, verifier, _ = _stand_in()
    return [
        (tool.get_scheme("mailgun"), MAILGUN_KEY, "wrong-key"),
        (tool.get_scheme("sendgrid", signer=signer, verifier=verifier), b"k", b"other"),
        (tool.get_scheme("basic-auth"), BASIC, "hook-user:nope"),
    ]


def test_only_the_correct_variant_verifies_for_every_scheme():
    for scheme, secret, wrong in _schemes():
        outcomes = tool.check_variants(
            scheme, BODY, secret, timestamp=NOW, now=NOW, wrong_secret=wrong)
        accepted = [o.variant for o in outcomes if o.accepted]
        assert accepted == [tool.VARIANT_CORRECT], (scheme.name, outcomes)
        assert all(o.correct for o in outcomes), scheme.name


def test_timestamped_schemes_get_the_replay_variant():
    for scheme, secret, wrong in _schemes()[:2]:
        names = [v.name for v in tool.build_variants(
            scheme, BODY, secret, timestamp=NOW, now=NOW, wrong_secret=wrong)]
        assert names == list(tool.VARIANTS), scheme.name


def test_a_receiver_that_accepts_everything_would_be_caught():
    for scheme, secret, wrong in _schemes():
        variants = tool.build_variants(
            scheme, BODY, secret, timestamp=NOW, now=NOW, wrong_secret=wrong)
        outcomes = [
            tool.VariantOutcome(scheme.name, v.name, v.expected_accepted, True, "accepted")
            for v in variants
        ]
        assert [o.variant for o in outcomes if not o.correct] == [
            v.name for v in variants if not v.expected_accepted
        ]


def test_the_variants_are_deterministic():
    scheme = tool.get_scheme("mailgun")
    first = tool.build_variants(scheme, BODY, MAILGUN_KEY, timestamp=NOW, now=NOW)
    second = tool.build_variants(scheme, BODY, MAILGUN_KEY, timestamp=NOW, now=NOW)
    assert first == second


def test_an_outcome_serializes_for_the_report():
    outcome = tool.VariantOutcome("mailgun", tool.VARIANT_UNSIGNED, False, True, "accepted")
    assert outcome.to_json()["verdict"] == "FINDING"
