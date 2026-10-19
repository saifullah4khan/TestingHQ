# Custom readback adapters

Worked `module:attribute` adapters for systems the built-in `http` and
`mailbox` kinds cannot read, usually because the system has no API to read a
ticket back and the readback has to go to its database.

Each file here is an example, not part of the `testinghq` package. Its
docstring says which system it reads, which tables and columns, which keys
its `[readback]` table takes, and which library it needs. The recipe that
uses it is next door in `examples/readback/`.

## Running one

From the root of a TestingHQ checkout, with the root importable and the
adapter's own library installed:

```
pip install PyMySQL
PYTHONPATH=. testinghq verify fire --target osticket --send --config examples/readback/osticket.toml
```

The recipe names the adapter by its import path from the root, for example
`kind = "examples.readback.adapters.osticket_mysql:OsTicketMysqlAdapter"`.

## What every adapter here does the same way

- **Its library is its own.** A database driver is installed next to
  TestingHQ, never added to TestingHQ's dependencies. It is imported in the
  code that connects, not at the top of the module, so a config naming the
  adapter loads and `config validate` runs without it, and a run without it is
  refused at `preflight()`, before anything is sent, naming the package.
- **Secrets are `env:` references.** The DSN is a key of the adapter's own
  table written as `dsn = "env:NAME"` and resolved with
  `testinghq.core.config.resolve_env_references`. A literal is refused, by the
  same rule as `[readback.headers]`.
- **Read only.** Give the adapter a database user that can only SELECT.
- **Every read ends its transaction.** A MySQL connection holding a
  transaction open keeps seeing the tables as they were at its first read, so
  a poll through it would never see a new ticket.
- **Testable without the database.** The adapter class takes `connect=`, any
  DB-API connection factory, so the tests run it against SQLite tables with the
  same names.
