"""`[targets.<name>]` transport, headers and SMTP keys: what loads, what is
refused, and what `resolve_target` hands the send paths.

Every config here is written to a temporary file and read by the real loader,
and every environment is an explicit mapping, so nothing depends on the
process environment or on DNS (every host is loopback, reserved or faked).
"""
from __future__ import annotations

import shlex
import socket

import pytest

from testinghq import config_doc
from testinghq.cli.main import main as cli_main
from testinghq.core import guardrails
from testinghq.core.config import ConfigError, load_config
from testinghq.core.exit_codes import EXIT_OK, EXIT_REFUSED
from testinghq.core.targets import BARRAGE, resolve_target
from testinghq.core.transport import SmtpEndpoint
from testinghq.pipeline.adapters import AdapterError, parse_readback_config

PUBLIC_HOST = "mail.mycompany.com"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        if host == PUBLIC_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guardrails.clear_dns_cache()
    yield
    guardrails.clear_dns_cache()


def _write(tmp_path, text):
    path = tmp_path / "target.toml"
    path.write_text(text, encoding="utf-8")
    return path


SMTP = """[targets.mail]
transport = "smtp"
host = "127.0.0.1"
port = 2525
"""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_a_target_without_transport_is_http(tmp_path):
    target = load_config(_write(tmp_path, '[targets.a]\nurl = "http://127.0.0.1:9/in"\n'),
                         env={}).get("a")
    assert target.transport == "http"
    assert target.smtp is None
    assert dict(target.headers) == {}


def test_an_smtp_target_loads_with_mime_and_an_smtp_url(tmp_path):
    target = load_config(_write(tmp_path, SMTP), env={}).get("mail")
    assert target.transport == "smtp"
    assert target.wire_format == "mime"
    assert target.url == "smtp://127.0.0.1:2525"
    assert target.smtp.host == "127.0.0.1"
    assert target.smtp.port == 2525
    assert target.smtp.starttls is False
    assert target.smtp.envelope_from is None
    assert target.smtp.envelope_to == ()


def test_an_smtp_target_defaults_to_port_25(tmp_path):
    text = '[targets.mail]\ntransport = "smtp"\nhost = "mail.example.test"\n'
    target = load_config(_write(tmp_path, text), env={}).get("mail")
    assert target.smtp.port == 25
    assert target.url == "smtp://mail.example.test:25"


def test_an_ipv6_smtp_host_keeps_a_parseable_url(tmp_path):
    text = '[targets.mail]\ntransport = "smtp"\nhost = "::1"\n'
    target = load_config(_write(tmp_path, text), env={}).get("mail")
    assert target.url == "smtp://[::1]:25"
    assert guardrails.extract_host(target.url) == "::1"


def test_format_mime_is_accepted_on_an_smtp_target(tmp_path):
    target = load_config(_write(tmp_path, SMTP + 'format = "mime"\n'), env={}).get("mail")
    assert target.wire_format == "mime"


def test_any_other_format_on_an_smtp_target_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="'mime' or absent"):
        load_config(_write(tmp_path, SMTP + 'format = "sendgrid"\n'), env={})


def test_an_unknown_transport_is_refused(tmp_path):
    text = '[targets.a]\ntransport = "carrier-pigeon"\nurl = "http://127.0.0.1/"\n'
    with pytest.raises(ConfigError, match="unknown transport 'carrier-pigeon'"):
        load_config(_write(tmp_path, text), env={})


def test_an_smtp_target_without_host_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="missing required key 'host'"):
        load_config(_write(tmp_path, '[targets.mail]\ntransport = "smtp"\n'), env={})


def test_a_url_on_an_smtp_target_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="url is for an http target"):
        load_config(_write(tmp_path, SMTP + 'url = "http://127.0.0.1/"\n'), env={})


