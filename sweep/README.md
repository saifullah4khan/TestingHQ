# The helpdesk sweep

Runs every TestingHQ tool against five open-source helpdesks in Docker, one
command per helpdesk. Needs Docker with Compose, bash (Git Bash on Windows),
Python 3.10+ and `pip install -e .` from the repository root.

```
sweep/run.sh chatwoot     # or osticket, zammad, freescout, frappe
sweep/run.sh all          # the five in turn; SWEEP_KEEP=1 leaves each running
sweep/run.sh mailpit      # no Docker: a pinned Mailpit binary and the demo pipeline
```

For a helpdesk `<h>` the run starts `compose/<h>.yml`, runs `setup/<h>.sh`
(which creates the account, inbox and API token and writes `sweep/.env.<h>`),
loads `profiles/<h>.env` and that `.env`, then fires at the target in
`configs/<h>.toml`: `config validate`, `blast fire --catalog`, `blast fire
--corpus` over Zammad's test mail (when `SWEEP_CORPUS=1`; cloned into
`sweep/.cache/`, never copied into the repository), `verify`, `ledger`,
`redeliver`, `loop`, `steady`, `barrage` at 5 per second for 60 seconds (when
`SWEEP_BARRAGE=1`), and `report --html` on every artifact.

Everything lands in `sweep/runs/<h>/<UTC timestamp>/`: each tool's JSON
artifact, its HTML report, a log per step, and `summary.json` with every
step's command and exit code and an overall `result` of `passed`, `findings`
or `error`. A failing step never stops the run. The script exits 0 when every
step completed (a tool exit of 0, or 3 for a finding) and 1 otherwise.

The profile sets `SWEEP_MAX_WAIT` and `SWEEP_QUIET_WINDOW` (passed to every
tool that reads back), `SWEEP_CORPUS`, `SWEEP_BARRAGE` and `SWEEP_HEALTH_URL`,
and optionally `SWEEP_READBACK_RATE`, `SWEEP_EXPECT_ROUTE` (the route verify,
ledger and redeliver expect, such as Zammad's `Users` group) and
`SWEEP_ARGS_<TOOL>`, flags appended to one tool (for example
`SWEEP_ARGS_REDELIVER="--retry-after 30"`). A variable already set in the
environment wins over the profile. Profiles and `.env` files are read as
`KEY=VALUE` lines, never sourced, so a value may hold spaces.

Helpdesks that fetch mail over POP3 or IMAP instead of taking a webhook are
described in [PULL_MAIL.md](PULL_MAIL.md).
