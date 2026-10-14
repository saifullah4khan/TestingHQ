# Contributing

## Set up

    git clone https://github.com/saifullah4khan/TestingHQ.git
    cd TestingHQ
    python -m venv .venv && . .venv/bin/activate
    pip install -e ".[dev]"

Python 3.10 is the floor. Runtime code uses the standard library only (plus
`tomli` below 3.11); a new runtime dependency needs a strong case.

## Check

    pytest -q -m "not allow_network"   # hermetic suite, what CI's tests job runs
    pytest -q -m allow_network         # real loopback sockets, what e2e runs
    ruff check .                       # pyflakes rules, see ruff.toml
    node --test tests/report_html/*.test.cjs

The suite blocks outbound sockets by default. A test that needs loopback is
marked `@pytest.mark.allow_network` and lives in `tests/e2e/` or
`tests/integration/`; collection fails anywhere else.

Test behaviour through public functions or the CLI. Do not add tests that
assert README wording, CI YAML, docstrings or that a file is absent; see
`docs/decisions/0003-what-the-suite-protects.md`.

## Commits and pull requests

- Lowercase `type: subject` (feat, fix, refactor, test, docs, chore, ci),
  subject under 72 characters, and a body that says why.
- No em-dashes anywhere, in code, comments, docs or commit messages.
- Every behaviour change ships with a test that fails before and passes after.
- Update `CHANGELOG.md` under `[Unreleased]`. A change to a flag, exit code,
  config key or the `report --json` shape is breaking.
- Both required checks, `tests` and `end-to-end`, must be green.
