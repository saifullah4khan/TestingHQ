# TestingHQ - Blast web UI

A small, dependency-free single-page app over the Blast engine. Vanilla
HTML/CSS/JS on the front end, Python's stdlib `http.server` on the back end.
Nothing to build, nothing to `npm install`.

## Run it

```
cd wt-web-ui
python -m web.server
```

Then open http://127.0.0.1:8765/ in a browser. Use `--port` to pick a
different port, `--host` to bind elsewhere.

## What it does

- Pick a mix of categories (clean, messy-but-valid, multilingual-gibberish,
  structurally-malformed, degenerate), a count, and a seed.
- **Dry run** (the default action) generates a run artifact and shows it.
  It never sends anything anywhere.
- **Fire** requires picking a target from a dropdown populated from the
  server's configured allow-list (`web/targets.json`), and an explicit
  confirm step in the UI before the request is sent. There is no free-text
  target field anywhere; the server re-validates the target server-side
  too, so the guardrail cannot be bypassed by editing the page.
- The results panel reads the run **by expectation**, not by status code:
  a degenerate payload that gets a clean 4xx is a PASS, a degenerate
  payload that gets a 500 or hangs is a FAIL, and a clean payload that does
  not get a 2xx is a FAIL. Both failure classes are called out as separate
  highlighted counts, and every offending row in the results table is
  highlighted the same way.

## How it's built

- `web/expectations.py` - pure functions implementing the expectation
  rules above (`classify_record`, `flag_for_record`, `compute_summary`).
  No I/O, directly unit tested.
- `web/generator.py` - a deterministic, fixture-backed stand-in for the
  real Blast engine (`testinghq/blast/generate.py`, which does not exist
  on this branch yet). Same seed/mix/count always produces the same
  artifact.
- `web/adapter.py` - the single seam between the UI and the engine.
  `dry_run()` and `fire()` are the only two functions the rest of the app
  calls to get a run artifact; when the real engine lands, only this file
  needs to change. It also owns the fire guardrails (configured target
  required, explicit `confirm=True` required) independent of
  `testinghq/core/guardrails.py`, so this lane builds and tests without
  depending on code owned by the parallel engine lane.
- `web/config.py` / `web/targets.json` - the target allow-list. Every
  target must resolve to a reserved demo domain
  (example.com/.net/.org/.edu, or a .test/.invalid/.example/.localhost
  host) - loading fails loudly otherwise.
- `web/server.py` - the stdlib HTTP server: serves `web/static/` and
  exposes `POST /api/dry-run` and `POST /api/fire`.
- `web/static/` - the actual page (`index.html`, `style.css`, `app.js`).
  `app.js` intentionally mirrors the classification rules in
  `expectations.py` so per-row highlighting in the table agrees with the
  summary panel.
- `web/tests/fixtures/` - two sample run artifacts matching the documented
  schema (one clean, one with both highlighted failure classes present),
  used by the test suite in `tests/web/` as a schema contract check.

## Run the tests

```
cd wt-web-ui
python -m pytest -q
```

The server tests bind to `127.0.0.1` on an OS-assigned ephemeral port in a
background thread; no external network is used anywhere in the suite.

## Responsible use

All demo content is synthetic and lives on reserved domains only. Dry-run
is the default. Firing is only possible at a target the operator has
explicitly configured in `web/targets.json`, and only after an explicit
confirm step both in the UI and on the server.
