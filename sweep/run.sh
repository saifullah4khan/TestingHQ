#!/usr/bin/env bash
# The helpdesk sweep: start a helpdesk, set it up, run every TestingHQ tool
# against it, render every report, and write summary.json. See sweep/README.md.
#
#   sweep/run.sh <helpdesk>   chatwoot, osticket, zammad, freescout or frappe
#   sweep/run.sh all          the five in turn, each stopped after its run
#                             unless SWEEP_KEEP=1
#   sweep/run.sh mailpit      no Docker: a pinned Mailpit release binary and
#                             the demo pipeline from examples/pipeline_demo.py
#
# Artifacts land in sweep/runs/<helpdesk>/<UTC timestamp>/. A step that fails
# does not stop the run. Exit status: 0 when every step completed, which for a
# testinghq tool is exit 0 or 3 (a finding: read its report); 1 when any step
# errored, was refused or was blocked by an earlier failure. sweep/harness.py
# has the reasoning.
#
# Environment: PYTHON (default python3, then python), TESTINGHQ (the command
# that runs the CLI; default $PYTHON -m testinghq.cli, else testinghq on PATH).
# The profile sweep/profiles/<helpdesk>.env sets SWEEP_MAX_WAIT,
# SWEEP_QUIET_WINDOW, SWEEP_CORPUS, SWEEP_BARRAGE and SWEEP_HEALTH_URL, and
# optionally SWEEP_READBACK_RATE, SWEEP_EXPECT_ROUTE (--expect-route for
# verify, ledger and redeliver) and SWEEP_ARGS_<TOOL>; a variable already set
# in the environment wins over the profile.
set -euo pipefail

HELPDESKS="chatwoot osticket zammad freescout frappe"

MAILPIT_VERSION=v1.31.6
MAILPIT_RELEASES=https://github.com/axllent/mailpit/releases/download
MAILPIT_HTTP_PORT=8025
MAILPIT_SMTP_PORT=1025
PIPELINE_PORT=8000

ZAMMAD_REPO=https://github.com/zammad/zammad.git
ZAMMAD_REF_DEFAULT=7.2.2
CORPUS_COUNT=100

BARRAGE_RATE=5
BARRAGE_DURATION=60

# Settings copied into summary.json. None of these is a secret: credentials
# live in sweep/.env.<helpdesk> and reach the tools through env: references.
SETTINGS="SWEEP_MAX_WAIT SWEEP_QUIET_WINDOW SWEEP_READBACK_RATE SWEEP_EXPECT_ROUTE SWEEP_CORPUS
SWEEP_BARRAGE SWEEP_HEALTH_URL SWEEP_ZAMMAD_REF SWEEP_CORPUS_DIR SWEEP_KEEP
SWEEP_ARGS_BLAST SWEEP_ARGS_VERIFY SWEEP_ARGS_LEDGER SWEEP_ARGS_REDELIVER
SWEEP_ARGS_LOOP SWEEP_ARGS_STEADY SWEEP_ARGS_BARRAGE"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.."

# Every path below is relative to the repository root, so commands and
# artifacts carry no machine-specific prefix and Windows path translation
# never sees an absolute MSYS path.
HARNESS=sweep/harness.py
CACHE_DIR=sweep/.cache
RUNS_DIR=sweep/runs

die() {
  printf 'sweep: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: sweep/run.sh <%s|all|mailpit>\n' "${HELPDESKS// /|}" >&2
  exit 1
}

find_python() {
  local candidate
  if [ -n "${PYTHON:-}" ]; then
    export PYTHON
    return 0
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      PYTHON=$candidate
      export PYTHON
      return 0
    fi
  done
  die "no Python 3.10 or newer found; set PYTHON"
}

# The repository root goes on PYTHONPATH, relative, so a config's custom
# adapter (`kind = "examples.readback.adapters.osticket_mysql:..."`) imports,
# and so `$PYTHON -m testinghq.cli` runs this checkout's code.
set_pythonpath() {
  local sep
  sep=$("$PYTHON" -c 'import os; print(os.pathsep)')
  PYTHONPATH=".${PYTHONPATH:+$sep$PYTHONPATH}"
  export PYTHONPATH
}

