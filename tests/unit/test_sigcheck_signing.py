"""sigcheck: the schemes against each provider's documented construction, the
plan of variants, the judging, and a run against endpoints that enforce, or do
not enforce, the authentication.

The Mailgun vector was computed with `openssl dgst -sha256 -hmac`, and the
ECDSA vectors are RFC 6979 appendix A.2.5 (P-256, SHA-256), so neither scheme
is only checked against itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from email import policy
from email.parser import BytesParser

import pytest

from testinghq import cli, reporting
from testinghq.core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.core.transport import ClientResponse
from testinghq.pipeline import sigcheck as tool

NOW = 1_700_000_000
BODY = b"--b\r\nContent-Disposition: form-data; name=\"subject\"\r\n\r\nhi\r\n--b--\r\n"
MAILGUN_KEY = "key-3ax6xnjp29jd6fds4gc373sgvjxteol0"
BASIC = "hook-user:hook-pass"

#: RFC 6979 A.2.5.
RFC_KEY = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
RFC_PUBLIC = (
    0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,
    0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299,
)
RFC_VECTORS = {
    b"sample": (0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
                0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
    b"test": (0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
              0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
}
RFC_KEY_HEX = f"{RFC_KEY:064x}"


# ---------------------------------------------------------------------------
# The schemes
# ---------------------------------------------------------------------------


def test_the_table_has_the_three_schemes():
    assert tool.known_schemes() == ("mailgun", "postmark-basic", "sendgrid")
    with pytest.raises(tool.SigcheckError, match="known"):
        tool.get_scheme("postmark")


def test_mailgun_matches_an_independently_computed_vector():
    signature = tool.mailgun_signature(
        MAILGUN_KEY, "1529006854", "a8ce0edb2dd8301dee6c2405235584e45aa91d1e9f979f3de0"
    )
    assert signature == "b63c0701c4f4b614f272106a1b367c5c3369bcdca664ae73ebd52787e45eef07"


def test_mailgun_sends_three_form_fields_and_does_not_sign_the_body():
    scheme = tool.get_scheme("mailgun")
    signed = scheme.sign(BODY, MAILGUN_KEY, NOW, "v")
    assert signed.headers == {}
    assert set(signed.fields) == {"timestamp", "token", "signature"}
    assert len(signed.fields["token"]) == 50
    assert scheme.verify(BODY, MAILGUN_KEY, signed)
    assert scheme.verify(BODY + b"tampered", MAILGUN_KEY, signed)
    assert not scheme.verify(BODY, "another-key", signed)
    assert not scheme.verify(BODY, MAILGUN_KEY, tool.Signed())


def test_each_mailgun_variant_gets_its_own_token():
    assert tool.mailgun_token(NOW, "a") != tool.mailgun_token(NOW, "b")
    assert tool.mailgun_token(NOW, "a") == tool.mailgun_token(NOW, "a")


def test_the_p256_signer_reproduces_the_rfc_6979_vectors():
    assert tool.p256_public_key(RFC_KEY) == RFC_PUBLIC
    for message, expected in RFC_VECTORS.items():
        digest = hashlib.sha256(message).digest()
        assert tool.p256_sign_digest(RFC_KEY, digest) == expected
        assert tool.p256_verify_digest(RFC_PUBLIC, digest, *expected)
        assert not tool.p256_verify_digest(RFC_PUBLIC, hashlib.sha256(b"other").digest(), *expected)


def test_keys_parse_from_hex_sec1_pem_and_spki():
    assert tool.parse_private_key(RFC_KEY_HEX) == RFC_KEY
    sec1 = (b"\x30\x31\x02\x01\x01\x04\x20" + RFC_KEY.to_bytes(32, "big")
            + b"\xa0\x0a\x06\x08" + bytes.fromhex("2a8648ce3d030107"))
    pem = ("-----BEGIN EC PRIVATE KEY-----\n" + base64.b64encode(sec1).decode()
           + "\n-----END EC PRIVATE KEY-----\n")
    assert tool.parse_private_key(pem) == RFC_KEY
    spki = base64.b64encode(tool.spki_der(RFC_PUBLIC)).decode()
    assert tool.parse_public_key(spki) == RFC_PUBLIC
    with pytest.raises(tool.SigcheckError):
        tool.parse_private_key("not a key")


def test_sendgrid_signs_timestamp_then_raw_body_in_the_documented_headers():
    scheme = tool.get_scheme("sendgrid")
    signed = scheme.sign(BODY, RFC_KEY_HEX, NOW)
    assert set(signed.headers) == {tool.SENDGRID_SIGNATURE_HEADER, tool.SENDGRID_TIMESTAMP_HEADER}
    r, s = tool.parse_der_signature(base64.b64decode(signed.headers[tool.SENDGRID_SIGNATURE_HEADER]))
    digest = hashlib.sha256(str(NOW).encode() + BODY).digest()
    assert tool.p256_verify_digest(RFC_PUBLIC, digest, r, s)
    assert scheme.verify(BODY, RFC_KEY_HEX, signed)
    assert not scheme.verify(BODY + b"x", RFC_KEY_HEX, signed)


def test_postmark_basic_is_an_authorization_header():
    scheme = tool.get_scheme("postmark-basic")
    signed = scheme.sign(BODY, BASIC, NOW)
    assert signed.headers == {"Authorization": "Basic " + base64.b64encode(BASIC.encode()).decode()}
    assert scheme.verify(BODY, BASIC, signed)
    assert not scheme.verify(BODY, BASIC, scheme.sign(BODY, scheme.wrong_secret(BASIC), NOW))
    with pytest.raises(tool.SigcheckError, match="user:password"):
        scheme.sign(BODY, "no-colon", NOW)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _plan(name, secret):
    return {p.variant: p for p in tool.plan(
        tool.get_scheme(name), secret, now=NOW, window=300,
        encode=lambda payload, signed: payload.subject.encode(),
    )}


def test_every_scheme_plans_the_six_variants_in_order():
    for name, secret in (("mailgun", MAILGUN_KEY), ("sendgrid", RFC_KEY_HEX),
                         ("postmark-basic", BASIC)):
        planned = tool.plan(tool.get_scheme(name), secret, now=NOW, window=300,
                            encode=lambda p, s: b"")
        assert [p.variant for p in planned] == list(tool.VARIANTS), name


def test_mailgun_replay_resends_the_valid_token_and_stale_is_separate():
    """Mailgun's defence is caching the token, so the replay is the valid
    request again; the stale timestamp is informational, not a finding."""
    plan = _plan("mailgun", MAILGUN_KEY)
    assert plan["valid"].expect == tool.EXPECT_ACCEPT
    assert plan["replayed-token"].expect == tool.EXPECT_REJECT
    assert plan["replayed-token"].signed == plan["valid"].signed
    assert plan["stale-timestamp"].expect == tool.EXPECT_INFO
    assert plan["stale-timestamp"].signed.fields["token"] != plan["valid"].signed.fields["token"]
    assert int(plan["stale-timestamp"].signed.fields["timestamp"]) < NOW - 300
    assert plan["tampered-body"].expect == tool.EXPECT_INFO


def test_sendgrid_tampered_body_carries_the_original_signature():
    plan = _plan("sendgrid", RFC_KEY_HEX)
    tampered = plan["tampered-body"]
    assert tampered.expect == tool.EXPECT_REJECT
    assert tampered.fixed_headers == plan["valid"].signed.headers
    assert tampered.payload.subject != plan["valid"].payload.subject
    assert plan["stale-timestamp"].expect == tool.EXPECT_REJECT
    assert plan["replayed-token"].expect == tool.EXPECT_INFO


def test_postmark_has_nothing_to_replay_or_make_stale():
    plan = _plan("postmark-basic", BASIC)
    assert plan["stale-timestamp"].expect == tool.EXPECT_NA
    assert plan["replayed-token"].expect == tool.EXPECT_NA


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expect,status,verdict", [
    (tool.EXPECT_ACCEPT, 200, tool.VERDICT_OK),
    (tool.EXPECT_ACCEPT, 401, tool.VERDICT_FINDING),
    (tool.EXPECT_REJECT, 403, tool.VERDICT_OK),
    (tool.EXPECT_REJECT, 204, tool.VERDICT_FINDING),
    (tool.EXPECT_REJECT, 500, tool.VERDICT_FINDING),
    (tool.EXPECT_INFO, 200, tool.VERDICT_INFO),
    (tool.EXPECT_INFO, 401, tool.VERDICT_INFO),
    (tool.EXPECT_REJECT, None, tool.VERDICT_FINDING),
])
def test_judging(expect, status, verdict):
    planned = tool.Planned("v", expect, "why")
    assert tool.judge(planned, status, None if status else "refused").verdict == verdict


def test_accepting_stale_but_refusing_replay_is_mailguns_own_guidance():
    def outcome(variant, status):
        return tool.Outcome(variant, tool.EXPECT_INFO, "", status, None, tool.VERDICT_INFO, "")

    notes = tool.notes_for([outcome("stale-timestamp", 200), outcome("replayed-token", 401)],
                           "mailgun")
    assert any("Mailgun's own guidance" in n and "Informational" in n for n in notes)


# ---------------------------------------------------------------------------
# A run against fake endpoints
# ---------------------------------------------------------------------------


def _fields(request):
    raw = (f"Content-Type: {request.headers['Content-Type']}\r\n\r\n").encode() + request.body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {part.get_param("name", header="content-disposition"): part.get_content()
            for part in message.iter_parts()}


class MailgunEndpoint:
    """Verifies the signature, refuses a token it has seen; lenient on age,
    as Mailgun's guidance allows."""

    def __init__(self, enforce=True):
        self.enforce = enforce
        self.seen = set()
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self.enforce:
            return ClientResponse(status=200, body=b"ok")
        fields = _fields(request)
        signed = tool.Signed(fields={k: fields.get(k, "") for k in tool.MAILGUN_FIELDS})
        if not all(signed.fields.values()) or not tool.MailgunScheme().verify(b"", MAILGUN_KEY, signed):
            return ClientResponse(status=401, body=b"bad signature")
        if signed.fields["token"] in self.seen:
            return ClientResponse(status=409, body=b"replayed")
        self.seen.add(signed.fields["token"])
        return ClientResponse(status=200, body=b"ok")


