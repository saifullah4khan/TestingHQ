#!/usr/bin/env bash
# Set up Frappe Helpdesk for the sweep, after
#   docker compose -f sweep/compose/frappe.yml up -d
#
# Run from the repository root: bash sweep/setup/frappe.sh
#
# What it does, every step safe to repeat:
#   1. waits for the create-site container to finish (first boot runs
#      `bench new-site` and installs telephony and helpdesk: several minutes),
#      then for /api/method/ping to answer
#   2. enables the scheduler, which pulls mail
#   3. creates an API key and secret for Administrator, unless the one already
#      in sweep/.env.frappe still works
#   4. creates or updates the Email Account "Sweep Support": POP3 from the
#      Mailpit sidecar, append_to = HD Ticket, so every pulled message opens a
#      ticket (or joins the one it replies to)
#   5. makes the mail pull run every minute instead of every ten
#   6. writes FRAPPE_API_AUTH to sweep/.env.frappe
#
# Needs docker, curl and python3 (or python) on the PATH.
set -euo pipefail

COMPOSE_FILE="sweep/compose/frappe.yml"
SITE="helpdesk.localhost"
BASE_URL="${FRAPPE_URL:-http://127.0.0.1:3500}"
ENV_FILE="sweep/.env.frappe"
TIMEOUT="${FRAPPE_SETUP_TIMEOUT:-1800}"

# The Email Account. The POP3 pair must match MP_POP3_AUTH in the compose file.
ACCOUNT_NAME="Sweep Support"
ACCOUNT_PATH="Sweep%20Support"
ACCOUNT_EMAIL="support@frappe.test"
POP3_HOST="mailpit"
POP3_PORT="1110"
POP3_USER="helpdesk"
POP3_PASSWORD="helpdesk-pop3"

# Frappe names a Scheduled Job Type after the last two parts of its method.
PULL_JOB="email_account.pull"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "run this from the repository root: $COMPOSE_FILE not found" >&2
  exit 1
fi

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
bench_site() { compose exec -T backend bench --site "$SITE" "$@"; }

deadline=$(( $(date +%s) + TIMEOUT ))
past_deadline() { (( $(date +%s) > deadline )); }

say() { echo "frappe setup: $*" >&2; }

# -- 1. wait for the site ---------------------------------------------------

say "waiting for create-site to finish (first boot takes several minutes)"
while :; do
  cid="$(compose ps -a -q create-site 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    state="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$cid")"
    if [[ "$state" == "exited 0" ]]; then
      break
    fi
    if [[ "$state" == exited* ]]; then
      say "create-site failed ($state); see: docker compose -f $COMPOSE_FILE logs create-site"
      exit 1
    fi
  fi
  if past_deadline; then
    say "create-site did not finish within ${TIMEOUT}s"
    exit 1
  fi
  sleep 10
done

say "waiting for $BASE_URL/api/method/ping"
until curl -fsS --max-time 10 "$BASE_URL/api/method/ping" 2>/dev/null | grep -q pong; do
  if past_deadline; then
    say "$BASE_URL/api/method/ping did not answer within ${TIMEOUT}s"
    exit 1
  fi
  sleep 5
done

# -- 2. the scheduler -------------------------------------------------------

bench_site enable-scheduler >&2

# -- 3. the API key ---------------------------------------------------------

# One request against the API. The status goes in $STATUS and the body in
# $RESPONSE, set in this shell (so never call it inside $(...)).
STATUS=""
RESPONSE=""
api() {
  local method="$1" path="$2" data="${3:-}" body
  body="$(mktemp)"
  local args=(-sS --max-time 30 -o "$body" -w '%{http_code}' -X "$method"
    -H "Authorization: $AUTH" -H "Accept: application/json")
  if [[ -n "$data" ]]; then
    args+=(-H "Content-Type: application/json" --data-binary "$data")
  fi
  STATUS="$(curl "${args[@]}" "$BASE_URL$path" || echo 000)"
  RESPONSE="$(cat "$body")"
  rm -f "$body"
}