find_cli() {
  if [ -n "${TESTINGHQ:-}" ]; then
    read -r -a THQ <<<"$TESTINGHQ"
  elif "$PYTHON" -c 'import testinghq' >/dev/null 2>&1; then
    THQ=("$PYTHON" -m testinghq.cli)
  elif command -v testinghq >/dev/null 2>&1; then
    THQ=(testinghq)
  else
    die "testinghq is not importable by $PYTHON; run pip install -e . from the repository root"
  fi
}

# require_helpdesk_tools HELPDESK: what compose, the setup scripts and the
# readback adapters need, checked before anything starts.
require_helpdesk_tools() {
  local h=$1 tool
  for tool in docker curl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      die "$h: $tool is not on PATH; the compose stack and setup scripts need it"
    fi
  done
  case $h in
    osticket | freescout)
      if ! "$PYTHON" -c 'import pymysql' >/dev/null 2>&1; then
        die "$h: its readback adapter reads MySQL through PyMySQL, which $PYTHON cannot import; run: $PYTHON -m pip install PyMySQL"
      fi
      ;;
  esac
}

# load_env_file FILE keep|override
# Reads KEY=VALUE lines without executing the file: comments and blank lines
# are skipped, a trailing CR is dropped (a profile saved on Windows), an
# `export ` prefix and one pair of surrounding quotes are removed, and a value
# may hold spaces (`Basic dXNlcjpwYXNz`). With keep, a variable the caller
# already set wins; with override, the file wins.
load_env_file() {
  local file=$1 mode=$2 line key value
  if [ ! -f "$file" ]; then
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    case $line in
      '' | '#'*) continue ;;
    esac
    line=${line#export }
    case $line in
      *=*) ;;
      *) continue ;;
    esac
    key=${line%%=*}
    key=${key%"${key##*[![:space:]]}"}
    value=${line#*=}
    case $key in
      '' | [0-9]* | *[!A-Za-z0-9_]*)
        printf 'sweep: skipping malformed line in %s\n' "$file" >&2
        continue
        ;;
    esac
    if [ "${#value}" -ge 2 ]; then
      case $value in
        \"*\" | \'*\') value=${value:1:${#value}-2} ;;
      esac
    fi
    if [ "$mode" = keep ] && [ -n "${!key+set}" ]; then
      continue
    fi
    export "$key=$value"
  done <"$file"
}

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# show_cmd ARG...: the command as one line a reader can paste.
show_cmd() {
  local out="" arg
  for arg in "$@"; do
    case $arg in
      '' | *[!A-Za-z0-9_./:=,@%+-]*) arg="'${arg//\'/\'\\\'\'}'" ;;
    esac
    out="$out${out:+ }$arg"
  done
  printf '%s' "$out"
}

