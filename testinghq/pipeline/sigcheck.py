"""sigcheck: does your endpoint check the webhook signature?

A provider authenticates its inbound webhook; the question is whether the
receiver checks it. Six requests answer that, one per variant:

  valid            correctly authenticated, timestamp now
  wrong-secret     well formed, made with a different secret
  unsigned         no authentication at all
  stale-timestamp  correctly authenticated, timestamp outside the window
  tampered-body    authentication from the original body, a different body sent
  replayed-token   the valid request sent a second time, unchanged

The schemes, from each provider's documentation:

  mailgun         Hex HMAC-SHA256 of `timestamp + token` (no separator), keyed
                  with the webhook signing key, sent as `timestamp`, `token` and
                  `signature` form fields. The body is not signed. Mailgun's
                  replay defence is caching the token and refusing a second
                  request carrying it; checking the timestamp is optional.
                  https://documentation.mailgun.com/docs/mailgun/user-manual/webhooks/securing-webhooks
  sendgrid        ECDSA with SHA-256 over `timestamp + raw body`, on the curve
                  SendGrid documents for its signed webhooks; base64 of the
                  ASN.1 (r, s) pair in X-Twilio-Email-Event-Webhook-Signature,
                  timestamp in X-Twilio-Email-Event-Webhook-Timestamp. The
                  endpoint holds the public key; this tool signs with the
                  matching private key, supplied as `env:NAME`. No nonce.
                  https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/securing-your-parse-webhooks
  postmark-basic  Postmark does not sign webhooks. Its documented protection is
                  HTTP Basic credentials in the webhook URL, which arrive as an
                  Authorization header. No timestamp, so no stale or replay case.
                  https://postmarkapp.com/developer/webhooks/webhooks-overview

The ECDSA signer below is the standard library only (RFC 6979 deterministic
nonces), so a run is reproducible and needs no dependency. It implements one
curve, NIST P-256, and is tested against the RFC 6979 P-256 vectors. Use it
with a key on the curve SendGrid documents for the public key it issues; a key
on any other curve is refused rather than signed with the wrong arithmetic.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..blast.payload import Envelope, GroundTruth, InboundEmail
from ..core.exit_codes import EXIT_FINDING, EXIT_OK, EXIT_REFUSED

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

VARIANT_VALID = "valid"
VARIANT_WRONG_SECRET = "wrong-secret"
VARIANT_UNSIGNED = "unsigned"
VARIANT_STALE = "stale-timestamp"
VARIANT_TAMPERED = "tampered-body"
VARIANT_REPLAYED = "replayed-token"
VARIANTS: Tuple[str, ...] = (
    VARIANT_VALID,
    VARIANT_WRONG_SECRET,
    VARIANT_UNSIGNED,
    VARIANT_STALE,
    VARIANT_TAMPERED,
    VARIANT_REPLAYED,
)

#: What a correct endpoint does with a variant.
EXPECT_ACCEPT = "accept"
EXPECT_REJECT = "reject"
#: Reported, never a finding: the scheme cannot tell, or the provider leaves it open.
EXPECT_INFO = "info"
#: Not sent: the scheme has nothing for this variant to vary.
EXPECT_NA = "n/a"

DEFAULT_WINDOW = 300.0

SENDGRID_SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
SENDGRID_TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"
MAILGUN_FIELDS = ("timestamp", "token", "signature")

#: The wire formats each scheme's authentication is defined for.
SCHEME_FORMATS: Dict[str, Tuple[str, ...]] = {
    "mailgun": ("mailgun",),
    "sendgrid": ("sendgrid", "sendgrid-raw"),
    "postmark-basic": ("postmark",),
}


class SigcheckError(ValueError):
    """A malformed scheme, key or configuration. A caller error, never a finding."""


# ---------------------------------------------------------------------------
# ECDSA P-256, standard library only
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_OID_EC_PUBLIC_KEY = bytes.fromhex("2a8648ce3d0201")
_OID_P256 = bytes.fromhex("2a8648ce3d030107")
#: DER prefix of a P-256 SubjectPublicKeyInfo, before the 65-byte point.
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")

Point = Optional[Tuple[int, int]]


def _on_curve(point: Point) -> bool:
    if point is None:
        return False
    x, y = point
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        slope = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _mul(k: int, point: Point) -> Point:
    result: Point = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def p256_public_key(private: int) -> Tuple[int, int]:
    if not 1 <= private < _N:
        raise SigcheckError("a P-256 private key must be in [1, n-1]")
    point = _mul(private, _G)
    assert point is not None
    return point


def _rfc6979_nonces(private: int, digest: bytes):
    """Deterministic nonces per RFC 6979 section 3.2, HMAC-SHA256, qlen 256."""
    x = private.to_bytes(32, "big")
    h1 = (int.from_bytes(digest, "big") % _N).to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def p256_sign_digest(private: int, digest: bytes) -> Tuple[int, int]:
    e = int.from_bytes(digest, "big") % _N
    for nonce in _rfc6979_nonces(private, digest):
        point = _mul(nonce, _G)
        assert point is not None
        r = point[0] % _N
        if r == 0:
            continue
        s = pow(nonce, -1, _N) * (e + r * private) % _N
        if s:
            return r, s
    raise AssertionError("unreachable")  # pragma: no cover


def p256_verify_digest(public: Tuple[int, int], digest: bytes, r: int, s: int) -> bool:
    if not (1 <= r < _N and 1 <= s < _N) or not _on_curve(public):
        return False
    e = int.from_bytes(digest, "big") % _N
    w = pow(s, -1, _N)
    point = _add(_mul(e * w % _N, _G), _mul(r * w % _N, public))
    return point is not None and point[0] % _N == r


# -- DER ----------------------------------------------------------------------


def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + _der_len(len(raw)) + raw


def _der_read(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    """(tag, value, next offset) of the TLV at `offset`."""
    try:
        tag = data[offset]
        length = data[offset + 1]
        offset += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or count > 4:
                raise SigcheckError("unsupported DER length")
            length = int.from_bytes(data[offset:offset + count], "big")
            offset += count
        value = data[offset:offset + length]
        if len(value) != length:
            raise SigcheckError("truncated DER")
        return tag, value, offset + length
    except IndexError:
        raise SigcheckError("truncated DER") from None


def _der_children(data: bytes) -> List[Tuple[int, bytes]]:
    out = []
    offset = 0
    while offset < len(data):
        tag, value, offset = _der_read(data, offset)
        out.append((tag, value))
    return out


def der_signature(r: int, s: int) -> bytes:
    body = _der_int(r) + _der_int(s)
    return b"\x30" + _der_len(len(body)) + body


def parse_der_signature(data: bytes) -> Tuple[int, int]:
    tag, value, end = _der_read(data)
    if tag != 0x30 or end != len(data):
        raise SigcheckError("not a DER SEQUENCE")
    children = _der_children(value)
    if len(children) != 2 or any(t != 0x02 for t, _ in children):
        raise SigcheckError("not a DER (r, s) pair")
    return int.from_bytes(children[0][1], "big"), int.from_bytes(children[1][1], "big")


def spki_der(public: Tuple[int, int]) -> bytes:
    """The SubjectPublicKeyInfo DER for a P-256 point, uncompressed."""
    return _SPKI_PREFIX + b"\x04" + public[0].to_bytes(32, "big") + public[1].to_bytes(32, "big")


def parse_public_key(text: str) -> Tuple[int, int]:
    """A base64 SubjectPublicKeyInfo (the form SendGrid's policy returns) or PEM."""
    der = _pem_or_base64(text, ("PUBLIC KEY",))
    if not der.startswith(_SPKI_PREFIX) or len(der) != len(_SPKI_PREFIX) + 65 or der[len(_SPKI_PREFIX)] != 4:
        raise SigcheckError("not an uncompressed P-256 SubjectPublicKeyInfo")
    point_bytes = der[len(_SPKI_PREFIX) + 1:]
    point = (int.from_bytes(point_bytes[:32], "big"), int.from_bytes(point_bytes[32:], "big"))
    if not _on_curve(point):
        raise SigcheckError("the public key is not a point on P-256")
    return point


def _pem_or_base64(text: str, labels: Tuple[str, ...]) -> bytes:
    text = text.replace("\\n", "\n").strip()
    if text.startswith("-----BEGIN"):
        lines = text.splitlines()
        label = lines[0].strip("- ").replace("BEGIN ", "", 1)
        if label not in labels:
            raise SigcheckError(f"expected a PEM {' or '.join(labels)}, got {label!r}")
        text = "".join(line for line in lines[1:] if not line.startswith("-----"))
    try:
        return base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise SigcheckError("not base64 or PEM") from None


def parse_private_key(text: str) -> int:
    """A P-256 private key: 64 hex digits, or PEM/base64 SEC1 or PKCS#8 DER."""
    stripped = text.strip()
    if len(stripped) == 64 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        value = int(stripped, 16)
    else:
        der = _pem_or_base64(stripped, ("EC PRIVATE KEY", "PRIVATE KEY"))
        value = _private_from_der(der)
    if not 1 <= value < _N:
        raise SigcheckError("the private key is out of range for P-256")
    return value


def _private_from_der(der: bytes) -> int:
    tag, body, _end = _der_read(der)
    if tag != 0x30:
        raise SigcheckError("a private key must be a DER SEQUENCE")
    children = _der_children(body)
    if len(children) >= 3 and children[1][0] == 0x30:  # PKCS#8
        algorithm = [value for t, value in _der_children(children[1][1]) if t == 0x06]
        if _OID_EC_PUBLIC_KEY not in algorithm or _OID_P256 not in algorithm:
            raise SigcheckError("the PKCS#8 key is not an EC key on P-256")
        return _private_from_der(children[2][1])
    if len(children) >= 2 and children[0][0] == 0x02 and children[1][0] == 0x04:  # SEC1
        for t, value in children[2:]:
            if t == 0xA0:
                oid = _der_children(value)
                if oid and oid[0][1] != _OID_P256:
                    raise SigcheckError("the EC key is not on P-256")
        return int.from_bytes(children[1][1], "big")
    raise SigcheckError("unrecognised private key structure")


# ---------------------------------------------------------------------------
# Schemes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signed:
    """What a scheme adds to a request: headers, body form fields, or both."""

    headers: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)