class SendgridEndpoint:
    def __init__(self, window=300, now=NOW):
        self.window, self.now = window, now

    def send(self, request):
        timestamp = request.headers.get(tool.SENDGRID_TIMESTAMP_HEADER)
        signature = request.headers.get(tool.SENDGRID_SIGNATURE_HEADER)
        if not timestamp or not signature or abs(self.now - int(timestamp)) > self.window:
            return ClientResponse(status=403, body=b"")
        r, s = tool.parse_der_signature(base64.b64decode(signature))
        digest = hashlib.sha256(timestamp.encode() + request.body).digest()
        ok = tool.p256_verify_digest(RFC_PUBLIC, digest, r, s)
        return ClientResponse(status=200 if ok else 403, body=b"")


def _config(tmp_path, fmt):
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.local]\nurl = "http://localhost:9/hook"\nformat = "{fmt}"\n',
                    encoding="utf-8")
    return str(path)


def _execute(tmp_path, scheme, secret, client, fmt):
    lines = []
    code = tool.execute(
        scheme_name=scheme, secret=secret, secret_ref="env:HOOK_SECRET",
        target_name="local", config_path=_config(tmp_path, fmt),
        out=str(tmp_path / "sig.json"), client=client, clock=lambda: NOW,
        printer=lines.append,
    )
    path = tmp_path / "sig.json"
    artifact = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return code, artifact, "\n".join(lines)


