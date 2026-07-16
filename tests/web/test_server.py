"""Tests for web/server.py.

Runs the real stdlib server on 127.0.0.1 with an OS-assigned ephemeral port
in a background thread - loopback only, no external network, fully
hermetic. Exercises the actual HTTP layer (routing, JSON (de)serialization,
status codes) rather than calling handler methods directly.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from web import config, server


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


def test_fire_with_configured_target_and_confirm_succeeds(running_server):
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


def test_unknown_post_path_is_404(running_server):
    status, payload = _post_json(running_server, "/api/no-such-endpoint", {})
    assert status == 404