# record PHASE STEP TOOL EXIT_CODE ARTIFACT HTML LOG SKIPPED COMMAND
# One tab-separated line in the step log; sweep/harness.py reads it back.
record() {
  local line="" field
  for field in "$@"; do
    field=${field//$'\t'/ }
    field=${field//$'\n'/ }
    field=${field//$'\r'/}
    line="$line${line:+$'\t'}$field"
  done
  printf '%s\n' "$line" >>"$STEPS"
}

# start_run NAME: a fresh run directory and step log.
start_run() {
  RUN_NAME=$1
  RUN_DIR="$RUNS_DIR/$RUN_NAME/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$RUN_DIR"
  STEPS="$RUN_DIR/steps.tsv"
  : >"$STEPS"
  STARTED=$(utc_now)
  ARTIFACT_STEPS=""
  BEFORE_STEP=""
  printf 'sweep: %s, artifacts in %s\n' "$RUN_NAME" "$RUN_DIR"
}

# run_step NAME TOOL ARTIFACT DISPLAY CMD...
# Runs CMD with its output shown and saved to NAME.log, records its exit code
# and sets LAST_CODE. Never stops the run.
run_step() {
  local name=$1 tool=$2 artifact=$3 display=$4 code
  shift 4
  printf '\n== %s\n$ %s\n' "$name" "$display"
  set +e
  ("$@") 2>&1 | tee "$RUN_DIR/$name.log"
  code=${PIPESTATUS[0]}
  set -e
  record step "$name" "$tool" "$code" "$artifact" "" "$name.log" "" "$display"
  LAST_CODE=$code
}

# thq_step NAME TOOL ARTIFACT ARG...: a testinghq command as a step.
thq_step() {
  local name=$1 tool=$2 artifact=$3
  shift 3
  if [ -n "$BEFORE_STEP" ]; then
    "$BEFORE_STEP"
  fi
  run_step "$name" "$tool" "$artifact" "$(show_cmd testinghq "$@")" "${THQ[@]}" "$@"
  if [ -n "$artifact" ]; then
    ARTIFACT_STEPS="$ARTIFACT_STEPS $name"
  fi
}

skip_step() {
  local name=$1 tool=$2 reason=$3
  printf '\n== %s: skipped (%s)\n' "$name" "$reason"
  record step "$name" "$tool" "" "" "" "" "$reason" ""
}

# extra_args VAR: EXTRA holds VAR's value split on whitespace.
extra_args() {
  EXTRA=()
  local value=${!1:-}
  if [ -n "$value" ]; then
    read -r -a EXTRA <<<"$value"
  fi
}

upper() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

# The readback options the five pipeline tools take.
readback_args() {
  READBACK_ARGS=()
  if [ -n "${SWEEP_MAX_WAIT:-}" ]; then
    READBACK_ARGS+=(--max-wait "$SWEEP_MAX_WAIT")
  fi
  if [ -n "${SWEEP_QUIET_WINDOW:-}" ]; then
    READBACK_ARGS+=(--quiet-window "$SWEEP_QUIET_WINDOW")
  fi
  if [ -n "${SWEEP_READBACK_RATE:-}" ]; then
    READBACK_ARGS+=(--readback-rate "$SWEEP_READBACK_RATE")
  fi
}

# fetch_corpus DEST REF: Zammad's test mail, cloned shallow and sparse. Zammad
# is AGPL, so the files are read from this cache and never copied into the
# repository.
fetch_corpus() {
  local dest=$1 ref=$2
  if [ -d "$dest/test/data/mail" ] && [ "$(cat "$dest/.sweep-ref" 2>/dev/null)" = "$ref" ]; then
    printf 'corpus: cached at %s (%s)\n' "$dest/test/data/mail" "$ref"
    return 0
  fi
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  git clone --quiet --depth 1 --branch "$ref" --filter=blob:none --sparse "$ZAMMAD_REPO" "$dest"
  git -C "$dest" sparse-checkout set test/data/mail
  printf '%s\n' "$ref" >"$dest/.sweep-ref"
}

# The order is deliberate. The readback tools run first, while the store
# holds only tagged mail of this run's earlier tools, so a listing for stray
# records is not crowded by untagged blast and corpus mail and a slow
# helpdesk is not still ingesting it. Blast and the corpus follow. Barrage
# is last, because a helpdesk may still be working through its load when the
# run ends; only report, which reads the artifacts, comes after it.
ORDER_NOTE="readback tools (verify, ledger, redeliver, loop, steady) run first,
while the helpdesk holds only their own tagged mail; blast, the corpus and
barrage follow, barrage last since ingestion may lag its load; report reads
only the artifacts"

# run_tools CONFIG TARGET: every tool in order, then a report per artifact.
run_tools() {
  local config=$1 target=$2 out=$RUN_DIR tool name
  local common=(--config "$config" --target "$target")
  readback_args

  thq_step config-validate config "" config validate "$config"

  for tool in verify ledger redeliver loop steady; do
    extra_args "SWEEP_ARGS_$(upper "$tool")"
    local route=()
    case $tool in
      verify | ledger | redeliver)
        if [ -n "${SWEEP_EXPECT_ROUTE:-}" ]; then
          route=(--expect-route "$SWEEP_EXPECT_ROUTE")
        fi
        ;;
    esac
    thq_step "$tool" "$tool" "$tool.json" "$tool" fire "${common[@]}" --send \
      ${READBACK_ARGS[@]+"${READBACK_ARGS[@]}"} ${route[@]+"${route[@]}"} \
      --out "$out/$tool.json" ${EXTRA[@]+"${EXTRA[@]}"}
  done

  extra_args SWEEP_ARGS_BLAST
  thq_step blast-catalog blast blast-catalog.json blast fire "${common[@]}" --send --catalog \
    --out "$out/blast-catalog.json" ${EXTRA[@]+"${EXTRA[@]}"}

  if [ "${SWEEP_CORPUS:-0}" = 1 ]; then
    local corpus=${SWEEP_CORPUS_DIR:-}
    if [ -z "$corpus" ]; then
      local ref=${SWEEP_ZAMMAD_REF:-$ZAMMAD_REF_DEFAULT}
      run_step corpus-fetch git "" \
        "$(show_cmd git clone --depth 1 --branch "$ref" --filter=blob:none --sparse "$ZAMMAD_REPO" "$CACHE_DIR/zammad")" \
        fetch_corpus "$CACHE_DIR/zammad" "$ref"
      corpus="$CACHE_DIR/zammad/test/data/mail"
    else
      LAST_CODE=0
    fi
    if [ "$LAST_CODE" = 0 ]; then
      thq_step blast-corpus blast blast-corpus.json blast fire "${common[@]}" --send \
        --corpus "$corpus" --count "$CORPUS_COUNT" --out "$out/blast-corpus.json" \
        ${EXTRA[@]+"${EXTRA[@]}"}
    else
      skip_step blast-corpus blast "blocked: corpus-fetch failed"
    fi
  else
    skip_step blast-corpus blast "SWEEP_CORPUS=${SWEEP_CORPUS:-0}"
  fi

  if [ "${SWEEP_BARRAGE:-0}" = 1 ]; then
    extra_args SWEEP_ARGS_BARRAGE
    thq_step barrage barrage barrage.json barrage fire "${common[@]}" --send \
      --rate "$BARRAGE_RATE" --duration "$BARRAGE_DURATION" --out "$out/barrage.json" \
      ${EXTRA[@]+"${EXTRA[@]}"}
  else
    skip_step barrage barrage "SWEEP_BARRAGE=${SWEEP_BARRAGE:-0}"
  fi

  "$PYTHON" "$HARNESS" relativize "$out" || true
  for name in $ARTIFACT_STEPS; do
    if [ ! -f "$out/$name.json" ]; then
      continue
    fi
    local display code
    display=$(show_cmd testinghq report "$out/$name.json" --html "$out/$name.html")
    printf '\n== report %s\n$ %s\n' "$name" "$display"
    set +e
    "${THQ[@]}" report "$out/$name.json" --html "$out/$name.html" >"$out/$name.txt" 2>&1
    code=$?
    set -e
    record report "$name" report "$code" "" "$name.html" "$name.txt" "" "$display"
  done
}

# finish_run CONFIG TARGET: summary.json, and the run's exit status.
finish_run() {
  local config=$1 target=$2 version name code
  version=$("${THQ[@]}" --version 2>/dev/null || true)
  version=${version#testinghq }
  local settings=()
  for name in $SETTINGS; do
    if [ -n "${!name+set}" ]; then
      settings+=(--setting "$name=${!name}")
    fi
  done
  set +e
  "$PYTHON" "$HARNESS" summary "$STEPS" --out "$RUN_DIR/summary.json" \
    --helpdesk "$RUN_NAME" --started "$STARTED" --finished "$(utc_now)" \
    --version "$version" --config "$config" --target "$target" \
    --note "$(printf '%s' "$ORDER_NOTE" | tr '\n' ' ')" ${settings[@]+"${settings[@]}"}
  code=$?
  set -e
  printf 'sweep: summary in %s/summary.json\n' "$RUN_DIR"
  return "$code"
}

# skip_tools REASON: every tool step, recorded as blocked.
skip_tools() {
  local reason=$1 name
  for name in config-validate verify ledger redeliver loop steady blast-catalog \
    blast-corpus barrage; do
    skip_step "$name" "${name%%-*}" "blocked: $reason"
  done
}

run_helpdesk() {
  local h=$1
  local compose="sweep/compose/$h.yml" setup="sweep/setup/$h.sh"
  local config="sweep/configs/$h.toml" profile="sweep/profiles/$h.env"
  local file
  for file in "$compose" "$setup" "$config" "$profile"; do
    if [ ! -f "$file" ]; then
      die "$h: $file is missing"
    fi
  done
  require_helpdesk_tools "$h"
  load_env_file "$profile" keep
  start_run "$h"

  run_step compose-up docker "" "$(show_cmd docker compose -f "$compose" up -d)" \
    docker compose -f "$compose" up -d
  if [ "$LAST_CODE" != 0 ]; then
    skip_step setup bash "blocked: compose-up failed"
    skip_tools "compose-up failed"
    finish_run "$config" "$h"
    return
  fi
  run_step setup bash "" "$(show_cmd bash "$setup")" bash "$setup"
  if [ "$LAST_CODE" != 0 ]; then
    skip_tools "setup failed"
    finish_run "$config" "$h"
    return
  fi
  load_env_file "sweep/.env.$h" override
  run_tools "$config" "$h"
  finish_run "$config" "$h"
}

run_all() {
  local h code failed=""
  for h in $HELPDESKS; do
    set +e
    (
      set -e
      run_helpdesk "$h"
    )
    code=$?
    set -e
    if [ "$code" != 0 ]; then
      failed="$failed $h"
    fi
    if [ "${SWEEP_KEEP:-0}" != 1 ] && [ -f "sweep/compose/$h.yml" ]; then
      docker compose -f "sweep/compose/$h.yml" down || true
    fi
  done
  if [ -n "$failed" ]; then
    printf 'sweep: errors in:%s\n' "$failed" >&2
    return 1
  fi
  printf 'sweep: every helpdesk run completed\n'
}

# --- mailpit ---------------------------------------------------------------

mailpit_asset() {
  local os arch ext=tar.gz
  case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    MINGW* | MSYS* | CYGWIN*) os=windows ext=zip ;;
    *) return 1 ;;
  esac
  case "$(uname -m)" in
    x86_64 | amd64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) return 1 ;;
  esac
  printf 'mailpit-%s-%s.%s' "$os" "$arch" "$ext"
}