def mailgun_token(timestamp: int, nonce: str = "") -> str:
    """A deterministic 50-character token, the length Mailgun sends. `nonce`
    gives each variant its own token, so a token cache cannot refuse a
    variant for the wrong reason."""
    seed = f"testinghq-mailgun-{timestamp}-{nonce}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:50]


def mailgun_signature(signing_key: str, timestamp: str, token: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"), f"{timestamp}{token}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


class MailgunScheme:
    name = "mailgun"
    has_timestamp = True
    signs_body = False

    def wrong_secret(self, secret: str) -> str:
        return "testinghq-not-the-signing-key"

    def sign(self, body: bytes, secret: str, timestamp: int, nonce: str = "") -> Signed:
        token = mailgun_token(timestamp, nonce)
        return Signed(fields={
            "timestamp": str(timestamp),
            "token": token,
            "signature": mailgun_signature(secret, str(timestamp), token),
        })

    def verify(self, body: bytes, secret: str, signed: Signed) -> bool:
        if any(name not in signed.fields for name in MAILGUN_FIELDS):
            return False
        expected = mailgun_signature(secret, signed.fields["timestamp"], signed.fields["token"])
        return hmac.compare_digest(expected, signed.fields["signature"])


class SendgridScheme:
    """`secret` is the P-256 private key the test signs with; the endpoint is
    configured with the matching public key (`public_key_b64`)."""

    name = "sendgrid"
    has_timestamp = True
    signs_body = True

    @staticmethod
    def _key(secret: Any) -> int:
        return secret if isinstance(secret, int) else parse_private_key(str(secret))

    def wrong_secret(self, secret: Any) -> int:
        other = int.from_bytes(hashlib.sha256(b"testinghq-sigcheck-wrong-key").digest(), "big") % _N
        return other if other != self._key(secret) else other + 1

    @staticmethod
    def public_key_b64(secret: Any) -> str:
        return base64.b64encode(spki_der(p256_public_key(SendgridScheme._key(secret)))).decode("ascii")

    def sign(self, body: bytes, secret: Any, timestamp: int, nonce: str = "") -> Signed:
        del nonce
        digest = hashlib.sha256(str(timestamp).encode("ascii") + body).digest()
        r, s = p256_sign_digest(self._key(secret), digest)
        return Signed(headers={
            SENDGRID_SIGNATURE_HEADER: base64.b64encode(der_signature(r, s)).decode("ascii"),
            SENDGRID_TIMESTAMP_HEADER: str(timestamp),
        })

    def verify(self, body: bytes, secret: Any, signed: Signed) -> bool:
        raw = signed.headers.get(SENDGRID_SIGNATURE_HEADER)
        timestamp = signed.headers.get(SENDGRID_TIMESTAMP_HEADER)
        if raw is None or timestamp is None:
            return False
        try:
            r, s = parse_der_signature(base64.b64decode(raw, validate=True))
        except (SigcheckError, binascii.Error, ValueError):
            return False
        digest = hashlib.sha256(timestamp.encode("ascii") + body).digest()
        return p256_verify_digest(p256_public_key(self._key(secret)), digest, r, s)


class PostmarkBasicScheme:
    """`secret` is `user:password`, as in `https://user:password@host/...`."""

    name = "postmark-basic"
    has_timestamp = False
    signs_body = False

    def wrong_secret(self, secret: str) -> str:
        user = secret.split(":", 1)[0]
        return f"{user}:testinghq-not-the-password"

    @staticmethod
    def _header(secret: str) -> str:
        if ":" not in secret:
            raise SigcheckError("a postmark-basic secret must be 'user:password'")
        return "Basic " + base64.b64encode(secret.encode("utf-8")).decode("ascii")

    def sign(self, body: bytes, secret: str, timestamp: int, nonce: str = "") -> Signed:
        del body, timestamp, nonce
        return Signed(headers={"Authorization": self._header(secret)})

    def verify(self, body: bytes, secret: str, signed: Signed) -> bool:
        presented = signed.headers.get("Authorization")
        return presented is not None and hmac.compare_digest(presented, self._header(secret))


_SCHEMES = {
    "mailgun": MailgunScheme,
    "sendgrid": SendgridScheme,
    "postmark-basic": PostmarkBasicScheme,
}


def known_schemes() -> Tuple[str, ...]:
    return tuple(sorted(_SCHEMES))


def get_scheme(name: str):
    constructor = _SCHEMES.get(name)
    if constructor is None:
        raise SigcheckError(
            f"unknown signature scheme {name!r}; known: {list(known_schemes())}"
        )
    return constructor()


# ---------------------------------------------------------------------------
# The plan: what each variant sends and what a correct endpoint does
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Planned:
    """One variant: the payload, the authentication, and the expectation."""

    variant: str
    expect: str
    reason: str
    payload: Optional[InboundEmail] = None
    signed: Signed = field(default_factory=Signed)
    #: Headers fixed in advance, for a variant whose signature must not follow
    #: the bytes actually sent (tampered-body).
    fixed_headers: Optional[Dict[str, str]] = None


def sample_payload(tampered: bool = False) -> InboundEmail:
    """The message every variant carries. `tampered` changes the subject."""
    sender = "sigcheck@example.com"
    to = "intake@example.com"
    subject = "Order 1042: question about delivery"
    if tampered:
        subject = "Order 1042: please refund to a new account"
    body = "Hello, could you tell me when order 1042 will arrive? Thank you."
    return InboundEmail(
        to=to,
        from_addr=sender,
        subject=subject,
        text=body + "\n",
        html=f"<html><body><p>{body}</p></body></html>",
        envelope=Envelope(to=(to,), from_addr=sender),
        ground_truth=GroundTruth(from_addr=sender, subject=subject, body_core=body),
        headers={"Message-ID": "<sigcheck-1042@hq.example.test>", "Subject": subject},
    )


def plan(scheme, secret: Any, *, now: int, window: float, encode: Callable[[InboundEmail, Signed], bytes]) -> List[Planned]:
    """Every variant for `scheme`, in send order. `encode(payload, signed)` is
    the body bytes the wire format produces, which SendGrid's signature covers."""
    original = sample_payload()
    tampered = sample_payload(tampered=True)
    stale_ts = now - int(window) - 60

    def signed_for(payload: InboundEmail, key: Any, timestamp: int, nonce: str) -> Signed:
        if scheme.signs_body:
            return scheme.sign(encode(payload, Signed()), key, timestamp, nonce)
        return scheme.sign(b"", key, timestamp, nonce)

    valid = signed_for(original, secret, now, VARIANT_VALID)
    out = [
        Planned(VARIANT_VALID, EXPECT_ACCEPT, "authenticated with the configured secret, timestamp now",
                original, valid),
        Planned(VARIANT_WRONG_SECRET, EXPECT_REJECT, "well formed, made with a different secret",
                original, signed_for(original, scheme.wrong_secret(secret), now, VARIANT_WRONG_SECRET)),
        Planned(VARIANT_UNSIGNED, EXPECT_REJECT, "no authentication at all", original, Signed()),
    ]

    if scheme.has_timestamp:
        stale = signed_for(original, secret, stale_ts, VARIANT_STALE)
        if scheme.name == "mailgun":
            out.append(Planned(
                VARIANT_STALE, EXPECT_INFO,
                f"correctly signed, timestamp {now - stale_ts}s old. Mailgun makes the "
                "timestamp check optional and names token caching as the replay defence",
                original, stale))
        else:
            out.append(Planned(
                VARIANT_STALE, EXPECT_REJECT,
                f"correctly signed, timestamp {now - stale_ts}s old, outside the "
                f"{window:g}s window. SendGrid has no token, so the window is the only "
                "replay defence it leaves",
                original, stale))
    else:
        out.append(Planned(VARIANT_STALE, EXPECT_NA,
                           f"{scheme.name} carries no timestamp"))

    if scheme.signs_body:
        out.append(Planned(
            VARIANT_TAMPERED, EXPECT_REJECT,
            "signature computed over the original body, a different body sent",
            tampered, fixed_headers=dict(valid.headers)))
    else:
        out.append(Planned(
            VARIANT_TAMPERED, EXPECT_INFO,
            f"{scheme.name} authentication does not cover the body, so a changed body "
            "with valid authentication cannot be detected by it",
            tampered, signed_for(tampered, secret, now, VARIANT_TAMPERED)))

    if scheme.name == "mailgun":
        out.append(Planned(
            VARIANT_REPLAYED, EXPECT_REJECT,
            "the valid request's timestamp, token and signature sent again. Mailgun's "
            "documented replay defence is to cache the token and refuse a second use",
            original, valid))
    elif scheme.has_timestamp:
        out.append(Planned(
            VARIANT_REPLAYED, EXPECT_INFO,
            "the valid request sent again inside the window. SendGrid sends no nonce, "
            "so only an endpoint keeping its own cache can refuse this",
            original, valid))
    else:
        out.append(Planned(
            VARIANT_REPLAYED, EXPECT_NA,
            f"{scheme.name} has no timestamp or token, so a replay is byte-identical "
            "to the valid request"))
    return out


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------

VERDICT_OK = "OK"
VERDICT_FINDING = "FINDING"
VERDICT_INFO = "INFO"
VERDICT_NA = "N/A"


@dataclass(frozen=True)
class Outcome:
    variant: str
    expect: str
    reason: str
    status: Optional[int]
    error: Optional[str]
    verdict: str
    detail: str

    @property
    def accepted(self) -> Optional[bool]:
        if self.status is None:
            return None
        return 200 <= self.status < 300

    def to_json(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "expect": self.expect,
            "reason": self.reason,
            "status": self.status,
            "error": self.error,
            "accepted": self.accepted,
            "verdict": self.verdict,
            "detail": self.detail,
        }


def judge(planned: Planned, status: Optional[int], error: Optional[str]) -> Outcome:
    """The verdict for one variant from the endpoint's answer.

    2xx is accepted; any 4xx is a rejection; a 5xx or no response is neither,
    and a finding, because a request with bad authentication should be refused
    cleanly rather than crash the receiver.
    """
    def out(verdict: str, detail: str) -> Outcome:
        return Outcome(planned.variant, planned.expect, planned.reason, status, error, verdict, detail)

    if planned.expect == EXPECT_NA:
        return out(VERDICT_NA, "not sent: " + planned.reason)
    if status is None:
        return out(VERDICT_FINDING, f"no response: {error or 'transport failure'}")
    accepted = 200 <= status < 300
    rejected = 400 <= status < 500
    if not accepted and not rejected:
        return out(VERDICT_FINDING, f"answered {status}; a request should be accepted (2xx) "
                                    "or refused (4xx), not crash the receiver")
    if planned.expect == EXPECT_INFO:
        word = "accepted" if accepted else "refused"
        return out(VERDICT_INFO, f"{word} ({status})")
    if planned.expect == EXPECT_ACCEPT:
        if accepted:
            return out(VERDICT_OK, f"accepted ({status})")
        return out(VERDICT_FINDING, f"a correctly authenticated request was refused ({status}): "
                                    "check the secret the endpoint is configured with")
    if rejected:
        return out(VERDICT_OK, f"refused ({status})")
    return out(VERDICT_FINDING, f"accepted ({status}): the endpoint does not enforce this")


def notes_for(outcomes: List[Outcome], scheme_name: str) -> List[str]:
    """Run-level notes the per-variant verdicts do not say on their own."""
    by = {o.variant: o for o in outcomes}
    notes = []
    stale = by.get(VARIANT_STALE)
    replay = by.get(VARIANT_REPLAYED)
    if scheme_name == "mailgun" and stale and replay and stale.accepted and replay.accepted is False:
        notes.append(
            "stale-timestamp was accepted and replayed-token refused: the endpoint "
            "follows Mailgun's own guidance (token caching, lenient timestamp). Informational."
        )
    valid = by.get(VARIANT_VALID)
    if valid and valid.verdict == VERDICT_FINDING:
        notes.append(
            "the valid request was not accepted, so every refusal below may be the "
            "endpoint refusing everything rather than checking anything"
        )
    return notes


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _encoder(scheme_name: str, target_format):
    """(encode(payload, signed) -> bytes, wire_format_for(signed)) for the scheme.

    Mailgun's signature travels as three form fields, so each variant gets its
    own `MailgunFormat` carrying them. Every other scheme authenticates with
    headers and uses the target's own format unchanged."""
    from ..core.formats import MailgunFormat
    from ..core.transport import DEFAULT_BOUNDARY

    def wire_format_for(signed: Signed):
        if scheme_name == "mailgun":
            fields = dict(signed.fields) if signed.fields else None
            return MailgunFormat(DEFAULT_BOUNDARY, signature_fields=fields)
        return target_format

    def encode(payload: InboundEmail, signed: Signed) -> bytes:
        return wire_format_for(signed).encode(payload).body

    return encode, wire_format_for


def run_config(
    scheme: str, target: Optional[str], format_name: Optional[str], window: float,
    secret_ref: Optional[str], dry_run: bool, allow_public_target: bool = False,
) -> Dict[str, Any]:
    """Everything needed to reproduce the run. The secret is recorded as the
    `env:NAME` reference it was read from, never as its value."""
    return {
        "tool": "sigcheck",
        "scheme": scheme,
        "target": target,
        "format": format_name,
        "timestamp_window_seconds": window,
        "secret": secret_ref,
        "dry_run": dry_run,
        "allow_public_target": bool(allow_public_target),
    }


def build_artifact(config: Dict[str, Any], outcomes: List[Outcome], notes: List[str],
                   *, aborted: bool = False) -> Dict[str, Any]:
    findings = sum(1 for o in outcomes if o.verdict == VERDICT_FINDING)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "sigcheck",
        "seed": None,
        "aborted": aborted,
        "config": config,
        "summary": {
            "verdict": "ENFORCED" if findings == 0 and not aborted else "GAPS",
            "findings": findings,
            "informational": sum(1 for o in outcomes if o.verdict == VERDICT_INFO),
            "sent": sum(1 for o in outcomes if o.verdict != VERDICT_NA),
            "variants": len(outcomes),
            "notes": notes,
        },
        "variants": [o.to_json() for o in outcomes],
    }