def test_an_smtp_key_on_an_http_target_is_refused(tmp_path):
    text = '[targets.a]\nurl = "http://127.0.0.1/"\nhost = "127.0.0.1"\nport = 25\n'
    with pytest.raises(ConfigError, match=r"host, port are smtp keys.*transport is http"):
        load_config(_write(tmp_path, text), env={})


def test_headers_on_an_smtp_target_are_refused(tmp_path):
    text = SMTP + '\n[targets.mail.headers]\nAuthorization = "env:X"\n'
    with pytest.raises(ConfigError, match="username and password"):
        load_config(_write(tmp_path, text), env={})


@pytest.mark.parametrize("value", ["0", "65536", "true", '"25"'])
def test_a_bad_port_is_refused(tmp_path, value):
    text = f'[targets.mail]\ntransport = "smtp"\nhost = "127.0.0.1"\nport = {value}\n'
    with pytest.raises(ConfigError, match="port must be an integer"):
        load_config(_write(tmp_path, text), env={})


def test_a_host_with_a_scheme_is_refused(tmp_path):
    text = '[targets.mail]\ntransport = "smtp"\nhost = "smtp://127.0.0.1"\n'
    with pytest.raises(ConfigError, match="bare host name"):
        load_config(_write(tmp_path, text), env={})


def test_starttls_must_be_a_boolean(tmp_path):
    with pytest.raises(ConfigError, match="starttls must be true or false"):
        load_config(_write(tmp_path, SMTP + 'starttls = "yes"\n'), env={})


def test_a_literal_smtp_password_is_refused(tmp_path):
    text = SMTP + 'username = "env:U"\npassword = "hunter2"\n'
    with pytest.raises(ConfigError, match="password is a literal value"):
        load_config(_write(tmp_path, text), env={})


def test_a_password_without_a_username_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="password is set without username"):
        load_config(_write(tmp_path, SMTP + 'password = "env:P"\n'), env={})


def test_envelope_to_takes_one_address_or_a_list(tmp_path):
    one = load_config(_write(tmp_path, SMTP + 'envelope_to = "desk@helpdesk.test"\n'),
                      env={}).get("mail")
    assert one.smtp.envelope_to == ("desk@helpdesk.test",)
    two = load_config(
        _write(tmp_path, SMTP + 'envelope_to = ["a@helpdesk.test", "b@helpdesk.test"]\n'),
        env={},
    ).get("mail")
    assert two.smtp.envelope_to == ("a@helpdesk.test", "b@helpdesk.test")


def test_a_malformed_envelope_to_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="envelope_to must be an address or a list"):
        load_config(_write(tmp_path, SMTP + "envelope_to = 3\n"), env={})


ENVELOPE_HTTP = """[targets.chat]
url = "http://127.0.0.1:3000/rails/action_mailbox/sendgrid/inbound_emails"
format = "sendgrid-raw"
"""


def test_an_http_target_takes_envelope_to(tmp_path):
    path = _write(tmp_path, ENVELOPE_HTTP + 'envelope_to = "desk@inbound.chat.test"\n')
    assert load_config(path, env={}).get("chat").envelope_to == ("desk@inbound.chat.test",)
    resolved = resolve_target("chat", str(path), env={})
    assert resolved.envelope_to == ("desk@inbound.chat.test",)
    assert resolved.smtp is None


def test_envelope_to_on_an_http_format_without_an_envelope_is_refused(tmp_path):
    text = '[targets.a]\nurl = "http://127.0.0.1/"\nformat = "mailgun"\nenvelope_to = "d@x.test"\n'
    with pytest.raises(ConfigError, match=r"format 'mailgun' carries none"):
        load_config(_write(tmp_path, text), env={})


def test_envelope_from_on_an_http_target_is_still_refused(tmp_path):
    text = ENVELOPE_HTTP + 'envelope_from = "bounce@sender.test"\n'
    with pytest.raises(ConfigError, match=r"envelope_from is an smtp key"):
        load_config(_write(tmp_path, text), env={})


