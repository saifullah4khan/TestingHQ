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
  needs to change.
- `web/config.py` / `web/targets.json` - the target allow-list that
  populates the dropdown. Loading fails loudly if a target is malformed,
  is not http(s), or names a host the canonical guardrail refuses.

## Guardrails: one definition, imported not copied

The web UI owns **no** guardrail rules of its own. `web/adapter.py` and
`web/config.py` import `testinghq.core.guardrails` and delegate to it, so
there is exactly one definition in the codebase of "may we send" and "is
this a host we are willing to fire at", and the UI inherits any future
hardening of it automatically. That module is imported, never edited here.

Two things sit on top, both additive and strictly narrowing:

- **Explicit confirm.** The canonical gate is "sending requires an
  explicit flag". The UI additionally requires that flag to be an
  unambiguous boolean `True`, so a stray truthy value in a JSON body
  (`"false"` is truthy in Python) can never read as consent.
- **Name-to-URL resolution.** The dropdown submits a configured target
  *name*, but the canonical public-host check parses a *host* out of its
  argument. A bare single-label name has no dot, so the guard would
  classify it as an internal host and pass it unconditionally. The adapter
  therefore also passes the resolved **URL** through the canonical guard,
  which is what makes the public-host check actually bite on the real
  destination. `tests/web/test_adapter.py` pins this wiring so it cannot
  silently regress.
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
confirm step both in the UI and on the server. A configured target that
points at a real, publicly routable host is refused by the canonical
guardrail even though it is in the allow-list; the UI never passes
`allow_public_hosts=True` to override that.