def test_an_enforcing_mailgun_endpoint_passes_and_gets_the_guidance_note(tmp_path):
    code, artifact, text = _execute(tmp_path, "mailgun", MAILGUN_KEY, MailgunEndpoint(), "mailgun")
    assert code == EXIT_OK, text
    assert artifact["summary"]["verdict"] == "ENFORCED"
    by = {v["variant"]: v for v in artifact["variants"]}
    assert by["replayed-token"]["status"] == 409 and by["replayed-token"]["verdict"] == "OK"
    assert by["stale-timestamp"]["verdict"] == "INFO" and by["stale-timestamp"]["accepted"]
    assert any("Mailgun's own guidance" in n for n in artifact["summary"]["notes"])


class DedupingMailgunEndpoint(MailgunEndpoint):
    """Also refuses an authenticated request whose Message-ID it has already
    accepted, as an endpoint that deduplicates at the door does."""

    def __init__(self):
        super().__init__()
        self.accepted_ids = set()

    def send(self, request):
        response = MailgunEndpoint.send(self, request)
        if response.status != 200:
            return response
        found = re.search(rb"<(sigcheck-[^>]+)>", request.body)
        message_id = found.group(1) if found else b""
        if message_id in self.accepted_ids:
            return ClientResponse(status=409, body=b"duplicate message")
        self.accepted_ids.add(message_id)
        return response


