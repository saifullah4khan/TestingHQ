# Decision 0003: what the suite protects

Date: 2026-10-15. Status: accepted, 2026-10-15.

For 1.0 the suite tests the product: its public functions and its CLI. About
155 tests that asserted README wording, CI YAML contents, docstrings, the
absence of files or a punctuation rule are removed. Each one encoded a real
lesson, so the lesson is kept here and, where it still applies, carried by a
smaller check, a CI job or `CONTRIBUTING.md`.

## Kept

- No duplicate test function names in a module (`tests/test_repo_invariants.py`).
- `allow_network` only under `tests/e2e/` or `tests/integration/`, enforced at
  collection in `tests/conftest.py`.
- The network block, plus every product send path raising under it
  (`test_suite_network_block.py`, four tests).
- Version agreement, a changelog section for the version, and a wheel that
  builds with its package data (`test_release.py`, three tests).
- Every non-Python file in the package declared as package data.
- `docs/CONFIG.md` equals what the generator produces.
- No test reads `.env` (`test_dogfood_harness.py`, one test).
- Exit-code tests that import and call code; README examples run as dry runs.

## Removed, and the lesson each had encoded

| Removed | Lesson | Where it lives now |
| --- | --- | --- |
| `test_readme_status.py` | A status section drifts from the CLI; a hand-kept tool count goes stale within weeks; a deleted file leaves dangling links. Its path split also made the suite red in any clone not named TestingHQ. | README examples run as dry runs; the README is short enough to review. |
| `test_ci_gate_names.py` | Branch protection matches the exact context `tests` and `end-to-end`; a job renamed by a matrix blocks every merge, and a gate without `if: always()` reports a skipped leg as success. | Comments in `ci.yml` and `e2e.yml`. Check the ruleset when renaming a job. |
| `test_python_support_claims.py` | `requires-python` and the CI matrix must be the same set, and below 3.11 the TOML parser is the `tomli` dependency. | The four-version matrix in `ci.yml` and the conditional dependency in `pyproject.toml`. |
| `test_user_facing_claims.py` | Claims on a landing page are written optimistically and corrected never. | `site/` is deleted. |
| `test_suite_time_budget.py` | A hermetic test that waits on a real clock slows every run and hides why. | The budget itself stays in `tests/conftest.py`; only its self-tests went. |
| `test_cli_package.py` | One module per command keeps the CLI navigable, and only `main.py` knows the command list. | Code review. |
| `test_hermetic_suite.py` | The two CI selections must partition the suite: the e2e job selects by marker, not by directory, or exempt tests outside that directory run nowhere. | The collection check in `tests/conftest.py`, and `-m allow_network` in `e2e.yml`. |
| Most of `test_repo_invariants.py` | One copy of the guardrails and the outcome rules, never a second in the web UI; a removed function can survive as a reference; the em-dash rule. | The web UI is deleted. The style rule is in `CONTRIBUTING.md`. |
| Most of `test_release.py` | A tag must match the version, publish only on `v*` tags, trusted publishing with no API token, the changelog checked before upload, no `import tomllib` in tests. | The guards in `release.yml`; the 3.10 matrix leg fails on a stray `tomllib` import. |
| Most of `test_dogfood_harness.py` | The HandleHQ recipe must not claim a run that never happened, and `.env` must be gitignored. | `.gitignore` and `docs/DOGFOOD.md`. |
| Source scans in `test_exit_codes.py` | Exit codes are defined once and no tool invents a fifth. | `core/exit_codes.py` is the only definition, and the remaining tests call the code. |
| Drift extras in `test_config_schema.py` | `--check` mode and the "generated file" banner. | The one drift test covers both. |
| `test_suite_network_block.py` extras | The marker is registered, the block covers all three socket doors, the demo runs in e2e. | `pyproject.toml` markers; `e2e.yml`. |
| `tests/web/`, `web/tests/render.test.cjs` | No JavaScript ran in CI, so a page that threw on every load stayed green. | `tests/report_html/render.test.cjs`, run by the `report-html-tests` job against the installed wheel. |

## Why

A test that reads prose or YAML fails on a reformat and passes on a wrong
behaviour. The ones above caught real mistakes once, and the mistakes are now
prevented by structure (the file is gone, the job exists, the check runs at
collection) rather than by a second copy of the text being policed.
