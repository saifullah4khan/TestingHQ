# Examples

Runnable examples and sample configuration. They show usage; the test suite is
under `tests/`.

## pipeline_demo.py

```
python examples/pipeline_demo.py
```

Starts a small inbound-email pipeline on 127.0.0.1 and runs `verify`, `ledger`,
`redeliver`, `loop` and `steady` against it over real sockets, about a minute
in all. The pipeline is the system under test and imports nothing from
TestingHQ: it parses the SendGrid-shaped webhook, recognises machine mail by
its own reading of RFC 3834, finds the tag with its own regex, deduplicates on
Message-ID, threads replies (including a reply that arrives before its
original), acknowledges new tickets by mail, and labels each message with a
keyword classifier that reads the content.

Each tool runs once against the correct pipeline and once with a defect
switched on, and the script fails unless every run exits as expected:

| Run | Defect | Expected exit |
| --- | --- | --- |
| verify, ledger | none | 0 |
| ledger | a worker writes every fifth message twice | 3 |
| ledger, verify | every third message dropped after a 200 | 3 |
| redeliver | none | 0 |
| loop | none | 0 |
| loop, twice | machine mail ticketed and acknowledged; then with `--ticket-policy allowed` | 3 |
| steady | none | 0 |
| steady | the classifier reads only the first 60 characters, so a forwarded message loses its label | 3 |

Every status code in every run is 200. The defects are on the write side,
where only a tool that reads the system back can see them. The README's ledger
report comes from the duplicate run.

## demo.py

```
python examples/demo.py
python examples/demo.py --count 50 --seed 7
```

Builds a small deterministic clean corpus and serializes each payload the way
the transport would. It never opens a socket: `--send` fires at an in-process
recording fake.

## target.example.toml

A sample config: two named targets (`url`, optional `format`) and a
`[readback]` table with its field map. Copy it to `target.toml`, point `url` at
an intake endpoint you own or are authorised to test, and check it before
sending anything:

```
testinghq config validate target.toml
testinghq blast fire --target local
```

`--config` defaults to `./target.toml`. Every key is described in
[docs/CONFIG.md](../docs/CONFIG.md).

## readback/

Worked `[readback]` tables, one per kind of system:

| Recipe | System | Status |
| --- | --- | --- |
| `generic-rest.toml` | any JSON API with a tag filter | run by this repository's tests over loopback |
| `mail-sink.toml` | a JSON Lines mail sink | run by this repository's tests |
| `mailpit.toml` | Mailpit's search API | written from Mailpit's API documentation |
| `chatwoot.toml` | Chatwoot conversation search | written from Chatwoot's API documentation |
| `zendesk.toml` | Zendesk Search API | written from Zendesk's API documentation |
| `freshdesk.toml` | Freshdesk Search API | written from Freshdesk's API documentation |
| `handlehq.toml` | a HandleHQ deployment | a template; see [docs/DOGFOOD.md](../docs/DOGFOOD.md) |

Each file's first lines say what was verified and what was not. Credentials are
always `env:` references, never literals.