def test_a_non_synthetic_http_envelope_to_is_refused_at_resolve(tmp_path):
    path = _write(tmp_path, ENVELOPE_HTTP + 'envelope_to = "someone@gmail.com"\n')
    with pytest.raises(guardrails.GuardrailError, match="envelope_from / envelope_to"):
        resolve_target("chat", str(path), env={})


def test_a_url_carrying_credentials_is_refused_pointing_at_the_headers_table(tmp_path):
    text = '[targets.chat]\nurl = "http://actionmailbox:secret@127.0.0.1:3000/in"\n'
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path, text), env={})
    message = str(caught.value)
    assert "user:pass@host" in message
    assert "[targets.chat.headers]" in message
    assert "config basic-auth" in message


def test_an_environment_url_carrying_credentials_is_refused_too(tmp_path):
    text = '[targets.chat]\nurl = "http://127.0.0.1:3000/in"\n'
    env = {"TESTINGHQ_TARGET_CHAT_URL": "http://u:p@127.0.0.1:3000/in"}
    with pytest.raises(ConfigError, match=r"\[targets.chat.headers\]"):
        load_config(_write(tmp_path, text), env=env)


def test_the_url_override_on_an_smtp_target_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="TESTINGHQ_TARGET_MAIL_URL is set"):
        load_config(_write(tmp_path, SMTP), env={"TESTINGHQ_TARGET_MAIL_URL": "http://x/"})


HEADERS = """[targets.chat]
url = "http://127.0.0.1:3000/in"

[targets.chat.headers]
Authorization = "env:CHAT_AUTH"
"""


def test_a_header_table_loads_as_references(tmp_path):
    target = load_config(_write(tmp_path, HEADERS), env={}).get("chat")
    assert dict(target.headers) == {"Authorization": "env:CHAT_AUTH"}


def test_a_literal_target_header_gets_the_readback_refusal(tmp_path):
    """The same rule and the same words as [readback.headers], with only the
    table's name changed."""
    text = HEADERS.replace('"env:CHAT_AUTH"', '"Basic abc"')
    with pytest.raises(ConfigError) as target_error:
        load_config(_write(tmp_path, text), env={})
    with pytest.raises(AdapterError) as readback_error:
        parse_readback_config(
            {"kind": "http", "url": "http://127.0.0.1/", "headers": {"Authorization": "Basic abc"}},
            env={},
        )
    assert str(target_error.value) == str(readback_error.value).replace(
        "[readback.headers]", "[targets.chat.headers]"
    )
    assert "is a literal value" in str(target_error.value)


def test_a_target_header_may_not_set_content_type(tmp_path):
    text = HEADERS + 'Content-Type = "env:CT"\n'
    with pytest.raises(ConfigError, match="computes Content-Type"):
        load_config(_write(tmp_path, text), env={})


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_resolve_reads_header_values_from_the_environment(tmp_path):
    resolved = resolve_target(
        "chat", str(_write(tmp_path, HEADERS)), env={"CHAT_AUTH": "Basic dTpw"}
    )
    assert resolved.transport == "http"
    assert dict(resolved.headers) == {"Authorization": "Basic dTpw"}
    assert "dTpw" not in repr(resolved)
    assert resolved.artifact_config()["transport"] == "http"


def test_an_unset_header_variable_is_refused_at_resolve(tmp_path):
    with pytest.raises(ConfigError, match="'CHAT_AUTH', which is not set"):
        resolve_target("chat", str(_write(tmp_path, HEADERS)), env={})


def test_a_header_value_with_a_line_break_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="line break"):
        resolve_target("chat", str(_write(tmp_path, HEADERS)),
                       env={"CHAT_AUTH": "Basic x\r\nX-Evil: 1"})


