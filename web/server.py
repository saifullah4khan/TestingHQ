"""Local dev server for the TestingHQ Blast web UI.

Stdlib only: `http.server` plus `json`. Nothing to build, nothing to install.
Serves the static single-page app and exposes two JSON endpoints:

    POST /api/dry-run   generate a run artifact WITHOUT sending. Default
                        action; never requires a target.
    POST /api/fire      generate and "send" a run artifact; requires a
                        target from the configured allow-list and an
                        explicit confirm flag. Refuses everything else.

Run with:
    python -m web.server
    python web/server.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):  # allows `python web/server.py` directly
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from web import adapter, config as config_module
else:
    from . import adapter, config as config_module

from testinghq.core import guardrails, report

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The HTTP client the fire path hands to the adapter. None means the engine's
# real transport client, which opens a socket.
#
# This exists because the adapter runs the real engine now, and the test suite
# must stay hermetic: web/targets.json's first entry is a localhost URL, so a
# fire test without an injected client would make a genuine request to
# localhost:8000 and sit there until the transport timeout expired. Before the
# adapter was on the real engine no fire path opened a socket at all, so this
# seam did not need to exist. Tests monkeypatch it; nothing in production does.
HTTP_CLIENT = None

_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
}

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def _annotate_outcomes(artifact):
    """Attach each record's outcome, computed by the engine, for the browser.

    This is a presentation-layer annotation and nothing more. The on-disk run
    artifact schema is unchanged and carries no `outcome` key, so fixtures and
    `core.report`'s schema tests are unaffected.

    It exists because `web/static/app.js` used to re-derive the outcome from
    the status code, which made the browser a third copy of the expectation
    rules. That copy could disagree with the engine and nothing would catch
    it, because no CI in this repo runs JavaScript. Annotating here means the
    rules are evaluated once, by the canonical implementation, and the
    browser only renders what it is told.
    """
    for record in artifact.get("records", []):
        record["outcome"] = report.classify_record(record)
    return artifact


class Handler(BaseHTTPRequestHandler):
    server_version = "TestingHQBlastUI/0.1"

    def log_message(self, fmt, *args):
        pass  # keep test/server output quiet; errors still surface in responses

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename):
        file_path = STATIC_DIR / filename
        if not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = file_path.read_bytes()
        content_type = _CONTENT_TYPES.get(file_path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            self._handle_config()
            return
        filename = _STATIC_FILES.get(path)
        if filename:
            self._send_static(filename)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/dry-run":
            self._handle_dry_run()
            return
        if path == "/api/fire":
            self._handle_fire()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_config(self):
        try:
            targets = config_module.load_targets()
        except config_module.ConfigError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(
            200,
            {
                "targets": [{"name": t.name} for t in targets.values()],
                "categories": list(report.CATEGORIES),
            },
        )

    def _handle_dry_run(self):
        try:
            body = _read_json_body(self)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        mix = body.get("mix") or []
        count = body.get("count", 20)
        seed = body.get("seed", 0)
        try:
            artifact = adapter.dry_run(mix, count, seed)
        except adapter.GeneratorError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, _annotate_outcomes(artifact))

    def _handle_fire(self):
        try:
            body = _read_json_body(self)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        target = body.get("target")
        mix = body.get("mix") or []
        count = body.get("count", 20)
        seed = body.get("seed", 0)
        confirm = body.get("confirm", False)
        try:
            artifact = adapter.fire(
                target, mix, count, seed, confirm, client=HTTP_CLIENT
            )
        except guardrails.GuardrailError as exc:
            # Canonical guardrail refusal: unconfigured target, unsafe public
            # host, or no explicit confirm. All are a refusal, not a bug.
            self._send_json(403, {"error": str(exc)})
            return
        except config_module.ConfigError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        except adapter.GeneratorError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, _annotate_outcomes(artifact))


def make_server(host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the TestingHQ Blast web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    httpd = make_server(args.host, args.port)
    print(f"TestingHQ Blast web UI: http://{args.host}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