existing_auth() {
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n 's/^FRAPPE_API_AUTH=//p' "$ENV_FILE" | tail -n 1
}

AUTH="$(existing_auth)"
if [[ -n "$AUTH" ]]; then
  api GET "/api/method/frappe.auth.get_logged_user"
  if [[ "$STATUS" != "200" ]]; then
    say "the API key in $ENV_FILE is refused ($STATUS); generating a new one"
    AUTH=""
  fi
fi

if [[ -z "$AUTH" ]]; then
  keys="$(bench_site execute frappe.core.doctype.user.user.generate_keys --args "['Administrator']")"
  AUTH="$(printf '%s\n' "$keys" | "$PYTHON" -c '
import json, sys
for line in reversed(sys.stdin.read().splitlines()):
    try:
        value = json.loads(line)
    except ValueError:
        continue
    if isinstance(value, dict) and value.get("api_key") and value.get("api_secret"):
        print("token %s:%s" % (value["api_key"], value["api_secret"]))
        break
else:
    sys.exit("generate_keys printed no api_key and api_secret")
')"
  api GET "/api/method/frappe.auth.get_logged_user"
  if [[ "$STATUS" != "200" ]]; then
    say "the new API key is refused ($STATUS)"
    exit 1
  fi
fi

# -- 4. the Email Account ---------------------------------------------------

account_json="$("$PYTHON" -c '
import json, sys
name, email, host, port, user, password = sys.argv[1:7]
print(json.dumps({
    "email_account_name": name,
    "email_id": email,
    "auth_method": "Basic",
    "login_id_is_different": 1,
    "login_id": user,
    "password": password,
    "awaiting_password": 0,
    "enable_incoming": 1,
    "use_imap": 0,
    "email_server": host,
    "incoming_port": port,
    "use_ssl": 0,
    "use_starttls": 0,
    "append_to": "HD Ticket",
    "default_incoming": 1,
    "create_contact": 1,
    "enable_outgoing": 0,
    "enable_auto_reply": 0,
}))
' "$ACCOUNT_NAME" "$ACCOUNT_EMAIL" "$POP3_HOST" "$POP3_PORT" "$POP3_USER" "$POP3_PASSWORD")"

api GET "/api/resource/Email%20Account/$ACCOUNT_PATH"
if [[ "$STATUS" == "200" ]]; then
  api PUT "/api/resource/Email%20Account/$ACCOUNT_PATH" "$account_json"
else
  api POST "/api/resource/Email%20Account" "$account_json"
fi
if [[ "$STATUS" != "200" ]]; then
  say "saving the Email Account failed ($STATUS). Frappe checks the POP3 login on save, so"
  say "check that the mailpit service is up and MP_POP3_AUTH matches. Response:"
  printf '%s\n' "$RESPONSE" | head -c 2000 >&2
  echo >&2
  exit 1
fi

# -- 5. pull every minute ---------------------------------------------------

# Frappe pulls mail every ten minutes by default. `bench migrate` resets this
# from the hooks, so it is set again on every run of this script.
api PUT "/api/resource/Scheduled%20Job%20Type/$PULL_JOB" \
  '{"frequency": "Cron", "cron_format": "* * * * *", "stopped": 0}'
if [[ "$STATUS" != "200" ]]; then
  say "setting the mail pull to every minute failed ($STATUS):"
  printf '%s\n' "$RESPONSE" | head -c 2000 >&2
  echo >&2
  exit 1
fi

# -- 6. sweep/.env.frappe ---------------------------------------------------

set_env_key() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [[ -f "$ENV_FILE" ]]; then
    grep -v "^$key=" "$ENV_FILE" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

set_env_key FRAPPE_API_AUTH "$AUTH"
say "wrote FRAPPE_API_AUTH to $ENV_FILE"

echo "URL: $BASE_URL/helpdesk"
echo "Login: Administrator / admin"
echo "Add to .env: FRAPPE_API_AUTH=$AUTH"