# Mailpit's releases publish no checksum file, so these are pinned here: the
# SHA-256 of each v1.31.6 release asset. A download that does not match is
# deleted and the run stops.
mailpit_sha256() {
  case $1 in
    mailpit-linux-amd64.tar.gz) echo 10fa12ccc610f653d4ed4df848c1817f9935dcf4e9a6a458a19ab0fa02093f91 ;;
    mailpit-linux-arm64.tar.gz) echo de07955b783236d9129da76da9a5970f1777834a18b2c07611de957be784dafe ;;
    mailpit-darwin-amd64.tar.gz) echo 2f89ed77d2e6623bf700de42236858d10dde1ba96a222c9dbdefd33fa094a6f3 ;;
    mailpit-darwin-arm64.tar.gz) echo 1411270aa8fd7f4fb0cc358318754d7bb7c2c30bfd70572dd12d05a53e956373 ;;
    mailpit-windows-amd64.zip) echo 5d5ad094bfb3bac9004f9d1105d2335fea9f8952c09951df73ae0a280e34d3b1 ;;
    mailpit-windows-arm64.zip) echo c04c77ac9fd9da27c91259d0484b93b163e8534964682f543204cf3301a6b967 ;;
    *) return 1 ;;
  esac
}

download() {
  local url=$1 dest=$2
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$dest" "$url"
  else
    "$PYTHON" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$dest"
  fi
}

