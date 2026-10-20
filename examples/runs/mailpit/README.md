# Mailpit reference run

Every tool run against [Mailpit](https://github.com/axllent/mailpit) v1.31.6
(the `mailpit-linux-amd64` release binary, SHA-256 checked) on 2026-10-19,
with TestingHQ 1.0.0 plus the changes listed under `[Unreleased]` in the
changelog. The command, from the repository root:

```
sweep/run.sh mailpit
```

The system under test is the demo pipeline from `examples/pipeline_demo.py`,
served by `sweep/mailpit_pipeline.py` on `http://127.0.0.1:8000/intake`. It
forwards every ticket it opens over SMTP to Mailpit on `127.0.0.1:1025`,
addressed to `<category>@team.example.com` for the category its classifier
picked. The readback is the recipe in
[examples/readback/mailpit.toml](../../readback/mailpit.toml), unchanged:
Mailpit's `/api/v1/search` on `127.0.0.1:8025`. [config.toml](config.toml) is
that recipe with the three tables the demo pipeline needs appended. Mailpit
and the pipeline are cleared before each tool.

| Tool | Verdict | Exit | What it shows |
| --- | --- | --- | --- |
| [verify](verify.html) | VERIFIED | 0 | 20 of 20 found in Mailpit with sender and subject intact |
| [ledger](ledger.html) | BALANCED | 0 | 50 sent, 50 forwarded copies, each exactly once |
| [redeliver](redeliver.html) | DELIVERY-SAFE | 0 | a duplicate and a slow retry each left one copy |
| [loop](loop.html) | LOOP-SAFE | 0 | 13 kinds of machine mail, no ticket and no reply for any |
| [steady](steady.html) | STABLE | 0 | 105 variants, 0% dissent, label read from the forward's To |
| [blast --catalog](blast-catalog.html) | FLAGGED | 3 | 25 of 26 edge cases answered 2xx; one malformed payload got no response |
| [barrage](barrage.html) | STEADY | 0 | 288 requests at 5/s, p99 26ms, no errors, no knee |

[summary.json](summary.json) holds every step's command and exit code; each
`.txt` is `testinghq report` on the artifact beside it.

The blast finding is real: `structurally-malformed-0-0010` carries an
`envelope` field that is not valid JSON, and the demo pipeline's handler
raises on it and closes the connection without answering. Every other tool
sends well-formed payloads, which is why only blast sees it.

What the recipe can and cannot check is visible in the reports. Mailpit's
message summary carries a snippet rather than the body and a count rather than
attachment names, so verify reports those checks NOT CHECKED rather than
passing them. Its `ID` is per message, so the redeliver profile runs the two
scenarios that count copies, and not the threading ones, which need a ticket
store (see the recipe's field map notes).

Paths in the artifacts are relative to the repository root and point at the
run directory the sweep wrote, `sweep/runs/mailpit/20261019T094007Z/`; the
loop artifact's outbound sink path was recorded absolute and was rewritten
relative by `sweep/harness.py relativize`, as the sweep does for every run.
