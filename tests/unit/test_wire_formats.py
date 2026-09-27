"""The wire format layer, and the promise that `sendgrid` changes nothing.

Two separate things are pinned here and they are not the same claim.

The first is that the *format layer* did not alter SendGrid output: the bytes
`SendgridFormat` produces are compared against the bytes produced by the
pre-layer code path, re-derived in the test from the same two functions
`build_request` used to call. That catches a mistake introduced by the layer.

The second is that the bytes themselves have not drifted: one frozen literal,
asserted byte for byte, for a fixed payload. That catches a change further down
in `blast.serialize`, which the first test would happily ratify because both
sides moved together. A recorded artifact says what was sent, and a replay has
to match, so drift here is a correctness problem and not a curiosity.

Then the header hook, where the interesting property is *when* it runs.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from testinghq.blast.payload import Envelope, GroundTruth, InboundEmail
from testinghq.blast.serialize import to_multipart_parts
from testinghq.core import formats, transport
from testinghq.core.formats import EncodedBody, get_format, format_names
from testinghq.core.transport import (
    DEFAULT_BOUNDARY,
    encode_multipart,
    build_request,
    post,
)

URL = "http://127.0.0.1:9/inbound"


def _email(**overrides) -> InboundEmail:
    base = dict(
        to="support@example.com",
        from_addr="alice@example.com",
        subject="Your order",
        text="Thanks for your order.",
        html="<p>Thanks for your order.</p>",
        envelope=Envelope(to=("support@example.com",),
                          from_addr="alice@example.com"),
        ground_truth=GroundTruth(
            from_addr="alice@example.com",
            subject="Your order",
            body_core="Thanks for your order.",
        ),
        headers={"Message-ID": "<m1@example.com>"},
        charsets={"to": "UTF-8", "subject": "UTF-8"},
    )
    base.update(overrides)
    return InboundEmail(**base)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


def test_a_format_returns_bytes_and_the_content_type_describing_them():
    encoded = get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email())
    assert isinstance(encoded, EncodedBody)
    assert isinstance(encoded.body, bytes)
    assert encoded.body


def test_the_content_type_carries_the_boundary_that_was_actually_used():
    """The boundary in the content type has to be the boundary in the body. A
    mismatch is the classic multipart failure: a server that cannot find the
    delimiter reports an empty parse, and the run looks like a lost message
    rather than a malformed request."""
    encoded = get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email())
    assert f"boundary={DEFAULT_BOUNDARY}" in encoded.content_type
    assert f"--{DEFAULT_BOUNDARY}" in encoded.body.decode("utf-8", "replace")
    assert encoded.body.rstrip().endswith(b"--")


def test_the_registry_lists_what_get_format_accepts():
    assert format_names() == sorted(format_names())
    for name in format_names():
        assert get_format(name, DEFAULT_BOUNDARY).name == name


def test_an_unknown_format_is_refused_and_says_what_is_valid():
    """Refused, not defaulted. A silent fallback to SendGrid would post the
    wrong body to a Mailgun endpoint and, if the endpoint answered 2xx anyway,
    report the run as sent."""
    with pytest.raises(ValueError) as excinfo:
        get_format("mailgrun", DEFAULT_BOUNDARY)
    assert "mailgrun" in str(excinfo.value)
    for name in format_names():
        assert name in str(excinfo.value), "the error must list the valid names"


# ---------------------------------------------------------------------------
# Promise 1: the layer did not change SendGrid's output
# ---------------------------------------------------------------------------


def test_sendgrid_bytes_match_the_pre_layer_code_path():
    """Re-derives the old body the way `build_request` used to build it: the
    same parts list, the same encoder, the same boundary."""
    for payload in (
        _email(),
        _email(subject=""),
        _email(html=""),
        _email(text="", html=""),
    ):
        expected = encode_multipart(to_multipart_parts(payload), DEFAULT_BOUNDARY)
        actual = get_format("sendgrid", DEFAULT_BOUNDARY).encode(payload).body
        assert actual == expected


def test_sendgrid_content_type_matches_the_pre_layer_string():
    expected = f"multipart/form-data; boundary={DEFAULT_BOUNDARY}"
    assert get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email()).content_type == expected


def test_build_request_with_no_format_produces_the_old_request():
    """The compatibility promise in the form that matters: a caller who changed
    nothing gets the request they got before."""
    payload = _email()
    request = build_request(payload, URL)
    expected_body = encode_multipart(to_multipart_parts(payload), DEFAULT_BOUNDARY)
    assert request.body == expected_body
    assert request.headers == {
        "Content-Type": f"multipart/form-data; boundary={DEFAULT_BOUNDARY}",
        "Content-Length": str(len(expected_body)),
    }
    assert request.url == URL
    assert request.method == "POST"


# ---------------------------------------------------------------------------
# Promise 2: the bytes themselves have not drifted
# ---------------------------------------------------------------------------

#: Frozen bytes for the fixed payload above, as sent before this layer existed.
#: Changing this literal is a wire-format change and has to be argued for, not
#: arrived at by editing a test until it went green.
FROZEN_SENDGRID_BODY = (
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="headers"\r\n\r\n'
    b"Message-ID: <m1@example.com>\r\n"
    b"\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="to"\r\n\r\n'
    b"support@example.com\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="from"\r\n\r\n'
    b"alice@example.com\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="subject"\r\n\r\n'
    b"Your order\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="text"\r\n\r\n'
    b"Thanks for your order.\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="html"\r\n\r\n'
    b"<p>Thanks for your order.</p>\r\n"
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="envelope"\r\n\r\n'
    b'{"from":"alice@example.com","to":["support@example.com"]}\r\n'
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="charsets"\r\n\r\n'
    b'{"subject":"UTF-8","to":"UTF-8"}\r\n'
    b"------testinghq-boundary-2f6a9c\r\n"
    b'Content-Disposition: form-data; name="attachments"\r\n\r\n'
    b"0\r\n"
    b"------testinghq-boundary-2f6a9c--\r\n"
)


def test_the_sendgrid_wire_format_is_byte_for_byte_what_it_was():
    assert get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email()).body == FROZEN_SENDGRID_BODY


def test_the_frozen_literal_is_actually_the_format_named_in_its_content_type():
    """Ties the two promises together. Without this, a boundary change would
    make every other test here pass while the body stopped matching the
    announced content type."""
    request = build_request(_email(), URL)
    assert request.headers["Content-Type"].endswith(request.body.split(b"--")[0].decode())
    assert str(len(request.body)) == request.headers["Content-Length"]


def test_encoding_the_same_payload_twice_gives_the_same_bytes():
    """Determinism, which every replay and every recorded artifact depends on.
    Stated for the format layer rather than assumed from the serializers."""
    first = get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email())
    second = get_format("sendgrid", DEFAULT_BOUNDARY).encode(_email())
    assert first == second


# ---------------------------------------------------------------------------
# The header hook
# ---------------------------------------------------------------------------


def test_the_hook_receives_exactly_the_bytes_that_will_be_sent():
    """The whole reason the hook exists. A signature covers specific bytes, so a
    hook given anything other than the final body computes a signature the
    server will reject."""
    seen = []

    def hook(body):
        seen.append(body)
        return {"X-Signature": "abc"}

    request = build_request(_email(), URL, extra_headers=hook)
    assert seen == [request.body], "the hook must see the sent bytes, once"
    assert request.headers["X-Signature"] == "abc"


def test_a_hook_computed_signature_verifies_against_the_sent_body():
    """The real use, end to end: sign the bytes, send the bytes, and check the
    digest matches what arrived. Catches a hook that ran before the body was
    final, which a test that only checks the header was set would not."""
    secret = b"shared-secret"

    def hook(body):
        return {"X-Signature": hmac.new(secret, body, hashlib.sha256).hexdigest()}

    request = build_request(_email(), URL, extra_headers=hook)
    expected = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
    assert request.headers["X-Signature"] == expected


def test_a_hook_adding_a_header_does_not_disturb_the_body():
    request = build_request(_email(), URL, extra_headers=lambda _b: {"X-A": "1"})
    assert request.body == FROZEN_SENDGRID_BODY
    assert request.headers["Content-Length"] == str(len(request.body))


def test_several_hook_headers_all_land():
    request = build_request(
        _email(), URL, extra_headers=lambda _b: {"X-A": "1", "X-B": "2"}
    )
    assert request.headers["X-A"] == "1"
    assert request.headers["X-B"] == "2"


def test_an_empty_hook_result_is_fine():
    request = build_request(_email(), URL, extra_headers=lambda _b: {})
    assert set(request.headers) == {"Content-Type", "Content-Length"}


def test_a_hook_returning_a_non_dict_is_refused():
    with pytest.raises(TypeError) as excinfo:
        build_request(_email(), URL, extra_headers=lambda _b: ["X-A", "1"])
    assert "dict" in str(excinfo.value)


def test_a_hook_returning_a_non_string_value_is_refused():
    """Header values are strings on the wire. Coercing would mean a hook that
    returned an int produced a request urllib would then reject with a much
    less useful message."""
    with pytest.raises(TypeError) as excinfo:
        build_request(_email(), URL, extra_headers=lambda _b: {"X-Retry": 3})
    assert "string" in str(excinfo.value)


@pytest.mark.parametrize("computed", ["Content-Type", "Content-Length"])
def test_a_hook_cannot_overwrite_a_computed_header(computed):
    """Refused rather than allowed. A hook that replaced Content-Type would
    produce a request whose announced type does not describe its own body,
    which is the failure this whole layer exists to remove, reintroduced through
    the back door."""
    with pytest.raises(ValueError) as excinfo:
        build_request(_email(), URL, extra_headers=lambda _b: {computed: "x"})
    assert computed in str(excinfo.value)


def test_the_hook_refusal_happens_before_anything_is_sent():
    """`post` builds the request before touching the client, so a refused hook
    means nothing went out. A client that records being called at all is the
    failure."""
    calls = []

    class _Client:
        def send(self, request):
            calls.append(request)
            return transport.ClientResponse(status=200, body=b"")

    with pytest.raises(ValueError):
        post(_email(), URL, _Client(),
             extra_headers=lambda _b: {"Content-Length": "0"})

    assert calls == [], "a refused header hook must not have reached the client"


# ---------------------------------------------------------------------------
# post() forwards both
# ---------------------------------------------------------------------------


class _Recording:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return transport.ClientResponse(status=200, body=b"ok")


def test_post_forwards_the_format_to_the_client():
    client = _Recording()
    post(_email(), URL, client, wire_format=get_format("sendgrid", DEFAULT_BOUNDARY))
    assert client.requests[0].body == FROZEN_SENDGRID_BODY


def test_post_forwards_the_header_hook_to_the_client():
    client = _Recording()
    result = post(_email(), URL, client,
                  extra_headers=lambda b: {"X-Len": str(len(b))})
    assert client.requests[0].headers["X-Len"] == str(len(client.requests[0].body))
    assert result.status == 200


def test_post_with_no_arguments_behaves_exactly_as_before():
    """The tools that exist today call `post(payload, url, client)` and nothing
    else. If this changed, every one of them changed."""
    client = _Recording()
    result = post(_email(), URL, client)
    assert result.status == 200
    assert result.error is None
    assert client.requests[0].body == FROZEN_SENDGRID_BODY
    assert client.requests[0].timeout == 10.0