# fetch_mailpit DIR ASSET: the pinned binary, downloaded once, checksum
# verified on every run.
fetch_mailpit() {
  local dir=$1 asset=$2 expected actual
  expected=$(mailpit_sha256 "$asset")
  mkdir -p "$dir"
  if [ ! -f "$dir/$asset" ]; then
    download "$MAILPIT_RELEASES/$MAILPIT_VERSION/$asset" "$dir/$asset.part"
    mv "$dir/$asset.part" "$dir/$asset"
  fi
  actual=$("$PYTHON" "$HARNESS" sha256 "$dir/$asset")
  if [ "$actual" != "$expected" ]; then
    rm -f "$dir/$asset"
    printf 'sha256 mismatch for %s: expected %s, got %s\n' "$asset" "$expected" "$actual" >&2
    return 1
  fi
  printf 'sha256 %s  %s\n' "$actual" "$asset"
  if [ ! -f "$dir/bin/mailpit" ] && [ ! -f "$dir/bin/mailpit.exe" ]; then
    "$PYTHON" "$HARNESS" extract "$dir/$asset" "$dir/bin"
  fi
}

BACKGROUND_PIDS=""

stop_background() {
  local pid
  for pid in $BACKGROUND_PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in $BACKGROUND_PIDS; do
    wait "$pid" 2>/dev/null || true
  done
  BACKGROUND_PIDS=""
}

# Before every tool: an empty Mailpit and a fresh pipeline, so each tool's
# listing for stray records starts from its own mail only.
reset_mailpit_run() {
  "$PYTHON" "$HARNESS" http DELETE "http://127.0.0.1:$MAILPIT_HTTP_PORT/api/v1/messages" >/dev/null ||
    printf 'sweep: could not clear Mailpit\n' >&2
  "$PYTHON" "$HARNESS" http DELETE "http://127.0.0.1:$PIPELINE_PORT/tickets" >/dev/null ||
    printf 'sweep: could not reset the pipeline\n' >&2
}

