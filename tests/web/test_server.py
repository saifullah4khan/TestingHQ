"""Tests for web/server.py.

Runs the real stdlib server on 127.0.0.1 with an OS-assigned ephemeral port
in a background thread - loopback only, no external network, fully
hermetic. Exercises the actual HTTP layer (routing, JSON (de)serialization,
status codes) rather than calling handler methods directly.

The one thing that is NOT hermetic by default is the fire path's transport,
because the adapter runs the real engine now and web/targets.json's first
entry is a localhost URL. `fake_transport` pins the server's client seam for
every test, so a fire request is served by a recording fake instead of a real
connection to localhost:8000. Without it these tests would pass or fail
depending on whether anything happened to be listening on that port.

The outcome annotation is also checked here rather than in the browser. The
server adds `outcome` to each record using the engine's own classify_record,
and web/static/app.js used to carry its own copy of those rules, which no test
in this repo could reach. The tests at the bottom of this file pin the
annotation to the engine's verdict on both endpoints.
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testinghq.core import report
from testinghq.core.transport import ClientResponse

from web import config, server

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures"


class _RecordingClient:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=self.status, body=self.body)


@pytest.fixture(autouse=True)
def fake_transport(monkeypatch):
    """Every test in this module gets a non-network transport. Autouse on
    purpose: opting in per test is how one of them ends up making a real
    request to a port that may or may not be listening."""
    client = _RecordingClient()
    monkeypatch.setattr(server, "HTTP_CLIENT", client)
    return client


@pytest.fixture()
def running_server():
    httpd = server.make_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base_url, path):
    try:
        with urllib.request.urlopen(base_url + path, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def _post_json(base_url, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_index_page_serves_and_is_branded(running_server):
    status, content_type, body = _get(running_server, "/")
    assert status == 200
    assert "text/html" in content_type
    assert b"TestingHQ" in body


def test_static_js_and_css_serve(running_server):
    status, content_type, body = _get(running_server, "/app.js")
    assert status == 200
    assert "javascript" in content_type
    assert b"classifyRecord" in body

    status, content_type, body = _get(running_server, "/style.css")
    assert status == 200
    assert "text/css" in content_type


def test_unknown_get_path_is_404(running_server):
    status, _, _ = _get(running_server, "/does-not-exist")
    assert status == 404


def test_api_config_lists_targets_and_categories(running_server):
    status, _, body = _get(running_server, "/api/config")
    assert status == 200
    payload = json.loads(body)
    real_targets = config.load_targets()
    assert {t["name"] for t in payload["targets"]} == set(real_targets.keys())
    assert len(payload["categories"]) == 5
    assert "clean" in payload["categories"]
    assert "degenerate" in payload["categories"]


def test_dry_run_never_requires_target_and_never_sends(running_server):
    status, payload = _post_json(
        running_server, "/api/dry-run", {"mix": ["clean", "degenerate"], "count": 10, "seed": 5}
    )
    assert status == 200
    assert payload["config"]["dry_run"] is True
    assert payload["config"]["target"] is None
    assert len(payload["records"]) == 10


def test_dry_run_default_body_uses_sane_defaults(running_server):
    status, payload = _post_json(running_server, "/api/dry-run", {})
    assert status == 200
    assert payload["config"]["dry_run"] is True
    assert len(payload["records"]) > 0


def test_dry_run_rejects_unknown_category(running_server):
    status, payload = _post_json(
        running_server, "/api/dry-run", {"mix": ["not-a-category"], "count": 5, "seed": 0}
    )
    assert status == 400
    assert "error" in payload


def test_dry_run_rejects_malformed_json_body(running_server):
    req = urllib.request.Request(
        running_server + "/api/dry-run",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 400


def test_fire_without_confirm_is_refused(running_server):
    real_targets = config.load_targets()
    some_target = next(iter(real_targets))
    status, payload = _post_json(
        running_server,
        "/api/fire",
        {"target": some_target, "mix": ["clean"], "count": 3, "seed": 0},
    )
    assert status == 403
    assert "error" in payload


def test_fire_at_unconfigured_target_is_refused(running_server):
    status, payload = _post_json(
        running_server,
        "/api/fire",
        {
            "target": "https://evil.example.com/hook",
            "mix": ["clean"],
            "count": 3,
            "seed": 0,
            "confirm": True,
        },
    )
    assert status == 403
    assert "error" in payload


def test_fire_refusal_surfaces_the_canonical_guardrail_message(running_server):
    """End to end over real HTTP: an unconfigured target is refused by the
    canonical guardrail and its refusal reaches the client as a 403. This is
    the anti-un-wiring check at the transport layer - if the adapter ever
    stops delegating, the canonical wording disappears and this fails.
    """
    status, payload = _post_json(
        running_server,
        "/api/fire",
        {"target": "totally-made-up", "mix": ["clean"], "count": 3, "seed": 0, "confirm": True},
    )
    assert status == 403
    assert "not in the configured target list" in payload["error"]


def test_fire_with_truthy_string_confirm_is_refused(running_server):
    """A JSON body is attacker-shaped input: "false" is truthy in Python.
    The UI-layer explicit-confirm narrowing must reject it over HTTP too.
    """
    real_targets = config.load_targets()
    some_target = next(iter(real_targets))
    status, payload = _post_json(
        running_server,
        "/api/fire",
        {
            "target": some_target,
            "mix": ["clean"],
            "count": 3,
            "seed": 0,
            "confirm": "false",
        },
    )
    assert status == 403
    assert "error" in payload


def test_fire_with_configured_target_and_confirm_succeeds(
    running_server, fake_transport
):
    real_targets = config.load_targets()
    some_target = next(iter(real_targets))
    status, payload = _post_json(
        running_server,
        "/api/fire",
        {"target": some_target, "mix": ["clean"], "count": 4, "seed": 1, "confirm": True},
    )
    assert status == 200
    assert payload["config"]["dry_run"] is False
    assert payload["config"]["target"] == some_target
    assert len(payload["records"]) == 4
    # And it really did go through the transport, once per record, at the
    # configured target's URL.
    assert len(fake_transport.requests) == 4
    assert {r.url for r in fake_transport.requests} == {real_targets[some_target].url}


def test_dry_run_over_http_sends_nothing(running_server, fake_transport):
    """The dry-run/fire split has to hold over the real HTTP layer too, not
    just at the adapter. This is the test that would catch a UI regression
    where the default action quietly started sending."""
    status, payload = _post_json(
        running_server, "/api/dry-run", {"mix": ["clean"], "count": 8, "seed": 2}
    )
    assert status == 200
    assert len(payload["records"]) == 8
    assert fake_transport.requests == []


# ---------------------------------------------------------------------------
# The outcome annotation.
#
# web/static/app.js used to re-derive each record's outcome from its status
# code, which made the browser a third copy of core/report.py's rules that no
# test in this repo could check, because no CI here runs JavaScript. The
# server now annotates every record with the engine's verdict and the browser
# renders it.
#
# These tests pin the half of that which is checkable: the annotation the
# server sends is the engine's own verdict, for every record, on both
# endpoints. They deliberately do not assert particular outcomes, because the
# statuses depend on which generator produced the artifact and pinning them
# here would make this a test of the generator.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"mix": ["clean"], "count": 6, "seed": 0},
        {"mix": ["clean", "messy-but-valid", "degenerate"], "count": 24, "seed": 11},
        {"mix": [], "count": 15, "seed": 3},
    ],
)
def test_dry_run_records_arrive_with_the_engines_own_outcome(running_server, payload):
    status, body = _post_json(running_server, "/api/dry-run", payload)
    assert status == 200
    assert body["records"]
    for record in body["records"]:
        assert record["outcome"] == report.classify_record(record)


def test_fire_records_arrive_with_the_engines_own_outcome(running_server):
    real_targets = config.load_targets()
    some_target = next(iter(real_targets))
    status, body = _post_json(
        running_server,
        "/api/fire",
        {"target": some_target, "mix": ["clean", "degenerate"], "count": 12,
         "seed": 4, "confirm": True},
    )
    assert status == 200
    for record in body["records"]:
        assert record["outcome"] == report.classify_record(record)


def test_outcome_annotation_is_response_layer_only(running_server):
    """The annotation is a response convenience, not a schema change. The
    on-disk artifact and the shipped fixtures carry no `outcome` key, and
    core/report.py's record keys are asserted elsewhere. This keeps the two
    layers from quietly merging, which would put a display field into the
    artifacts Barrage replays."""
    fixture = json.loads(
        (FIXTURES_DIR / "sample_run_clean.json").read_text(encoding="utf-8")
    )
    assert all("outcome" not in record for record in fixture["records"])

    status, body = _post_json(
        running_server, "/api/dry-run", {"mix": ["clean"], "count": 2, "seed": 0}
    )
    assert status == 200
    assert all("outcome" in record for record in body["records"])


def test_unknown_post_path_is_404(running_server):
    status, payload = _post_json(running_server, "/api/no-such-endpoint", {})
    assert status == 404
