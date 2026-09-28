"""sigcheck: the signing schemes, and the requests a checking endpoint must reject.

A provider authenticates a webhook. The question is whether your endpoint checks
it. Up to four requests per scheme answer that: correctly signed, signed with the
wrong secret, unsigned, and correctly signed with a timestamp outside the
allowed window. Only the first should be accepted.

The schemes, each from the provider's own documentation:

  mailgun     HMAC-SHA256 over `timestamp + token` with the webhook signing key,
              hex encoded, sent as the `timestamp`, `token` and `signature`
              fields in the request body. The body itself is not signed.
              https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http.md
  sendgrid    ECDSA (P-256, SHA-256) over `timestamp + raw body`, sent in the
              X-Twilio-Email-Event-Webhook-Signature and -Timestamp headers.
              Opt-in for Inbound Parse through a webhook security policy.
              https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/securing-your-parse-webhooks
              The standard library has no ECDSA and this package has no runtime
              dependencies, so the sign and verify primitives are injected.
  basic-auth  HTTP Basic credentials on the request. Postmark does not sign its
              webhooks; it relies on Basic auth in the webhook URL (and IP
              allowlisting). SendGrid Inbound Parse without a security policy is
              in the same position. A scheme with no timestamp has no replay
              variant, because a replayed request would be byte-identical to a
              live one.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

#: The requests, and which of them a correct endpoint accepts. The names are
#: the report's vocabulary.
VARIANT_CORRECT = "correctly-signed"
VARIANT_WRONG_SECRET = "wrong-secret"
VARIANT_UNSIGNED = "unsigned"
VARIANT_REPLAYED = "replayed-outside-window"
VARIANTS: Tuple[str, ...] = (
    VARIANT_CORRECT,
    VARIANT_WRONG_SECRET,
    VARIANT_UNSIGNED,
    VARIANT_REPLAYED,
)
ACCEPTED_VARIANT = VARIANT_CORRECT

#: Default seconds of clock skew allowed before a request is stale.
DEFAULT_TOLERANCE = 300.0

SENDGRID_SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
SENDGRID_TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"

MAILGUN_FIELDS = ("timestamp", "token", "signature")


class SigcheckError(ValueError):
    """A malformed scheme configuration, or a scheme asked to do something its
    configured primitives cannot. A caller error, never a finding."""


@dataclass(frozen=True)
class Signed:
    """What a scheme adds to a request: headers, body form fields, or both.

    Mailgun authenticates through body fields and SendGrid through headers, so
    a scheme returns both kinds and the send path puts each where it belongs.
    """

    headers: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.headers and not self.fields


@dataclass(frozen=True)
class VerifyResult:
    """One verification, and why it went the way it did. `stale` separates a
    bad signature (open endpoint) from an old timestamp (replay window)."""

    accepted: bool
    reason: str
    stale: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason, "stale": self.stale}


class SignatureScheme(Protocol):
    name: str
    carries_timestamp: bool

    def sign(self, body: bytes, secret: str, timestamp: int) -> Signed: ...

    def verify(
        self,
        body: bytes,
        secret: str,
        signed: Signed,
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult: ...


def _stale(timestamp: int, now: int, tolerance: float) -> Optional[VerifyResult]:
    age = abs(now - timestamp)
    if age > tolerance:
        return VerifyResult(
            False,
            f"the signature is well formed but the timestamp is {age}s from now, "
            f"outside the {tolerance:g}s window, so a captured request could be "
            f"replayed",
            stale=True,
        )
    return None


# ---------------------------------------------------------------------------
# mailgun
# ---------------------------------------------------------------------------


def mailgun_token(timestamp: int) -> str:
    """A deterministic 50-character token, the length Mailgun sends, so a run
    is reproducible. Mailgun's own tokens are random; the receiver only needs
    the string it was handed."""
    return hashlib.sha256(f"testinghq-mailgun-{timestamp}".encode("ascii")).hexdigest()[:50]


def mailgun_signature(signing_key: str, timestamp: str, token: str) -> str:
    """HMAC-SHA256 of `timestamp + token` (no separator), keyed with the
    webhook signing key, hex encoded. This is the whole scheme."""
    return hmac.new(
        signing_key.encode("utf-8"),
        f"{timestamp}{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class MailgunScheme:
    name: str = "mailgun"
    carries_timestamp: bool = True

    def sign(self, body: bytes, secret: str, timestamp: int) -> Signed:
        token = mailgun_token(timestamp)
        return Signed(
            fields={
                "timestamp": str(timestamp),
                "token": token,
                "signature": mailgun_signature(secret, str(timestamp), token),
            }
        )

    def verify(
        self,
        body: bytes,
        secret: str,
        signed: Signed,
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult:
        missing = [name for name in MAILGUN_FIELDS if name not in signed.fields]
        if missing:
            return VerifyResult(
                False, f"no {', '.join(missing)} field(s), so nothing was verified"
            )
        raw_timestamp = signed.fields["timestamp"]
        try:
            timestamp = int(raw_timestamp.strip())
        except ValueError:
            return VerifyResult(False, "the timestamp field is not a unix timestamp", stale=True)
        stale = _stale(timestamp, now, tolerance)
        if stale is not None:
            return stale
        expected = mailgun_signature(secret, raw_timestamp, signed.fields["token"])
        if not hmac.compare_digest(expected, signed.fields["signature"].strip().lower()):
            return VerifyResult(False, "the signature does not match the timestamp, token and key")
        return VerifyResult(
            True,
            "signature valid over timestamp and token (Mailgun does not sign the body)",
        )


# ---------------------------------------------------------------------------
# sendgrid (ECDSA, primitives injected)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendgridScheme:
    """ECDSA over `timestamp + body`. `signer(message, key) -> raw signature`
    and `verifier(message, signature, key) -> bool` are injected because the
    standard library has no ECDSA."""

    name: str = "sendgrid"
    carries_timestamp: bool = True
    signer: Optional[Callable[[bytes, Any], bytes]] = None
    verifier: Optional[Callable[[bytes, bytes, Any], bool]] = None

    @staticmethod
    def signed_message(body: bytes, timestamp: str) -> bytes:
        return timestamp.encode("ascii") + body

    def _no_primitive(self, what: str) -> SigcheckError:
        return SigcheckError(
            f"the sendgrid scheme is ECDSA and this package ships no {what} "
            f"primitive for it, because it has no runtime dependencies and the "
            f"standard library has none. Pass {what}=, or use one of: "
            f"{sorted(n for n in known_schemes() if n != 'sendgrid')}"
        )

    def sign(self, body: bytes, secret: Any, timestamp: int) -> Signed:
        if self.signer is None:
            raise self._no_primitive("signer")
        message = self.signed_message(body, str(timestamp))
        return Signed(
            headers={
                SENDGRID_SIGNATURE_HEADER: base64.b64encode(
                    self.signer(message, secret)
                ).decode("ascii"),
                SENDGRID_TIMESTAMP_HEADER: str(timestamp),
            }
        )

    def verify(
        self,
        body: bytes,
        secret: Any,
        signed: Signed,
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult:
        raw = signed.headers.get(SENDGRID_SIGNATURE_HEADER)
        if raw is None:
            return VerifyResult(
                False, f"no {SENDGRID_SIGNATURE_HEADER} header, so nothing was verified"
            )
        if self.verifier is None:
            raise self._no_primitive("verifier")
        try:
            signature = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return VerifyResult(
                False, f"{SENDGRID_SIGNATURE_HEADER} is not base64, so not an ECDSA signature"
            )
        raw_timestamp = signed.headers.get(SENDGRID_TIMESTAMP_HEADER)
        if raw_timestamp is None:
            return VerifyResult(
                False,
                f"no {SENDGRID_TIMESTAMP_HEADER} header, so the request cannot be "
                "checked for staleness",
                stale=True,
            )
        try:
            timestamp = int(str(raw_timestamp).strip())
        except ValueError:
            return VerifyResult(
                False, f"{SENDGRID_TIMESTAMP_HEADER} is not a unix timestamp", stale=True
            )
        stale = _stale(timestamp, now, tolerance)
        if stale is not None:
            return stale
        if not self.verifier(self.signed_message(body, str(raw_timestamp)), signature, secret):
            return VerifyResult(False, "the signature does not verify against the key")
        return VerifyResult(True, f"signature verified over {len(body)} byte(s)")


# ---------------------------------------------------------------------------
# basic-auth (Postmark, and SendGrid without a security policy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BasicAuthScheme:
    """HTTP Basic credentials. `secret` is `user:password`. No timestamp, so
    there is no replay variant: a replay would be byte-identical to a live
    request, and replay protection here is the transport's (TLS)."""

    name: str = "basic-auth"
    carries_timestamp: bool = False

    @staticmethod
    def _header(secret: str) -> str:
        if ":" not in secret:
            raise SigcheckError("basic-auth secret must be 'user:password'")
        return "Basic " + base64.b64encode(secret.encode("utf-8")).decode("ascii")

    def sign(self, body: bytes, secret: str, timestamp: int) -> Signed:
        return Signed(headers={"Authorization": self._header(secret)})

    def verify(
        self,
        body: bytes,
        secret: str,
        signed: Signed,
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult:
        presented = signed.headers.get("Authorization")
        if presented is None:
            return VerifyResult(False, "no Authorization header, so nothing was checked")
        if not hmac.compare_digest(presented, self._header(secret)):
            return VerifyResult(False, "the credentials do not match")
        return VerifyResult(True, "credentials match")


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_SCHEMES = {
    "mailgun": MailgunScheme,
    "sendgrid": SendgridScheme,
    "basic-auth": BasicAuthScheme,
}


def known_schemes() -> Tuple[str, ...]:
    return tuple(sorted(_SCHEMES))


def get_scheme(name: str, **kwargs) -> SignatureScheme:
    """The named scheme. An unknown name is refused rather than defaulted, and
    only `sendgrid` accepts injected primitives (`signer=`, `verifier=`)."""
    constructor = _SCHEMES.get(name)
    if constructor is None:
        raise SigcheckError(
            f"unknown signature scheme {name!r}; known: {list(known_schemes())}"
        )
    if kwargs and name != "sendgrid":
        raise SigcheckError(
            f"the {name!r} scheme takes no injected primitives, got {sorted(kwargs)}"
        )
    return constructor(**kwargs)


# ---------------------------------------------------------------------------
# The variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One request to send, and whether a correct endpoint accepts it."""

    name: str
    expected_accepted: bool
    signed: Signed = field(default_factory=Signed)
    note: str = ""


def build_variants(
    scheme: SignatureScheme,
    body: bytes,
    secret: Any,
    *,
    timestamp: int,
    now: int,
    tolerance: float = DEFAULT_TOLERANCE,
    wrong_secret: Any = "not-the-secret:not-the-password",
) -> List[Variant]:
    """The requests, all derived from one body and one timestamp. Deterministic:
    `now` is a parameter and `wrong_secret` a literal. The replay variant is
    only built for a scheme that carries a timestamp."""
    variants = [
        Variant(
            name=VARIANT_CORRECT,
            expected_accepted=True,
            signed=scheme.sign(body, secret, timestamp),
            note="authenticated with the operator's secret, inside the window",
        ),
        Variant(
            name=VARIANT_WRONG_SECRET,
            expected_accepted=False,
            signed=scheme.sign(body, wrong_secret, timestamp),
            note="well formed, made with a different secret",
        ),
        Variant(
            name=VARIANT_UNSIGNED,
            expected_accepted=False,
            signed=Signed(),
            note="no authentication at all",
        ),
    ]
    if scheme.carries_timestamp:
        stale_timestamp = now - int(tolerance) - 60
        variants.append(
            Variant(
                name=VARIANT_REPLAYED,
                expected_accepted=False,
                signed=scheme.sign(body, secret, stale_timestamp),
                note=(
                    f"authenticated correctly, dated {abs(now - stale_timestamp)}s "
                    f"from now, outside the {tolerance:g}s window"
                ),
            )
        )
    return variants


@dataclass(frozen=True)
class VariantOutcome:
    """What the endpoint did with one variant, against what it should do."""

    scheme: str
    variant: str
    expected_accepted: bool
    accepted: bool
    reason: str
    note: str = ""

    @property
    def correct(self) -> bool:
        return self.accepted == self.expected_accepted

    def to_json(self) -> Dict[str, Any]:
        return {
            "scheme": self.scheme,
            "variant": self.variant,
            "expected_accepted": self.expected_accepted,
            "accepted": self.accepted,
            "verdict": "OK" if self.correct else "FINDING",
            "reason": self.reason,
            "note": self.note,
        }


def check_variants(
    scheme: SignatureScheme,
    body: bytes,
    secret: Any,
    *,
    timestamp: int,
    now: int,
    tolerance: float = DEFAULT_TOLERANCE,
    wrong_secret: Any = "not-the-secret:not-the-password",
) -> List[VariantOutcome]:
    """Build the variants and verify each one locally. The send path (next PR)
    delivers them; this fixes the expected answer for every variant first."""
    outcomes = []
    for variant in build_variants(
        scheme, body, secret,
        timestamp=timestamp, now=now, tolerance=tolerance, wrong_secret=wrong_secret,
    ):
        result = scheme.verify(body, secret, variant.signed, now=now, tolerance=tolerance)
        outcomes.append(
            VariantOutcome(
                scheme=scheme.name,
                variant=variant.name,
                expected_accepted=variant.expected_accepted,
                accepted=result.accepted,
                reason=result.reason,
                note=variant.note,
            )
        )
    return outcomes
