# TestingHQ - Blast web UI

A small, dependency-free single-page app over the Blast engine. Vanilla
HTML/CSS/JS on the front end, Python's stdlib `http.server` on the back end.
Nothing to build, nothing to `npm install`.

## Run it

```
python -m web.server
```

Then open http://127.0.0.1:8765/ in a browser. Use `--port` to pick a
different port, `--host` to bind elsewhere. Run it from the repository
root, not from inside `web/`.

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

- `web/expectations.py` - a re-export of the expectation rules from
  `testinghq/core/report.py`. It defines none of them itself. See the
  "one definition" section below.
- `web/adapter.py` - the single seam between the UI and the engine.
  `dry_run()` and `fire()` are how the rest of the app gets a run artifact.
  Both build their corpus with `testinghq.blast.generate` and
  `testinghq.blast.corrupt`, and both build records with
  `testinghq.core.report`; only `fire()` posts, through
  `testinghq.core.transport`. It also owns `GeneratorError`, the 400-mapped
  error for a malformed mix, count, or seed.
- `web/config.py` / `web/targets.json` - the target allow-list that
  populates the dropdown. Loading fails loudly if a target is malformed,
  is not http(s), or names a host the canonical guardrail refuses.
- `web/server.py` - the stdlib HTTP server: serves `web/static/` and
  exposes `POST /api/dry-run`, `POST /api/fire` and `GET /api/config`. The
  last one is what fills the dropdown, which is why a request to it is how
  the page learns which targets exist.
- `web/static/` - the actual page: `index.html`, `style.css`, `render.js`
  and `app.js`. `render.js` holds the presentation and is loaded before
  `app.js`, which reads it at load time; `app.js` only wires the form to the
  API. `render.js` is what `web/tests/render.test.cjs` runs under Node.
  `app.js` does not classify anything: the server annotates every record
  with its `outcome`, computed by the engine, and the browser renders that.
  The one place the engine's report module is reached for is
  `web/server.py`, which calls `classify_record` to produce that
  annotation on the way out. That is the engine computing it, not the web
  lane re-deriving it.
- `web/tests/fixtures/` - two sample run artifacts matching the documented
  schema (one clean, one with both highlighted failure classes present),
  used by the test suite in `tests/web/` as a schema contract check.

There is no local generator. There used to be: `web/generator.py` was a
deterministic stand-in written when the engine modules did not exist, and it
was deleted once the adapter moved onto the real engine. It had a
`generate_run()` that produced convincing artifacts and its own error class,
which is precisely what makes a stale stand-in dangerous: it keeps working and
  nothing complains when something gets wired to it.
  `tests/test_repo_invariants.py`

now fails if anything imports it, and the fixtures survive as data.

## One definition, imported not copied

The expectation rules live in exactly one place, `testinghq/core/report.py`:

- `web/expectations.py` re-exports them and defines none of them.
- `web/static/app.js` used to carry a second, JavaScript copy that no test
  in this repo could reach. It is gone. The server annotates each record
  with the engine's verdict and the browser renders it. The annotation is
  response-layer only; the on-disk artifact schema is unchanged.
- `tests/test_repo_invariants.py` fails the build if a rule body reappears in
  `web/expectations.py`, or if `classifyRecord` or its `is2xx`/`is5xx`/
  `isTimeout` helpers reappear in `web/static/app.js` or `web/static/render.js`.
  It rejects the name anywhere in the file, not only a definition, which is
  the version that matters: a leftover *reference* to a function that had been
  removed is a `ReferenceError` on every page load, and it survived an earlier
  definition-only check for exactly that long.

This is not tidiness. The web lane previously duplicated the *guardrails*,
the security lane hardened the canonical copy, and the two copies disagreed:
a target the CLI refused was one the UI would have fired at. Two correct
copies are worse than one, because nothing in the build notices.

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

## Run the tests

```
python -m pytest -q tests/web
```

The server tests bind to `127.0.0.1` on an OS-assigned ephemeral port in a
background thread. The fire path takes an injectable HTTP client, pinned to
a recording fake by an autouse fixture, so no test in the suite reaches the
network even though `web/targets.json` lists a localhost URL.

## Responsible use

All demo content is synthetic and lives on reserved domains only. Dry-run
is the default. Firing is only possible at a target the operator has
explicitly configured in `web/targets.json`, and only after an explicit
confirm step both in the UI and on the server. A configured target that
points at a real, publicly routable host is refused by the canonical
guardrail even though it is in the allow-list; the UI never passes
`allow_public_hosts=True` to override that.

**The web UI does not rate limit.** This is the one protection it does not
inherit from the CLI. Every command-line firing path goes through a
`TokenBucket`; `fire()` here posts in a plain loop, so the ceiling is the
corpus size and the target's response time rather than a rate the operator
set. Firing from a browser is pacing itself, not being paced.

It is bounded in the ways that matter: the target is one the operator has
already put in the allow-list, the corpus is Blast's default, and the server
binds loopback unless the operator passes `--host`. But keep browser-initiated
runs short. `docs/SECURITY.md` records this as a known gap.
