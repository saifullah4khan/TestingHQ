"""`transport.send` and the SMTP half of it, against fake clients.

No socket is opened here: the SMTP clients are driven through a fake
`smtplib.SMTP` factory, and `send` is given fake HTTP and SMTP clients. The
real socket path is in tests/integration/test_smtp_transport.py.
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from testinghq.blast.generate import generate_corpus
from testinghq.core import report
from testinghq.core.formats import get_format
from testinghq.core.transport import (
    DEFAULT_BOUNDARY,
    EHLO_NAME,
    ClientResponse,
    HttpDestination,
    PersistentSmtpClient,
    SmtpEndpoint,
    SmtplibClient,
    SmtpReply,
    build_request,
    build_smtp_request,
    send,
    send_smtp,
    with_envelope_to,
)

ENDPOINT = SmtpEndpoint(host="127.0.0.1", port=2525)
MIME = get_format("mime", DEFAULT_BOUNDARY)


def _email():
    return generate_corpus(3, 1)[0]


class _Smtp:
    """A destination as `send` sees it, for an SMTP target."""

    transport = "smtp"
    url = "smtp://127.0.0.1:2525"
    headers: dict = {}

    def __init__(self, endpoint=ENDPOINT):
        self.smtp = endpoint
        self.wire_format = MIME


class _RecordingSmtp:
    def __init__(self, reply=SmtpReply(250, "2.0.0 Ok: queued as A1"), raises=None):
        self.reply, self.raises, self.requests = reply, raises, []

    def send(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.reply


class _RecordingHttp:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=202, body=b"queued")


# ---------------------------------------------------------------------------
# The message and the envelope
# ---------------------------------------------------------------------------


def test_the_message_is_exactly_what_the_mime_format_builds():
    email = _email()
    request = build_smtp_request(email, ENDPOINT)
    assert request.message == MIME.encode(email).body
    parsed = message_from_bytes(request.message, policy=default_policy)
    assert parsed["Subject"] == email.subject


def test_the_envelope_defaults_to_the_payloads_own():
    email = _email()
    request = build_smtp_request(email, ENDPOINT)
    assert request.envelope_from == email.envelope.from_addr
    assert request.envelope_to == tuple(email.envelope.to)


def test_the_target_envelope_overrides_the_payloads():
    endpoint = SmtpEndpoint(host="127.0.0.1", envelope_from="bounce@sender.test",
                            envelope_to=("desk@helpdesk.test",))
    request = build_smtp_request(_email(), endpoint)
    assert request.envelope_from == "bounce@sender.test"
    assert request.envelope_to == ("desk@helpdesk.test",)


def test_an_empty_envelope_sender_stays_a_null_reverse_path():
    import dataclasses

    email = _email()
    bounce = dataclasses.replace(email, envelope=dataclasses.replace(email.envelope, from_addr=""))
    assert build_smtp_request(bounce, ENDPOINT).envelope_from == ""


def test_with_no_envelope_recipient_the_to_header_is_used():
    import dataclasses

    email = _email()
    bare = dataclasses.replace(email, envelope=dataclasses.replace(email.envelope, to=()))
    assert build_smtp_request(bare, ENDPOINT).envelope_to == (
        report_address(email.to),
    )


def report_address(value):
    from email.utils import getaddresses

    return getaddresses([value])[0][1]


def test_a_payload_with_no_recipient_at_all_is_an_error_not_a_send():
    import dataclasses

    email = _email()
    nobody = dataclasses.replace(email, to="", envelope=dataclasses.replace(email.envelope, to=()))
    client = _RecordingSmtp()
    result = send_smtp(nobody, ENDPOINT, client)
    assert client.requests == []
    assert result.status is None
    assert "no envelope recipient" in result.error


# ---------------------------------------------------------------------------
# Replies and errors
# ---------------------------------------------------------------------------


def test_an_accepted_message_is_status_250_with_no_error():
    clock = iter([1.0, 1.25])
    result = send_smtp(_email(), ENDPOINT, _RecordingSmtp(), clock=lambda: next(clock))
    assert result.status == 250
    assert result.error is None
    assert result.body_snippet == "2.0.0 Ok: queued as A1"
    assert result.latency_ms == 250.0


@pytest.mark.parametrize("code,text", [(550, "5.1.1 no such user"), (451, "4.3.0 try later")])
def test_a_negative_reply_is_the_status_with_the_text_as_error(code, text):
    result = send_smtp(_email(), ENDPOINT, _RecordingSmtp(reply=SmtpReply(code, text)))
    assert result.status == code
    assert result.error == text
    record = report.response_from_transport(result)
    assert record["status"] == code
    assert record["error"] == text
    assert record["error_kind"] is None


def test_a_raised_negative_reply_is_recorded_as_a_status_too():
    error = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
    result = send_smtp(_email(), ENDPOINT, _RecordingSmtp(raises=error))
    assert result.status == 535
    assert result.error == "5.7.8 bad credentials"


@pytest.mark.parametrize("error,kind", [
    (ConnectionRefusedError(111, "Connection refused"), "refused"),
    (socket.gaierror(-2, "Name or service not known"), "dns"),
    (socket.timeout("timed out"), "timeout"),
    (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), "tls"),
    (smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."), "tls"),
    (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "other"),
])
def test_no_reply_lands_in_the_transport_error_buckets(error, kind):
    result = send_smtp(_email(), ENDPOINT, _RecordingSmtp(raises=error))
    assert result.status is None
    assert report.response_from_transport(result)["error_kind"] == kind


# ---------------------------------------------------------------------------
# send dispatches on the transport
# ---------------------------------------------------------------------------


def test_send_takes_an_smtp_target_over_smtp():
    client = _RecordingSmtp()
    result = send(_email(), _Smtp(), client)
    assert result.status == 250
    assert len(client.requests) == 1
    assert client.requests[0].endpoint == ENDPOINT


def test_send_refuses_a_header_hook_on_an_smtp_target():
    with pytest.raises(ValueError, match="no request headers"):
        send(_email(), _Smtp(), _RecordingSmtp(), extra_headers=lambda body: {"X": "1"})


def test_send_adds_the_target_headers_to_an_http_request():
    client = _RecordingHttp()
    destination = HttpDestination(url="http://127.0.0.1:9/in",
                                  headers={"Authorization": "Basic dTpw"})
    result = send(_email(), destination, client)
    assert result.status == 202
    assert client.requests[0].headers["Authorization"] == "Basic dTpw"


def test_an_http_envelope_to_replaces_the_envelope_the_format_sends():
    email = _email()
    raw = get_format("sendgrid-raw", DEFAULT_BOUNDARY)
    client = _RecordingHttp()
    destination = HttpDestination(url="http://127.0.0.1:9/in", wire_format=raw,
                                  envelope_to=("desk@inbound.chat.test",))
    send(email, destination, client)
    body = client.requests[0].body
    readdressed = with_envelope_to(email, ("desk@inbound.chat.test",))
    assert body == raw.encode(readdressed).body
    assert body != raw.encode(email).body
    assert b'"to":["desk@inbound.chat.test"]' in body
    # The message itself still carries the To header it was generated with.
    assert readdressed.headers == email.headers
    assert readdressed.envelope.from_addr == email.envelope.from_addr


def test_without_envelope_to_the_payload_goes_out_unchanged():
    email = _email()
    raw = get_format("sendgrid-raw", DEFAULT_BOUNDARY)
    client = _RecordingHttp()
    send(email, HttpDestination(url="http://127.0.0.1:9/in", wire_format=raw), client)
    assert client.requests[0].body == raw.encode(email).body


def test_a_hook_header_replaces_a_target_header_of_the_same_name():
    request = build_request(
        _email(), "http://127.0.0.1/", headers={"authorization": "Basic target"},
        extra_headers=lambda _body: {"Authorization": "Basic signed"},
    )
    assert request.headers["Authorization"] == "Basic signed"
    assert "authorization" not in request.headers


def test_a_target_header_cannot_replace_content_length():
    with pytest.raises(ValueError, match="computes it from the body"):
        build_request(_email(), "http://127.0.0.1/", headers={"content-length": "1"})


# ---------------------------------------------------------------------------
# The smtplib clients, against a fake smtplib.SMTP
# ---------------------------------------------------------------------------


class FakeSMTP:
    """Records the calls a client makes and answers from `replies`."""

    instances: list = []

    def __init__(self, host, port, local_hostname=None, timeout=None, *, log, replies,
                 disconnect_after=None):
        self.log, self.replies = log, dict(replies)
        self.disconnect_after = disconnect_after
        self.transactions = 0
        self.sock = None
        log.append(("connect", host, port, local_hostname, timeout))

    def ehlo_or_helo_if_needed(self):
        self.log.append(("ehlo",))

    def ehlo(self):
        self.log.append(("ehlo",))

    def starttls(self, context=None):
        self.log.append(("starttls", isinstance(context, ssl.SSLContext)))

    def login(self, user, password):
        self.log.append(("login", user, password))

    def mail(self, sender):
        if self.disconnect_after is not None and self.transactions >= self.disconnect_after:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        self.log.append(("mail", sender))
        return self.replies.get("mail", (250, b"ok"))

    def rcpt(self, recipient):
        self.log.append(("rcpt", recipient))
        return self.replies.get("rcpt", (250, b"ok"))

    def data(self, message):
        self.log.append(("data", len(message)))
        self.transactions += 1
        reply = self.replies.get("data", (250, b"2.0.0 queued"))
        if reply[0] != 250 and reply[0] >= 500 and self.replies.get("data_raises"):
            raise smtplib.SMTPDataError(*reply)
        return reply

    def rset(self):
        self.log.append(("rset",))

    def quit(self):
        self.log.append(("quit",))


def _factory(log, **kwargs):
    return lambda host, port, local_hostname=None, timeout=None: FakeSMTP(
        host, port, local_hostname, timeout, log=log, **kwargs
    )


def test_the_one_shot_client_runs_the_whole_connection_and_quits():
    log = []
    endpoint = SmtpEndpoint(host="127.0.0.1", port=2525, starttls=True,
                            username="relay", password="pw")
    request = build_smtp_request(_email(), endpoint, timeout=4.0)
    reply = SmtplibClient(_factory(log, replies={})).send(request)
    assert reply == SmtpReply(250, "2.0.0 queued")
    steps = [entry[0] for entry in log]
    assert steps == ["connect", "ehlo", "starttls", "ehlo", "login", "mail",
                     *(["rcpt"] * len(request.envelope_to)), "data", "quit"]
    assert log[0] == ("connect", "127.0.0.1", 2525, EHLO_NAME, 4.0)
    assert ("starttls", True) in log
    assert ("login", "relay", "pw") in log


def test_a_refused_recipient_ends_the_transaction_before_data():
    log = []
    request = build_smtp_request(_email(), ENDPOINT)
    reply = SmtplibClient(_factory(log, replies={"rcpt": (550, b"5.1.1 unknown")})).send(request)
    assert reply == SmtpReply(550, "5.1.1 unknown")
    steps = [entry[0] for entry in log]
    assert "data" not in steps
    assert steps[-2:] == ["rset", "quit"]


def test_a_refused_data_raised_by_smtplib_is_returned_as_the_reply():
    log = []
    replies = {"data": (554, b"5.6.0 rejected"), "data_raises": True}
    reply = SmtplibClient(_factory(log, replies=replies)).send(
        build_smtp_request(_email(), ENDPOINT)
    )
    assert reply == SmtpReply(554, "5.6.0 rejected")


def test_the_persistent_client_opens_one_connection_for_many_messages():
    log = []
    ticks = iter([0.0, 0.004])
    client = PersistentSmtpClient(_factory(log, replies={}), clock=lambda: next(ticks))
    setup = client.connect(ENDPOINT, 5.0)
    assert setup == pytest.approx(0.004)
    for email in generate_corpus(3, 3):
        assert client.send(build_smtp_request(email, ENDPOINT)).code == 250
    assert [entry[0] for entry in log].count("connect") == 1
    assert [entry[0] for entry in log].count("data") == 3
    assert client.connect_seconds == [pytest.approx(0.004)]
    assert client.reconnects == 0
    client.close()
    assert log[-1] == ("quit",)


def test_the_persistent_client_reconnects_once_when_a_used_connection_drops():
    log = []
    client = PersistentSmtpClient(_factory(log, replies={}, disconnect_after=1))
    client.connect(ENDPOINT, 5.0)
    first, second = generate_corpus(3, 2)
    assert client.send(build_smtp_request(first, ENDPOINT)).code == 250
    assert client.send(build_smtp_request(second, ENDPOINT)).code == 250
    assert client.reconnects == 1
    assert len(client.connect_seconds) == 2


def test_a_fresh_connection_that_drops_is_not_retried():
    log = []
    client = PersistentSmtpClient(_factory(log, replies={}, disconnect_after=0))
    client.connect(ENDPOINT, 5.0)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        client.send(build_smtp_request(_email(), ENDPOINT))
    assert client.reconnects == 0
    assert [entry[0] for entry in log].count("connect") == 1