def format_dry_run(scheme_name: str, planned: List[Planned], target: Optional[str],
                   window: float, public_key: Optional[str],
                   secret_missing: Optional[str] = None) -> str:
    lines = [
        f"dry-run preview: sigcheck, scheme {scheme_name!r}, {len(planned)} variants",
        f"  target:            {target or '(none given)'}",
        f"  timestamp window:  {window:g}s",
    ]
    if public_key:
        lines.append(f"  public key for the endpoint (base64 SPKI): {public_key}")
    for item in planned:
        lines.append(f"    {item.variant:16} expect {item.expect:7} {item.reason}")
    if secret_missing:
        lines.append(f"a real run refuses until the secret can be read: {secret_missing}")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


def format_results(artifact: Dict[str, Any]) -> str:
    summary = artifact["summary"]
    lines = [
        f"sigcheck: {summary['verdict']}  ({summary['sent']} request(s) sent, "
        f"scheme {artifact['config']['scheme']!r})",
        "",
        f"  {'variant':16} {'expect':7} {'status':6} verdict",
    ]
    for item in artifact["variants"]:
        status = "-" if item["status"] is None else str(item["status"])
        lines.append(f"  {item['variant']:16} {item['expect']:7} {status:6} {item['verdict']}: {item['detail']}")
    for note in summary["notes"]:
        lines.append("")
        lines.append(f"  note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_SECTION = "sigcheck"

#: A fixed clock for dry runs, so a preview prints the same thing every time.
DRY_RUN_NOW = 1_700_000_000


@dataclass(frozen=True)
class Settings:
    """The scheme, where its secret comes from, and the timestamp window."""

    scheme: str
    secret_ref: Optional[str]
    window: float


def load_sigcheck_config(path: Optional[str], *, required: bool = True) -> Dict[str, Any]:
    """The validated `[sigcheck]` table. With `required=False` a missing config
    file reads as an empty table, for a dry run."""
    from .toolconfig import ToolConfigError, read_tool_table, read_tool_table_if_present

    reader = read_tool_table if required else read_tool_table_if_present
    try:
        section = dict(reader(path, CONFIG_SECTION) if path else {})
    except ToolConfigError as exc:
        raise SigcheckError(str(exc)) from exc
    scheme = section.get("scheme")
    if scheme is not None and scheme not in _SCHEMES:
        raise SigcheckError(
            f"[{CONFIG_SECTION}].scheme must be one of {list(known_schemes())}, got {scheme!r}"
        )
    secret = section.get("secret")
    if secret is not None and not (isinstance(secret, str) and secret.startswith("env:")):
        raise SigcheckError(
            f"[{CONFIG_SECTION}].secret must be an env:NAME reference, never a literal"
        )
    window = section.get("timestamp_window_seconds")
    if window is not None and (
        isinstance(window, bool) or not isinstance(window, (int, float)) or window <= 0
    ):
        raise SigcheckError(
            f"[{CONFIG_SECTION}].timestamp_window_seconds must be a positive number, "
            f"got {window!r}"
        )
    return section


def effective_settings(
    section: Dict[str, Any], *, scheme: Optional[str], secret: Optional[str],
    window: Optional[float],
) -> Settings:
    """Flag, then `[sigcheck]`, then the default. A scheme is required."""
    chosen = scheme or section.get("scheme")
    if not chosen:
        raise SigcheckError(
            f"no signature scheme: pass --scheme ({' | '.join(known_schemes())}) "
            f"or set [{CONFIG_SECTION}].scheme"
        )
    get_scheme(chosen)
    span = window if window is not None else section.get("timestamp_window_seconds", DEFAULT_WINDOW)
    if span <= 0:
        raise SigcheckError(f"--window must be a positive number of seconds, got {span}")
    return Settings(scheme=chosen, secret_ref=secret or section.get("secret"), window=float(span))


def resolve_secret(settings: Settings, env: Optional[Dict[str, str]] = None) -> str:
    """The secret's value, from the environment variable its reference names."""
    from .toolconfig import ToolConfigError, resolve_env_secret

    if not settings.secret_ref:
        raise SigcheckError(
            f"no secret: pass --secret env:NAME or set [{CONFIG_SECTION}].secret"
        )
    try:
        return resolve_env_secret(settings.secret_ref, "--secret", env)
    except ToolConfigError as exc:
        raise SigcheckError(str(exc)) from exc


def _placeholder_secret(scheme_name: str) -> Any:
    """Something to sign a dry-run preview with when no secret is available."""
    if scheme_name == "sendgrid":
        return 1
    if scheme_name == "postmark-basic":
        return "dry-run:dry-run"
    return "dry-run"


def preview(settings: Settings, secret: Any = None, target: Optional[str] = None,
            missing: Optional[str] = None) -> str:
    """The dry-run text: every variant, what it sends, what a correct endpoint
    does. With no secret, the plan is built with a placeholder and `missing`
    says why."""
    scheme = get_scheme(settings.scheme)
    key = secret if secret is not None else _placeholder_secret(scheme.name)
    planned = plan(scheme, key, now=DRY_RUN_NOW, window=settings.window,
                   encode=lambda _payload, _signed: b"")
    public_key = None
    if scheme.name == "sendgrid" and secret is not None:
        public_key = SendgridScheme.public_key_b64(key)
    if secret is None and missing is None:
        missing = "no secret was given"
    return format_dry_run(scheme.name, planned, target, settings.window, public_key,
                          secret_missing=missing if secret is None else None)


def format_mismatch(scheme_name: str, target_name: str, format_name: str) -> Optional[str]:
    """Why a target in `format_name` cannot carry `scheme_name`, or None.

    One wording for the dry run and the real run, so the preview names the
    refusal the send would hit."""
    allowed = SCHEME_FORMATS[scheme_name]
    if format_name in allowed:
        return None
    return (
        f"target {target_name!r} is configured with format {format_name!r}; "
        f"the {scheme_name} scheme authenticates {' or '.join(allowed)} requests. "
        f"Set format = \"{allowed[0]}\" on the target."
    )


def configured_format_mismatch(
    scheme_name: str, target_name: Optional[str], config_path: Optional[str]
) -> Optional[str]:
    """The format refusal a real run against `target_name` would hit, or None.

    Checked before anything is announced, by the dry run as well as the send,
    so a preview never looks fine for a command `--send` refuses. A dry run
    does not need the target to exist, so a missing config file, an unknown
    target or no target at all is not reported here (a real run refuses those
    when it resolves the target): only a target the config does declare, in a
    format the scheme cannot authenticate."""
    from ..core.config import ConfigError, load_config

    if not target_name or not config_path or not Path(config_path).exists():
        return None
    try:
        target = load_config(config_path).get(target_name)
    except ConfigError:
        return None
    return format_mismatch(scheme_name, target.name, target.wire_format)


def execute(
    *,
    scheme_name: str,
    secret: Any,
    secret_ref: Optional[str],
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    window: float = DEFAULT_WINDOW,
    allow_public_target: bool = False,
    client: Any = None,
    clock: Callable[[], float] = time.time,
    printer: Callable[[str], None] = print,
) -> int:
    """Send every applicable variant once, in order, and judge each answer."""
    from ..core import guardrails
    from ..core.targets import resolve_target
    from ..core.transport import post

    try:
        scheme = get_scheme(scheme_name)
        target = resolve_target(target_name, config_path, allow_public_target=allow_public_target)
        mismatch = format_mismatch(scheme.name, target.name, target.format_name)
        if mismatch is not None:
            raise SigcheckError(mismatch)
        guardrails.require_synthetic_content(
            [sample_payload().to, sample_payload().from_addr]
        )
        if scheme.name == "sendgrid":
            SendgridScheme._key(secret)
        encode, wire_format_for = _encoder(scheme.name, target.wire_format)
        now = int(clock())
        planned = plan(scheme, secret, now=now, window=window, encode=encode)
    except Exception as exc:  # noqa: BLE001 - every setup failure is a refusal
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    config = run_config(scheme.name, target.name, target.format_name, window, secret_ref,
                        False, target.public)
    outcomes: List[Outcome] = []
    aborted = False
    try:
        for item in planned:
            if item.expect == EXPECT_NA:
                outcomes.append(judge(item, None, None))
                continue
            fixed = item.fixed_headers if item.fixed_headers is not None else item.signed.headers
            result = post(
                item.payload, target.url, client=client,
                wire_format=wire_format_for(item.signed),
                extra_headers=(lambda _body, h=dict(fixed): dict(h)) if fixed else None,
            )
            outcomes.append(judge(item, result.status, result.error))
    except KeyboardInterrupt:
        aborted = True

    notes = notes_for(outcomes, scheme.name)
    artifact = build_artifact(config, outcomes, notes, aborted=aborted)
    printer(format_results(artifact))
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    if aborted or artifact["summary"]["findings"]:
        return EXIT_FINDING
    return EXIT_OK