def test_a_second_run_is_not_refused_as_a_repeat_of_the_first(tmp_path):
    """Every run used to carry one fixed Message-ID, so an endpoint that drops
    a repeat refused the second run's valid request and sigcheck reported it.
    Within a run the replay still carries the valid request's id."""
    endpoint = DedupingMailgunEndpoint()
    for offset in (0, 60):
        lines = []
        code = tool.execute(
            scheme_name="mailgun", secret=MAILGUN_KEY, secret_ref="env:HOOK_SECRET",
            target_name="local", config_path=_config(tmp_path, "mailgun"),
            out=str(tmp_path / "sig.json"), client=endpoint,
            clock=lambda offset=offset: NOW + offset, printer=lines.append,
        )
        artifact = json.loads((tmp_path / "sig.json").read_text(encoding="utf-8"))
        by = {v["variant"]: v for v in artifact["variants"]}
        assert code == EXIT_OK, "\n".join(lines)
        assert by["valid"]["status"] == 200
    ids = [re.search(rb"<(sigcheck-[^>]+)>", r.body).group(1) for r in endpoint.requests]
    half = len(ids) // 2
    assert len(set(ids[:half])) == 1 and len(set(ids[half:])) == 1
    assert ids[0] != ids[half]


def test_an_endpoint_that_accepts_everything_is_caught(tmp_path):
    code, artifact, text = _execute(
        tmp_path, "mailgun", MAILGUN_KEY, MailgunEndpoint(enforce=False), "mailgun"
    )
    assert code == EXIT_FINDING
    assert artifact["summary"]["verdict"] == "GAPS"
    failing = {v["variant"] for v in artifact["variants"] if v["verdict"] == "FINDING"}
    assert failing == {"wrong-secret", "unsigned", "replayed-token"}
    assert "GAPS" in text


def test_a_sendgrid_endpoint_that_verifies_the_body_catches_the_tampered_one(tmp_path):
    code, artifact, text = _execute(tmp_path, "sendgrid", RFC_KEY_HEX, SendgridEndpoint(), "sendgrid")
    assert code == EXIT_OK, text
    by = {v["variant"]: v for v in artifact["variants"]}
    assert by["valid"]["status"] == 200
    assert by["tampered-body"]["status"] == 403
    assert by["stale-timestamp"]["status"] == 403
    assert by["replayed-token"]["verdict"] == "INFO"


def test_the_artifact_records_the_secret_reference_never_the_value(tmp_path):
    _code, artifact, _text = _execute(tmp_path, "mailgun", MAILGUN_KEY, MailgunEndpoint(), "mailgun")
    assert artifact["schema_version"] == 1
    assert artifact["tool"] == "sigcheck"
    assert artifact["config"]["tool"] == "sigcheck"
    assert artifact["config"]["secret"] == "env:HOOK_SECRET"
    assert artifact["config"]["format"] == "mailgun"
    assert artifact["config"]["allow_public_target"] is False
    assert MAILGUN_KEY not in json.dumps(artifact)


def test_a_scheme_at_a_target_of_another_format_is_refused_before_sending(tmp_path):
    endpoint = MailgunEndpoint()
    code, artifact, text = _execute(tmp_path, "mailgun", MAILGUN_KEY, endpoint, "sendgrid")
    assert code == EXIT_REFUSED
    assert artifact is None and endpoint.requests == []
    assert 'Set format = "mailgun"' in text