def test_resolve_builds_the_smtp_endpoint_with_credentials(tmp_path):
    text = SMTP + (
        'starttls = true\nusername = "env:SMTP_USER"\npassword = "env:SMTP_PASS"\n'
        'envelope_from = "bounce@sender.test"\nenvelope_to = "desk@helpdesk.test"\n'
    )
    resolved = resolve_target(
        "mail", str(_write(tmp_path, text)),
        env={"SMTP_USER": "relay", "SMTP_PASS": "s3cret"},
    )
    assert resolved.transport == "smtp"
    assert resolved.format_name == "mime"
    assert resolved.smtp == SmtpEndpoint(
        host="127.0.0.1", port=2525, starttls=True, username="relay",
        password="s3cret", envelope_from="bounce@sender.test",
        envelope_to=("desk@helpdesk.test",),
    )
    assert "s3cret" not in repr(resolved)
    assert resolved.artifact_config() == {
        "format": "mime", "allow_public_target": False, "transport": "smtp",
    }


def test_an_unset_smtp_password_variable_is_refused(tmp_path):
    text = SMTP + 'username = "env:SMTP_USER"\npassword = "env:SMTP_PASS"\n'
    with pytest.raises(ConfigError, match="'SMTP_PASS', which is not set"):
        resolve_target("mail", str(_write(tmp_path, text)), env={"SMTP_USER": "relay"})


def test_a_non_synthetic_envelope_override_is_refused(tmp_path):
    text = SMTP + 'envelope_to = "someone@gmail.com"\n'
    with pytest.raises(guardrails.GuardrailError, match="envelope_from / envelope_to"):
        resolve_target("mail", str(_write(tmp_path, text)), env={})


def test_a_public_smtp_host_is_refused_without_the_flag(tmp_path):
    text = f'[targets.mail]\ntransport = "smtp"\nhost = "{PUBLIC_HOST}"\n'
    with pytest.raises(guardrails.GuardrailError, match="--allow-public-target"):
        resolve_target("mail", str(_write(tmp_path, text)), env={})
    resolved = resolve_target("mail", str(_write(tmp_path, text)), env={},
                              allow_public_target=True)
    assert resolved.public is True


def test_barrage_at_a_public_smtp_host_needs_the_flag_and_public_true(tmp_path):
    text = f'[targets.mail]\ntransport = "smtp"\nhost = "{PUBLIC_HOST}"\n'
    with pytest.raises(guardrails.GuardrailError, match=r"Missing: public = true"):
        resolve_target("mail", str(_write(tmp_path, text)), env={},
                       allow_public_target=True, load_tool=BARRAGE)
    both = _write(tmp_path, text + "public = true\n")
    resolved = resolve_target("mail", str(both), env={},
                              allow_public_target=True, load_tool=BARRAGE)
    assert resolved.public is True


# ---------------------------------------------------------------------------
# config validate
# ---------------------------------------------------------------------------


def test_validate_renders_an_smtp_target(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "relay")
    text = SMTP + 'username = "env:SMTP_USER"\nenvelope_to = "desk@helpdesk.test"\n'
    targets, _readback, _notes = config_doc.validate(_write(tmp_path, text))
    assert targets["mail"] == {
        "transport": "smtp", "url": "smtp://127.0.0.1:2525", "format": "mime",
        "public": False, "public_host": False, "host": "127.0.0.1", "port": 2525,
        "starttls": False, "username": "env:SMTP_USER",
        "envelope_to": ["desk@helpdesk.test"],
    }
    assert "transport smtp" in config_doc.describe_target("mail", targets["mail"])


def test_validate_refuses_an_unset_target_header_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAT_AUTH", raising=False)
    with pytest.raises(config_doc.ValidationError, match="CHAT_AUTH"):
        config_doc.validate(_write(tmp_path, HEADERS))


def test_validate_never_prints_a_target_header_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHAT_AUTH", "Basic c2VjcmV0LXZhbHVl")
    for argv in (["config", "validate", str(_write(tmp_path, HEADERS))],
                 ["config", "validate", "--toml", str(_write(tmp_path, HEADERS))]):
        assert cli_main(argv) == EXIT_OK
        out = capsys.readouterr().out
        assert "c2VjcmV0LXZhbHVl" not in out
        assert "env:CHAT_AUTH" in out or "--toml" not in argv


