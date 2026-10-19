"""The Frappe Helpdesk recipes, through the real CLI, against sweep/fakes/frappe.py.

The fake takes mail over SMTP the way the Email Account pull would deliver it,
stores each message as a Communication on an HD Ticket, and answers
/api/resource/<doctype> in Frappe's shapes, so `verify fire` with the sweep
config and with the example recipe makes the whole round trip: SMTP in, a
token-authenticated readback by the tag in the Communication's content, every
check that has something to check PASS.

Every connection is loopback: the fake binds 127.0.0.1 only.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testinghq.cli.main import main as cli_main
from testinghq.core.exit_codes import EXIT_OK, EXIT_REFUSED

pytestmark = pytest.mark.allow_network

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_PATH = REPO_ROOT / "sweep" / "fakes" / "frappe.py"


def _load_fake():
    name = "sweep_fake_frappe"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FAKE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fake = _load_fake()

# (config file, its target, its readback base url, its SMTP port line, the
# Email Account name its list_path scopes strays to)
CONFIGS = {
    "sweep": (REPO_ROOT / "sweep" / "configs" / "frappe.toml", "frappe",
              "http://127.0.0.1:3500", "port = 3525", "Sweep Support"),
    "recipe": (REPO_ROOT / "examples" / "readback" / "frappe-helpdesk.toml", "helpdesk",
               "http://127.0.0.1:8000", "port = 1025", "Support"),
}


def _pointed_at(helpdesk, which, tmp_path):
    """A copy of the config with its readback url and SMTP port moved to the
    fake's ephemeral ones, and nothing else changed."""
    path, target, base_url, port_line, _account = CONFIGS[which]
    text = path.read_text(encoding="utf-8")
    assert text.count(base_url) == 1 and text.count(port_line) == 1, which
    text = text.replace(base_url, helpdesk.url).replace(
        port_line, f"port = {helpdesk.smtp_port}")
    copy = tmp_path / f"{which}.toml"
    copy.write_text(text, encoding="utf-8")
    return str(copy), target


def _verify(config, target, out, *extra):
    return cli_main([
        "verify", "fire", "--send", "--target", target, "--config", config,
        "--count", "8", "--rate", "200", "--quiet-window", "0.3",
        "--poll-interval", "0.05", "--out", str(out), *extra,
    ])


@pytest.mark.parametrize("which", sorted(CONFIGS))
def test_verify_round_trips_through_the_fake_helpdesk(which, tmp_path, monkeypatch, capsys):
    account = CONFIGS[which][4]
    with fake.FakeFrappeHelpdesk(email_account=account) as helpdesk:
        monkeypatch.setenv("FRAPPE_API_AUTH", helpdesk.authorization)
        config, target = _pointed_at(helpdesk, which, tmp_path)
        out = tmp_path / "verify.json"
        code = _verify(config, target, out, "--expect-route", account)
        printed = capsys.readouterr()
        requests = list(helpdesk.requests)
        tickets = helpdesk.snapshot()
    assert code == EXIT_OK, printed.out + printed.err

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["verdict"] == "VERIFIED"
    assert artifact["config"]["transport"] == "smtp"
    # Eight from the run, plus the welcome ticket a fresh install holds.
    assert len(tickets) == 9
    statuses = {}
    for record in artifact["records"]:
        assert record["response"]["status"] == 250
        for check in record["verification"]["checks"]:
            statuses.setdefault(check["check"], set()).add((check["status"], check["reason"]))
    for name in ("ticket_created", "sender", "subject", "body", "routing"):
        assert statuses[name] == {("passed", None)}, (name, statuses[name])

    readback = [r for r in requests if r[1] == "/api/resource/Communication"]
    assert readback, "the readback never asked for a Communication"
    assert {r[3] for r in readback} == {helpdesk.authorization}
    tags = {record["tag"] for record in artifact["records"]}
    looked_up = set()
    for _method, _path, query, _auth in readback:
        assert json.loads(query["fields"])[:5] == [
            "name", "reference_name", "sender", "subject", "content"]
        assert query["limit_page_length"] == "0"
        first, *scope = json.loads(query["filters"])
        assert scope == [["reference_doctype", "=", "HD Ticket"],
                         ["sent_or_received", "=", "Received"]]
        if first[0] == "content":
            assert first[1] == "like" and first[2][0] == first[2][-1] == "%"
            looked_up.add(first[2].strip("%"))
        else:
            # The stray listing is scoped to the Email Account under test.
            assert first == ["email_account", "=", account]
    assert tags <= looked_up


