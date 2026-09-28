"""sigcheck: the signing schemes, and the four variants a check must reject.

A provider attaches a signature to a webhook. The question is whether your
endpoint checks it, and four requests per provider is enough to find out:
correctly signed, signed with the wrong secret, unsigned, and correctly signed
with a timestamp outside the allowed window. Only the first should be accepted.

WHAT IS AND IS NOT IN HERE. The HMAC schemes are implemented in full on the
standard library. The asymmetric scheme (SendGrid, ECDSA over P-256) is not,
because the standard library has no ECDSA and this package has no runtime
dependencies by design. Its *contract* is implemented: which headers carry the
signature and the timestamp, what exact bytes are covered, how the signature is
encoded, and what a stale timestamp means. The signing and verifying primitives
are injected, and asking to verify without one raises a `SigcheckError` that
names the alternatives rather than returning a verdict nobody computed.

That is a design decision, not a gap worked around, and the send path in the
next PR is where the owner decides whether the package takes an optional
dependency. Flagged in that PR too.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

#: The four requests, and which of them a correct endpoint accepts. The names
#: are the report's vocabulary.
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

#: Default seconds of clock skew allowed before a request is stale. Generous,
#: because a receiver whose clock is right should not refuse a provider's, and
#: tight enough that a captured request cannot be replayed days later.
DEFAULT_TOLERANCE = 300.0


class SigcheckError(ValueError):
    """A malformed scheme configuration, or a scheme asked to do something its
    configured primitives cannot. A caller error, never a finding."""


@dataclass(frozen=True)
class VerifyResult:
    """One verification, and why it went the way it did.

    `reason` is what a report needs, because "invalid" and "valid signature, but
    the timestamp is forty minutes old" are different findings with different
    fixes: the first is an open endpoint, the second is a replay window that is
    too wide or a clock that is wrong.
    """

    accepted: bool
    reason: str
    stale: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason, "stale": self.stale}


class SignatureScheme(Protocol):
    """What a provider's signing scheme has to be able to do."""

    name: str

    def sign_headers(
        self, body: bytes, secret: str, timestamp: int
    ) -> Dict[str, str]: ...

    def verify(
        self,
        body: bytes,
        secret: str,
        headers: Dict[str, str],
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult: ...


def _encode_hex(digest: bytes) -> str:
    return digest.hex()


def _encode_base64(digest: bytes) -> str:
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class HmacScheme:
    """An HMAC-SHA256 scheme, which is most of them.

    The providers differ in what they sign and in how they render the result,
    not in the primitive, so they differ in two callables and this class holds
    the parts that are the same.
    """

    name: str
    signature_header: str
    #: The exact bytes to authenticate. Receives the body and the timestamp
    #: (None for a scheme with none) and returns the message to HMAC.
    signing_string: Callable[[bytes, Optional[int]], bytes]
    encode: Callable[[bytes], str]
    #: Name of a header carrying the timestamp on its own, or None. Mailgun has
    #: no such header: its timestamp is the `t=` inside `X-Signature`, which is
    #: why this is separate from `compound_header`.
    timestamp_header: Optional[str] = None
    #: True when the signature header carries `t=<timestamp>,v1=<signature>`.
    #: Reading such a value as the bare signature made a header this class had
    #: just produced fail to verify, which is the kind of self-inconsistency
    #: that only a round trip catches.
    compound_header: bool = False
    #: True when `encode` produces hex. Stated rather than inferred from a probe
    #: value, so the decoder cannot disagree with the encoder.
    hex_encoded: bool = False

    @property
    def carries_timestamp(self) -> bool:
        """Whether this scheme has a timestamp at all, and therefore whether a
        missing or unusable one is a staleness finding.

        Keyed on either mechanism rather than on `timestamp_header` alone: a
        compound scheme carries its timestamp inside the signature header, so
        asking only about the separate header let a junk `t=` fall through to
        the signature comparison and get reported as an invalid signature
        instead of as the replay problem it is.
        """
        return self.compound_header or self.timestamp_header is not None

    def _timestamp(self, headers: Dict[str, str]) -> Optional[int]:
        if self.compound_header:
            raw = self._extract_timestamp(headers.get(self.signature_header, ""))
        elif self.timestamp_header is not None:
            raw = headers.get(self.timestamp_header)
        else:
            return None
        if raw is None:
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    @staticmethod
    def _extract_timestamp(raw: str) -> Optional[str]:
        for part in (raw or "").split(","):
            key, sep, value = part.strip().partition("=")
            if sep and key.strip() == "t":
                return value.strip()
        return None

    def sign_headers(
        self, body: bytes, secret: str, timestamp: int
    ) -> Dict[str, str]:
        digest = hmac.new(
            secret.encode("utf-8"),
            self.signing_string(body, timestamp),
            hashlib.sha256,
        ).digest()
        signature = self.encode(digest)
        headers = {
            self.signature_header: (
                f"t={timestamp},v1={signature}" if self.compound_header else signature
            )
        }
        if self.timestamp_header is not None and not self.compound_header:
            headers[self.timestamp_header] = str(timestamp)
        return headers

    def verify(
        self,
        body: bytes,
        secret: str,
        headers: Dict[str, str],
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult:
        raw = headers.get(self.signature_header)
        if raw is None:
            return VerifyResult(
                False, f"no {self.signature_header} header, so nothing was verified"
            )

        timestamp = self._timestamp(headers)
        if self.carries_timestamp and timestamp is None:
            return VerifyResult(
                False,
                f"no usable timestamp in {self.signature_header}, so the request "
                f"cannot be checked for staleness",
                stale=True,
            )

        presented = self._extract(raw)
        if presented is None:
            return VerifyResult(
                False, f"{self.signature_header} carries no recognisable signature"
            )

        if timestamp is not None and abs(now - timestamp) > tolerance:
            return VerifyResult(
                False,
                f"the signature is well formed but the timestamp is "
                f"{abs(now - timestamp)}s from now, outside the {tolerance:g}s "
                f"window, so a captured request could be replayed",
                stale=True,
            )

        try:
            presented_bytes = bytes.fromhex(presented) if self.hex_encoded else (
                base64.b64decode(presented, validate=True)
            )
        except (binascii.Error, ValueError):
            return VerifyResult(
                False, f"{self.signature_header} is not a well-formed signature"
            )

        expected = hmac.new(
            secret.encode("utf-8"),
            self.signing_string(body, timestamp),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(presented_bytes, expected):
            return VerifyResult(
                False, "the signature does not match the body and secret"
            )
        return VerifyResult(True, f"signature valid over {len(body)} byte(s)")

    def _extract(self, raw: str) -> Optional[str]:
        """Pull the signature out of the header value.

        Only a compound header needs this, and reading a whole compound value as
        the bare signature is what made a header this class had just produced
        fail to verify.
        """
        if not self.compound_header:
            return raw.strip()
        for part in (raw or "").split(","):
            key, sep, value = part.strip().partition("=")
            if sep and key.strip() == "v1":
                return value.strip()
        return None


def _mailgun_signing_string(body: bytes, timestamp: Optional[int]) -> bytes:
    """Mailgun authenticates the timestamp concatenated with the body.

    The order is the whole of it. Signing the body alone passes a test that
    computes the same wrong thing and fails against the real provider, so it is
    spelled out here rather than left to the reader of a comment.
    """
    return f"{timestamp}{body.decode('utf-8', 'replace')}".encode("utf-8")


def _postmark_signing_string(body: bytes, timestamp: Optional[int]) -> bytes:
    """Postmark authenticates the body alone. It carries no timestamp header, so
    it has no replay window of its own and relies on the transport."""
    return body


#: The pluggable table. A new provider is a row, not a new mechanism.
SCHEMES: Dict[str, SignatureScheme] = {
    "mailgun": HmacScheme(
        name="mailgun",
        signature_header="X-Signature",
        timestamp_header=None,
        compound_header=True,
        signing_string=_mailgun_signing_string,
        encode=_encode_hex,
        hex_encoded=True,
    ),
    "postmark": HmacScheme(
        name="postmark",
        signature_header="X-Postmark-Signature",
        timestamp_header=None,
        signing_string=_postmark_signing_string,
        encode=_encode_base64,
        hex_encoded=False,
    ),
}


#: The asymmetric scheme's headers, named here rather than inside the class so
#: the report and the send path can name them without instantiating anything.
SENDGRID_SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
SENDGRID_TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"


@dataclass(frozen=True)
class AsymmetricScheme:
    """A scheme whose signature is public-key rather than secret-key.

    The contract is here and the primitive is injected, because the standard
    library has no ECDSA and this package has no dependencies. `signer` is
    `(body, key_material) -> raw signature` and `verifier` is
    `(body, signature, key_material) -> bool`.
    """

    name: str
    signature_header: str = SENDGRID_SIGNATURE_HEADER
    timestamp_header: str = SENDGRID_TIMESTAMP_HEADER
    signer: Optional[Callable[[bytes, Any], bytes]] = None
    verifier: Optional[Callable[[bytes, bytes, Any], bool]] = None
    encode: Callable[[bytes], str] = _encode_base64

    def _no_primitive(self, what: str) -> SigcheckError:
        return SigcheckError(
            f"the {self.name} scheme is asymmetric and this package ships no "
            f"{what} primitive for it, because it has no runtime dependencies "
            f"and the standard library has none either. Pass "
            f"{what}=, or use one of the HMAC schemes: {sorted(SCHEMES)}"
        )

    def sign_headers(
        self, body: bytes, secret: str, timestamp: int
    ) -> Dict[str, str]:
        if self.signer is None:
            raise self._no_primitive("signer")
        return {
            self.signature_header: self.encode(self.signer(body, secret)),
            self.timestamp_header: str(timestamp),
        }

    def verify(
        self,
        body: bytes,
        secret: str,
        headers: Dict[str, str],
        *,
        now: int,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyResult:
        raw = headers.get(self.signature_header)
        if raw is None:
            return VerifyResult(
                False, f"no {self.signature_header} header, so nothing was verified"
            )
        if self.verifier is None:
            raise self._no_primitive("verifier")
        try:
            signature = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return VerifyResult(
                False, f"{self.signature_header} is not base64, so not an "
                "ECDSA signature"
            )

        raw_timestamp = headers.get(self.timestamp_header)
        if raw_timestamp is None:
            return VerifyResult(
                False,
                f"no {self.timestamp_header} header, so the request cannot be "
                f"checked for staleness",
                stale=True,
            )
        try:
            age = abs(now - int(str(raw_timestamp).strip()))
        except ValueError:
            return VerifyResult(
                False, f"{self.timestamp_header} is not a unix timestamp", stale=True
            )
        if age > tolerance:
            return VerifyResult(
                False,
                f"the signature is well formed but the timestamp is {age}s from "
                f"now, outside the {tolerance:g}s window",
                stale=True,
            )
        if not self.verifier(body, signature, secret):
            return VerifyResult(
                False, "the signature does not verify against the key"
            )
        return VerifyResult(True, f"signature verified over {len(body)} byte(s)")


def known_schemes() -> Tuple[str, ...]:
    return tuple(sorted(set(SCHEMES) | {"sendgrid"}))


def get_scheme(name: str, **kwargs) -> SignatureScheme:
    """The named scheme, from the pluggable table.

    An unknown name is a caller error rather than a fallback. Signing a webhook
    with the wrong algorithm produces a request the provider's real receiver
    would reject, so a test that fell back to a default would report the
    opposite of the truth.

    `kwargs` go to the scheme's constructor, which is how `signer=` and
    `verifier=` reach the asymmetric one.
    """
    if name == "sendgrid":
        return AsymmetricScheme(name="sendgrid", **kwargs)
    scheme = SCHEMES.get(name)
    if scheme is None:
        raise SigcheckError(
            f"unknown signature scheme {name!r}; known: {list(known_schemes())}"
        )
    if kwargs:
        raise SigcheckError(
            f"the {name!r} scheme takes its signing string and encoding from the "
            f"table and cannot be given {sorted(kwargs)}"
        )
    return scheme


# ---------------------------------------------------------------------------
# The four variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One request to send, and whether a correct endpoint accepts it."""

    name: str
    expected_accepted: bool
    headers: Dict[str, str] = field(default_factory=dict)
    note: str = ""


def _carries_timestamp(scheme: SignatureScheme) -> bool:
    """Whether `scheme` has a timestamp in its signature, however it is
    spelled. `HmacScheme` knows for itself; the asymmetric one always does."""
    getter = getattr(scheme, "carries_timestamp", None)
    if isinstance(getter, bool):
        return getter
    return getattr(scheme, "timestamp_header", None) is not None


def build_variants(
    scheme: SignatureScheme,
    body: bytes,
    secret: str,
    *,
    timestamp: int,
    now: int,
    tolerance: float = DEFAULT_TOLERANCE,
    wrong_secret: str = "not-the-secret",
) -> List[Variant]:
    """The four requests, all derived from one body and one timestamp.

    `wrong_secret` defaults to a literal rather than a random value, and `now`
    is a parameter rather than a clock read, so a run is reproducible. The
    replayed variant is signed correctly and then dated outside the window, which
    is the case a receiver with no timestamp check cannot tell from a live
    request.

    A scheme that carries no timestamp cannot express that variant: its replayed
    request would be byte-identical to the correct one, so the "variant" would
    test nothing while reporting that it had. That raises rather than emitting a
    duplicate, and the reason names the real situation, which is that such a
    scheme's replay protection is the transport's and not the signature's.
    """
    if not _carries_timestamp(scheme):
        raise SigcheckError(
            f"the {scheme.name!r} scheme carries no timestamp, so a replayed "
            "request is byte-identical to a live one and the replay variant "
            "would test nothing. That scheme's replay protection is the "
            "transport's, not its signature's, and testing it means testing the "
            "transport"
        )
    stale_timestamp = now - int(tolerance) - 60
    return [
        Variant(
            name=VARIANT_CORRECT,
            expected_accepted=True,
            headers=scheme.sign_headers(body, secret, timestamp),
            note="signed with the operator's secret, inside the window",
        ),
        Variant(
            name=VARIANT_WRONG_SECRET,
            expected_accepted=False,
            headers=scheme.sign_headers(body, wrong_secret, timestamp),
            note="well formed, signed with a different secret",
        ),
        Variant(
            name=VARIANT_UNSIGNED,
            expected_accepted=False,
            headers={},
            note="no signature headers at all",
        ),
        Variant(
            name=VARIANT_REPLAYED,
            expected_accepted=False,
            headers=scheme.sign_headers(body, secret, stale_timestamp),
            note=(
                f"signed correctly, dated {abs(now - stale_timestamp)}s from now, "
                f"outside the {tolerance:g}s window"
            ),
        ),
    ]


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
    secret: str,
    *,
    timestamp: int,
    now: int,
    tolerance: float = DEFAULT_TOLERANCE,
    wrong_secret: str = "not-the-secret",
) -> List[VariantOutcome]:
    """Build the four and verify each one.

    Verifying here rather than sending is what makes this part land before the
    send path: the scheme is decided now, and the send path only has to deliver
    these headers. It also means the expected answer for every variant is known
    before anything is sent, so a run can say what a correct endpoint would have
    done rather than inferring it afterwards.
    """
    outcomes = []
    for variant in build_variants(
        scheme, body, secret,
        timestamp=timestamp, now=now, tolerance=tolerance, wrong_secret=wrong_secret,
    ):
        result = scheme.verify(
            body, secret, variant.headers, now=now, tolerance=tolerance
        )
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