@pytest.mark.parametrize("text", [
    HEADERS,
    SMTP + 'envelope_to = ["a@helpdesk.test"]\n',
    ENVELOPE_HTTP + 'envelope_to = ["a@helpdesk.test"]\n',
])
def test_the_toml_render_of_new_keys_round_trips_through_the_loader(tmp_path, monkeypatch, text):
    monkeypatch.setenv("CHAT_AUTH", "Basic x")
    original = load_config(_write(tmp_path, text), env={})
    targets, readback, _ = config_doc.validate(_write(tmp_path, text))
    rendered = tmp_path / "rendered.toml"
    rendered.write_text(config_doc.render_effective(targets, readback), encoding="utf-8")
    again = load_config(rendered, env={})
    assert again.targets == original.targets


# ---------------------------------------------------------------------------
# config basic-auth
# ---------------------------------------------------------------------------


class _Pipe:
    def __init__(self, text):
        self._lines = text.splitlines(keepends=True)

    def isatty(self):
        return False

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _Tty:
    def isatty(self):
        return True


def _basic_auth(argv, **kwargs):
    from testinghq.cli import utility
    from testinghq.cli.main import build_parser

    return utility._cmd_config_basic_auth(build_parser().parse_args(argv), **kwargs)


def test_basic_auth_prints_the_env_line_from_a_piped_password(capsys):
    code = _basic_auth(["config", "basic-auth", "actionmailbox", "--env", "CHATWOOT_INGRESS_AUTH"],
                       stdin=_Pipe("p@ss word\n"))
    assert code == EXIT_OK
    out, err = capsys.readouterr()
    # base64("actionmailbox:p@ss word")
    assert out == 'CHATWOOT_INGRESS_AUTH="Basic YWN0aW9ubWFpbGJveDpwQHNzIHdvcmQ="\n'
    assert 'Authorization = "env:CHATWOOT_INGRESS_AUTH"' in err


def test_basic_auth_asks_a_terminal_twice_without_echo(capsys):
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "pw"

    code = _basic_auth(["config", "basic-auth", "u"], stdin=_Tty(), prompt=prompt)
    assert code == EXIT_OK
    assert len(prompts) == 2
    assert capsys.readouterr().out == 'TARGET_AUTH="Basic dTpwdw=="\n'


def test_basic_auth_line_is_one_assignment_when_the_shell_loads_it(capsys):
    """.env.example loads .env with `set -a && . ./.env`. Unquoted, the space
    in `Basic <token>` splits the line into an assignment of `Basic` and a
    command named after the token, so the shell must see one word."""
    assert _basic_auth(["config", "basic-auth", "u"], stdin=_Pipe("pw\n")) == EXIT_OK
    line = capsys.readouterr().out.strip()
    assert shlex.split(line) == ["TARGET_AUTH=Basic dTpwdw=="]


def test_basic_auth_refuses_two_different_passwords(capsys):
    answers = iter(["one", "two"])
    code = _basic_auth(["config", "basic-auth", "u"], stdin=_Tty(),
                       prompt=lambda _text: next(answers))
    assert code == EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == ""
    assert "do not match" in err


@pytest.mark.parametrize("argv,stdin,reason", [
    (["config", "basic-auth", "u"], "\n", "empty"),
    (["config", "basic-auth", "u"], "", "no password"),
    (["config", "basic-auth", "a:b"], "pw\n", "colon"),
    (["config", "basic-auth", "u", "--env", "1BAD"], "pw\n", "not an environment variable"),
])
def test_basic_auth_refusals(capsys, argv, stdin, reason):
    assert _basic_auth(argv, stdin=_Pipe(stdin)) == EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == ""
    assert reason in err


def test_basic_auth_takes_no_password_argument(capsys):
    with pytest.raises(SystemExit) as caught:
        cli_main(["config", "basic-auth", "u", "pw"])
    assert caught.value.code == EXIT_REFUSED