def test_report_reads_a_sigcheck_artifact(tmp_path):
    _execute(tmp_path, "mailgun", MAILGUN_KEY, MailgunEndpoint(enforce=False), "mailgun")
    summary = reporting.load(tmp_path / "sig.json")
    assert summary.tool == "sigcheck"
    assert summary.verdict == "GAPS" and summary.verdict_source == "recorded"
    assert summary.exit_code == EXIT_FINDING
    assert summary.counts.failed == 3
    assert {"wrong-secret", "unsigned", "replayed-token"} <= {f.kind for f in summary.findings}


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_sigcheck_fire_is_registered_and_dry_runs_by_default(tmp_path, capsys):
    code = cli.main(["sigcheck", "fire", "--scheme", "mailgun",
                     "--config", str(tmp_path / "absent.toml")])
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    for variant in tool.VARIANTS:
        assert variant in out
    assert "no network calls were made" in out


def test_a_literal_secret_is_refused(tmp_path, capsys):
    code = cli.main(["sigcheck", "fire", "--scheme", "mailgun", "--secret", "plain-text",
                     "--target", "local", "--config", _config(tmp_path, "mailgun"), "--send"])
    assert code == EXIT_REFUSED
    assert "env:NAME" in capsys.readouterr().err


def test_the_sigcheck_table_supplies_scheme_secret_and_window(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOOK_KEY", RFC_KEY_HEX)
    path = tmp_path / "target.toml"
    path.write_text('[sigcheck]\nscheme = "sendgrid"\nsecret = "env:HOOK_KEY"\n'
                    'timestamp_window_seconds = 120\n', encoding="utf-8")
    code = cli.main(["sigcheck", "fire", "--config", str(path)])
    out = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert "scheme 'sendgrid'" in out and "timestamp window:  120s" in out
    assert base64.b64encode(tool.spki_der(RFC_PUBLIC)).decode() in out


def test_a_literal_secret_in_the_table_is_refused(tmp_path, capsys):
    path = tmp_path / "target.toml"
    path.write_text('[sigcheck]\nscheme = "mailgun"\nsecret = "key-abc"\n', encoding="utf-8")
    assert cli.main(["sigcheck", "fire", "--config", str(path)]) == EXIT_REFUSED
    assert "never a literal" in capsys.readouterr().err


def test_no_scheme_is_refused(tmp_path, capsys):
    assert cli.main(["sigcheck", "fire", "--config", str(tmp_path / "absent.toml")]) == EXIT_REFUSED
    assert "--scheme" in capsys.readouterr().err


def test_the_config_reference_states_the_real_sigcheck_default():
    from testinghq.core import config_schema

    keys = {k.name: k.default for k in config_schema.table_named("sigcheck").keys}
    assert float(keys["timestamp_window_seconds"]) == tool.DEFAULT_WINDOW


def test_the_dry_run_refuses_a_target_whose_format_the_scheme_cannot_sign(
    tmp_path, capsys, monkeypatch
):
    """The dry run says what --send would say. A preview that looked fine for a
    command --send then refused hid the refusal until the real run."""
    monkeypatch.setenv("HOOK_SECRET", MAILGUN_KEY)
    argv = ["sigcheck", "fire", "--scheme", "mailgun", "--secret", "env:HOOK_SECRET",
            "--target", "local", "--config", _config(tmp_path, "sendgrid")]
    assert cli.main(argv) == EXIT_REFUSED
    dry = capsys.readouterr()
    assert cli.main([*argv, "--send"]) == EXIT_REFUSED
    sent = capsys.readouterr()
    assert 'Set format = "mailgun"' in dry.err
    assert dry.err == sent.err, "the dry run and the send give the same refusal"
    assert "no network calls were made" not in dry.out


def test_the_dry_run_of_a_matching_target_still_previews(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", MAILGUN_KEY)
    code = cli.main(["sigcheck", "fire", "--scheme", "mailgun", "--secret", "env:HOOK_SECRET",
                     "--target", "local", "--config", _config(tmp_path, "mailgun")])
    captured = capsys.readouterr()
    assert code == EXIT_DRY_RUN
    assert "no network calls were made" in captured.out
    assert captured.err == ""