# The recipe as published, plus the three tables the demo pipeline needs:
# its reply address and acknowledgement sink for loop, and the readback field
# its classifier's label lands in for steady.
write_mailpit_config() {
  local dest=$1
  {
    cat examples/readback/mailpit.toml
    cat <<'EOF'

# ---------------------------------------------------------------------------
# Added by sweep/run.sh for the demo pipeline (sweep/mailpit_pipeline.py)
# ---------------------------------------------------------------------------
#
# The pipeline forwards each ticket to <category>@team.example.com, so the
# recipe's route (To.0.Address) carries its classifier's label. Its
# acknowledgements go to a JSON Lines sink beside this file.

[loop]
reply_address = "support@example.com"

[loop.outbound]
kind = "mailbox"
path = "outbound.jsonl"

[steady]
label_field = "route"
EOF
  } >"$dest"
}

run_mailpit() {
  local asset dir bin config
  load_env_file sweep/profiles/mailpit.env keep
  start_run mailpit
  config="$RUN_DIR/config.toml"
  if ! asset=$(mailpit_asset) || ! mailpit_sha256 "$asset" >/dev/null; then
    die "no pinned Mailpit $MAILPIT_VERSION build for $(uname -s) $(uname -m)"
  fi
  dir="$CACHE_DIR/mailpit-$MAILPIT_VERSION"
  run_step mailpit-fetch harness "" \
    "$(show_cmd curl -fsSL -o "$dir/$asset" "$MAILPIT_RELEASES/$MAILPIT_VERSION/$asset")" \
    fetch_mailpit "$dir" "$asset"
  if [ "$LAST_CODE" != 0 ]; then
    skip_tools "mailpit-fetch failed"
    finish_run "$config" pipeline
    return
  fi
  bin="$dir/bin/mailpit"
  if [ -f "$bin.exe" ]; then
    bin="$bin.exe"
  fi

  trap stop_background EXIT
  "$bin" --listen "127.0.0.1:$MAILPIT_HTTP_PORT" --smtp "127.0.0.1:$MAILPIT_SMTP_PORT" \
    --max 10000 --disable-version-check --smtp-disable-rdns >"$RUN_DIR/mailpit.log" 2>&1 &
  BACKGROUND_PIDS="$BACKGROUND_PIDS $!"
  "$PYTHON" sweep/mailpit_pipeline.py --port "$PIPELINE_PORT" \
    --smtp-port "$MAILPIT_SMTP_PORT" --outbound "$RUN_DIR/outbound.jsonl" \
    >"$RUN_DIR/pipeline.log" 2>&1 &
  BACKGROUND_PIDS="$BACKGROUND_PIDS $!"
  run_step mailpit-start harness "" \
    "$(show_cmd "$bin" --listen "127.0.0.1:$MAILPIT_HTTP_PORT" --smtp "127.0.0.1:$MAILPIT_SMTP_PORT" --max 10000)" \
    "$PYTHON" "$HARNESS" wait-port 127.0.0.1 "$MAILPIT_HTTP_PORT" --timeout 30
  local mailpit_up=$LAST_CODE
  run_step pipeline-start harness "" \
    "$(show_cmd python sweep/mailpit_pipeline.py --port "$PIPELINE_PORT" --smtp-port "$MAILPIT_SMTP_PORT" --outbound "$RUN_DIR/outbound.jsonl")" \
    "$PYTHON" "$HARNESS" wait-port 127.0.0.1 "$PIPELINE_PORT" --timeout 30
  if [ "$mailpit_up" != 0 ] || [ "$LAST_CODE" != 0 ]; then
    stop_background
    skip_tools "mailpit or the pipeline did not start"
    finish_run "$config" pipeline
    return
  fi

  write_mailpit_config "$config"
  BEFORE_STEP=reset_mailpit_run
  run_tools "$config" pipeline
  BEFORE_STEP=""
  stop_background
  trap - EXIT
  finish_run "$config" pipeline
}

main() {
  if [ "$#" -ne 1 ]; then
    usage
  fi
  find_python
  set_pythonpath
  find_cli
  case $1 in
    all) run_all ;;
    mailpit) run_mailpit ;;
    -h | --help) usage ;;
    *)
      case " $HELPDESKS " in
        *" $1 "*) run_helpdesk "$1" ;;
        *) usage ;;
      esac
      ;;
  esac
}

main "$@"
