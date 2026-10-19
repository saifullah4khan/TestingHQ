# The helpdesk sweep

Runs every TestingHQ tool against five open-source helpdesks in Docker, one
command per helpdesk. Needs Docker with Compose, bash (Git Bash on Windows),
curl, Python 3.10+ with `pip install -e .` from the repository root, and for
osTicket and FreeScout, whose readback adapters read MySQL, `pip install
PyMySQL` in the same interpreter. run.sh checks these before starting.

```
sweep/run.sh chatwoot     # or osticket, zammad, freescout, frappe
sweep/run.sh all          # the five in turn; SWEEP_KEEP=1 leaves each running
sweep/run.sh mailpit      # no Docker: a pinned Mailpit binary and the demo pipeline
```

For a helpdesk `<h>` the run starts `compose/<h>.yml`, runs `setup/<h>.sh`
(which creates the account, inbox and API token and writes `sweep/.env.<h>`),
loads `profiles/<h>.env` and that `.env`, then fires at the target in
`configs/<h>.toml` from the repository root, with the root on `PYTHONPATH` so
a config's custom adapter imports. The tools run in this order: `config
validate`, `verify`, `ledger`, `redeliver`, `loop` and `steady` first, while
the helpdesk holds only their own tagged mail; then `blast fire --catalog`,
`blast fire --corpus` over Zammad's test mail (when `SWEEP_CORPUS=1`; cloned
into `sweep/.cache/`, never copied into the repository), and `barrage` at 5
per second for 60 seconds (when `SWEEP_BARRAGE=1`) last, since a helpdesk may
still be ingesting its load when the run ends. `report --html` then renders
every artifact and reads nothing from the helpdesk.

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