def test_a_key_the_helpdesk_does_not_know_is_refused_before_sending(tmp_path, monkeypatch, capsys):
    with fake.FakeFrappeHelpdesk() as helpdesk:
        monkeypatch.setenv("FRAPPE_API_AUTH", "token sweepkey0000001:wrong-secret")
        config, target = _pointed_at(helpdesk, "sweep", tmp_path)
        code = _verify(config, target, tmp_path / "verify.json")
        printed = capsys.readouterr()
        messages = list(helpdesk.messages)
    assert code == EXIT_REFUSED
    assert "refused a preflight lookup with 401" in printed.out + printed.err
    assert messages == []


# -- the fake on its own -----------------------------------------------------


def _get(helpdesk, path, authorization=None):
    request = urllib.request.Request(helpdesk.url + path)
    if authorization is not None:
        request.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _send_mail(helpdesk, raw):
    import smtplib

    with smtplib.SMTP("127.0.0.1", helpdesk.smtp_port, timeout=5) as client:
        client.sendmail("alice@example.test", ["support@frappe.test"], raw)


def _raw(subject, body, message_id, extra=""):
    return (
        f"From: Alice <alice@example.test>\r\nTo: support@frappe.test\r\n"
        f"Subject: {subject}\r\nMessage-ID: <{message_id}>\r\n{extra}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode("utf-8")


def test_the_fake_refuses_guests_and_unknown_keys_as_frappe_does():
    with fake.FakeFrappeHelpdesk() as helpdesk:
        assert _get(helpdesk, "/api/method/ping") == (200, {"message": "pong"})
        status, body = _get(helpdesk, "/api/resource/HD%20Ticket")
        assert (status, body["exc_type"]) == (403, "PermissionError")
        status, body = _get(helpdesk, "/api/resource/HD%20Ticket", "token nope:nope")
        assert (status, body["exc_type"]) == (401, "AuthenticationError")
        basic = "Basic " + base64.b64encode(
            f"{helpdesk.api_key}:{helpdesk.api_secret}".encode()).decode()
        assert _get(helpdesk, "/api/resource/HD%20Ticket", basic)[0] == 200
        assert _get(helpdesk, "/api/method/frappe.auth.get_logged_user",
                    helpdesk.authorization) == (200, {"message": "Administrator"})


def test_the_fake_lists_with_fields_like_filters_and_frappes_default_page():
    with fake.FakeFrappeHelpdesk(welcome_ticket=False) as helpdesk:
        for index in range(25):
            _send_mail(helpdesk, _raw(f"Printer {index}", f"body [testinghq:tag-{index:04d}]",
                                      f"m{index}@example.test"))
        auth = helpdesk.authorization
        status, body = _get(helpdesk, "/api/resource/HD%20Ticket", auth)
        assert status == 200 and len(body["data"]) == 20
        assert set(body["data"][0]) == {"name"}

        query = (
            "/api/resource/HD%20Ticket?fields=%5B%22name%22%2C%22raised_by%22%5D"
            "&limit_page_length=0&filters=%5B%5B%22description%22%2C%22like%22%2C"
            "%22%25TAG-0007%25%22%5D%5D"
        )
        status, body = _get(helpdesk, query, auth)
        assert status == 200
        assert body["data"] == [{"name": 8, "raised_by": "alice@example.test"}]

        everything = "/api/resource/HD%20Ticket?limit_page_length=0&fields=%5B%22*%22%5D"
        rows = _get(helpdesk, everything, auth)[1]["data"]
        assert len(rows) == 25
        assert rows[0]["email_account"] == "Sweep Support"
        bad = "/api/resource/HD%20Ticket?fields=%5B%22no_such_field%22%5D"
        assert _get(helpdesk, bad, auth)[0] == 417


def test_the_fake_dedupes_by_message_id_and_threads_replies():
    with fake.FakeFrappeHelpdesk(welcome_ticket=False) as helpdesk:
        _send_mail(helpdesk, _raw("Help", "first", "a@example.test"))
        _send_mail(helpdesk, _raw("Help", "first", "a@example.test"))
        _send_mail(helpdesk, _raw("Re: Help", "second", "b@example.test",
                                  "In-Reply-To: <a@example.test>\r\n"))
        _send_mail(helpdesk, _raw("Help", "another", "c@example.test"))
        tickets = helpdesk.snapshot()
        communications = list(helpdesk.communications)
    assert [t["name"] for t in tickets] == [1, 2]
    assert [c["reference_name"] for c in communications] == ["1", "1", "2"]
    assert communications[1]["in_reply_to"] == communications[0]["name"]
    assert len(helpdesk.messages) == 4
