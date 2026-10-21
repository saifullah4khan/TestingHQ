#!/usr/bin/env bash
# Set up the Chatwoot in sweep/compose/chatwoot.yml for a TestingHQ sweep.
#
#   docker compose -f sweep/compose/chatwoot.yml up -d
#   bash sweep/setup/chatwoot.sh
#
# Run from the repository root. Starts nothing itself, and is safe to run again:
# every step checks what is already there first.
#
#   1. Waits for Chatwoot to answer (SWEEP_HEALTH_URL in
#      sweep/profiles/chatwoot.env) with its database and Redis up.
#   2. With a rails runner in the rails container: the account (id 1), the
#      admin user as its administrator, and the admin's API access token, and
#      ends the installation onboarding so the dashboard opens on login.
#   3. Through Chatwoot's API, with that token: the email inbox
#      support@chatwoot.test, the `testinghq` label, and an automation rule
#      that adds the label to every conversation the inbox opens.
#   4. Pins the inbox's forwarding address to desk@inbound.chatwoot.test, the
#      envelope_to of sweep/configs/chatwoot.toml (Chatwoot generates a random
#      one and its API cannot set it), with a second rails runner.
#   5. Writes CHATWOOT_INGRESS_AUTH, CHATWOOT_API_TOKEN, CHATWOOT_ADMIN_EMAIL,
#      CHATWOOT_ADMIN_PASSWORD and CHATWOOT_INBOX_ID to sweep/.env.chatwoot,
#      leaving every other line of it alone. CHATWOOT_INGRESS_AUTH holds a
#      space, so it is written in double quotes, as `testinghq config
#      basic-auth` prints it, and `set -a; . sweep/.env.chatwoot` reads it as
#      one value; run.sh's loader strips the quotes. The rest have no space
#      and are written as they are.
#
# CHATWOOT_INGRESS_AUTH is "Basic <base64 of actionmailbox:password>" for the
# RAILS_INBOUND_EMAIL_PASSWORD the rails container runs with, the value
# `testinghq config basic-auth actionmailbox` prints for that password.
#
# Needs bash, curl, docker compose and Python 3 (PYTHON overrides which one).
set -euo pipefail

COMPOSE_FILE="sweep/compose/chatwoot.yml"
PROFILE="sweep/profiles/chatwoot.env"
ENV_FILE="sweep/.env.chatwoot"
# CHATWOOT_URL points the script at another instance; the health check then
# follows it instead of the profile's SWEEP_HEALTH_URL.
CHATWOOT_URL_GIVEN="${CHATWOOT_URL:-}"
CHATWOOT_URL="${CHATWOOT_URL:-http://127.0.0.1:3100}"
ACCOUNT_ID=1
ADMIN_EMAIL="admin@chatwoot.test"
INBOX_NAME="TestingHQ Sweep"
INBOX_EMAIL="support@chatwoot.test"
# Must match envelope_to in sweep/configs/chatwoot.toml, and sit on
# MAILER_INBOUND_EMAIL_DOMAIN in the compose file.
FORWARD_TO="desk@inbound.chatwoot.test"
LABEL="testinghq"
RULE_NAME="TestingHQ label"
TIMEOUT="${CHATWOOT_SETUP_TIMEOUT:-900}"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "setup/chatwoot: run this from the repository root ($COMPOSE_FILE not found)" >&2
  exit 1
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi

log() { echo "setup/chatwoot: $*" >&2; }
die() { log "$*"; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# The value of KEY in a KEY=VALUE file, read literally (never sourced).
read_key() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    if [ "${line%%=*}" = "$key" ]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done < "$file"
}

# Set KEY=VALUE lines in a file, replacing those keys and keeping every other line.
write_keys() {
  local file="$1"; shift
  local tmp="$file.tmp.$$" line key pair keep
  : > "$tmp"
  if [ -f "$file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      key="${line%%=*}"
      keep=1
      for pair in "$@"; do
        if [ "$key" = "${pair%%=*}" ]; then keep=0; fi
      done
      if [ "$keep" = 1 ]; then printf '%s\n' "$line" >> "$tmp"; fi
    done < "$file"
  fi
  for pair in "$@"; do printf '%s\n' "$pair" >> "$tmp"; done
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

# Print the HTTP status of a request; the body goes to the file in $BODY.
BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT
status_of() {
  local code
  : > "$BODY"
  code="$(curl -s -o "$BODY" -w '%{http_code}' --max-time 30 "$@")" || true
  printf '%s' "${code:-000}"
}

# Read one value out of the JSON in $BODY with a Python expression over `d`.
json_get() {
  "$PYTHON" -c 'import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    value = eval(sys.argv[2], {"d": d, "arg": sys.argv[3:]})
except Exception:
    value = None
print("" if value is None else (json.dumps(value) if isinstance(value, (dict, list, bool)) else value))
' "$BODY" "$@"
}

# An account API call with the admin's token: METHOD PATH [JSON]. Prints the
# status; the body is in $BODY.
api() {
  local method="$1" path="$2" data="${3:-}"
  local args=(-X "$method" -H "api_access_token: $TOKEN" -H "Content-Type: application/json")
  if [ -n "$data" ]; then args+=(--data "$data"); fi
  status_of "${args[@]}" "$CHATWOOT_URL/api/v1/accounts/$ACCOUNT_ID$path"
}

# Run Ruby in the rails container; prints only the THQ_ lines it writes, since
# Rails logs to stdout too, and keeps the whole output in $RUNNER_LOG for the
# error message. Extra arguments are `-e NAME=VALUE` for the script.
RUNNER_LOG="$(mktemp)"
trap 'rm -f "$BODY" "$RUNNER_LOG"' EXIT
rails_runner() {
  local script="$1"; shift
  compose exec -T "$@" rails bundle exec rails runner "$script" > "$RUNNER_LOG" 2>&1 || true
  grep '^THQ_' "$RUNNER_LOG" || true
}
runner_failed() {
  die "$1; the rails runner said: $(tail -n 15 "$RUNNER_LOG")"
}

# 1. Wait for Chatwoot ------------------------------------------------------

HEALTH_URL=""
if [ -z "$CHATWOOT_URL_GIVEN" ]; then HEALTH_URL="$(read_key "$PROFILE" SWEEP_HEALTH_URL)"; fi
HEALTH_URL="${HEALTH_URL:-$CHATWOOT_URL/api}"
log "waiting for $HEALTH_URL (up to ${TIMEOUT}s; the first boot loads the schema)"
deadline=$((SECONDS + TIMEOUT))
until [ "$(status_of "$HEALTH_URL")" = "200" ] \
    && [ "$(json_get 'd.get("data_services") == "ok" and d.get("queue_services") == "ok"')" = "true" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    die "Chatwoot did not answer $HEALTH_URL within ${TIMEOUT}s; see: docker compose -f $COMPOSE_FILE logs prepare rails"
  fi
  sleep 5
done
log "Chatwoot $(json_get 'd.get("version")') is up"

INBOUND_PASSWORD="$(compose exec -T rails printenv RAILS_INBOUND_EMAIL_PASSWORD | tr -d '\r\n')"
[ -n "$INBOUND_PASSWORD" ] || die "the rails container has no RAILS_INBOUND_EMAIL_PASSWORD"
INGRESS_AUTH="Basic $(printf '%s' "actionmailbox:$INBOUND_PASSWORD" | base64 | tr -d '\r\n')"

# 2. Account, admin, access token ------------------------------------------

ADMIN_PASSWORD="$(read_key "$ENV_FILE" CHATWOOT_ADMIN_PASSWORD)"
if [ -z "$ADMIN_PASSWORD" ]; then
  # Chatwoot wants upper and lower case, a digit and a special character.
  ADMIN_PASSWORD="Thq-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')-Sw9"
fi

# shellcheck disable=SC2016  # Ruby, not shell: the #{} is Ruby's.
ADMIN_RUBY='
email = ENV.fetch("THQ_ADMIN_EMAIL")
password = ENV.fetch("THQ_ADMIN_PASSWORD")
account = Account.find_by(id: 1) || Account.create!(name: "TestingHQ Sweep", locale: "en")
user = User.find_by(email: email) || User.new(email: email, name: "TestingHQ Sweep")
unless user.persisted? && user.valid_password?(password)
  user.password = password
  user.password_confirmation = password
end
user.type = "SuperAdmin"
user.skip_confirmation! unless user.confirmed?
user.save! if user.changed? || !user.persisted?
AccountUser.find_or_create_by!(account_id: account.id, user_id: user.id) do |member|
  member.role = :administrator
end
token = user.access_token || user.create_access_token
Redis::Alfred.delete(Redis::Alfred::CHATWOOT_INSTALLATION_ONBOARDING)
puts "THQ_ACCOUNT_ID=#{account.id}"
puts "THQ_API_TOKEN=#{token.token}"
'
output="$(rails_runner "$ADMIN_RUBY" -e "THQ_ADMIN_EMAIL=$ADMIN_EMAIL" -e "THQ_ADMIN_PASSWORD=$ADMIN_PASSWORD")"
account="$(printf '%s\n' "$output" | sed -n 's/^THQ_ACCOUNT_ID=//p' | tail -n 1)"
TOKEN="$(printf '%s\n' "$output" | sed -n 's/^THQ_API_TOKEN=//p' | tail -n 1)"
[ -n "$TOKEN" ] || runner_failed "no access token was reported"
if [ "$account" != "$ACCOUNT_ID" ]; then
  die "the sweep account is id ${account:-unknown}, and the config reads account $ACCOUNT_ID; start from a fresh stack: docker compose -f $COMPOSE_FILE down -v"
fi
log "account $ACCOUNT_ID, admin $ADMIN_EMAIL, access token ready"

code="$(api GET /inboxes)"
[ "$code" = "200" ] || die "the access token cannot list inboxes (HTTP $code: $(head -c 300 "$BODY"))"

# 3. Inbox, label, automation rule ------------------------------------------

INBOX_ID="$(json_get 'next((i["id"] for i in d["payload"] if i.get("email") == arg[0]), None)' "$INBOX_EMAIL")"
if [ -z "$INBOX_ID" ]; then
  code="$(api POST /inboxes "{\"name\": \"$INBOX_NAME\", \"channel\": {\"type\": \"email\", \"email\": \"$INBOX_EMAIL\"}}")"
  INBOX_ID="$(json_get 'd.get("id")')"
  if [ "$code" != "200" ] || [ -z "$INBOX_ID" ]; then
    die "could not create the email inbox (HTTP $code: $(head -c 300 "$BODY"))"
  fi
  log "created the email inbox $INBOX_EMAIL (id $INBOX_ID)"
else
  log "the email inbox $INBOX_EMAIL is there (id $INBOX_ID)"
fi

code="$(api GET /labels)"
[ "$code" = "200" ] || die "could not list labels (HTTP $code)"
if [ -z "$(json_get 'next((l["id"] for l in d["payload"] if l.get("title") == arg[0]), None)' "$LABEL")" ]; then
  code="$(api POST /labels "{\"label\": {\"title\": \"$LABEL\", \"description\": \"Mail sent by TestingHQ\", \"color\": \"#1f93ff\", \"show_on_sidebar\": true}}")"
  [ "$code" = "200" ] || die "could not create the $LABEL label (HTTP $code: $(head -c 300 "$BODY"))"
  log "created the $LABEL label"
fi

RULE="{\"name\": \"$RULE_NAME\", \"description\": \"Labels every conversation the TestingHQ sweep inbox opens\", \"event_name\": \"conversation_created\", \"active\": true, \"conditions\": [{\"attribute_key\": \"inbox_id\", \"filter_operator\": \"equal_to\", \"values\": [$INBOX_ID], \"query_operator\": null}], \"actions\": [{\"action_name\": \"add_label\", \"action_params\": [\"$LABEL\"]}]}"
code="$(api GET /automation_rules)"
[ "$code" = "200" ] || die "could not list automation rules (HTTP $code)"
RULE_ID="$(json_get 'next((r["id"] for r in d["payload"] if r.get("name") == arg[0]), None)' "$RULE_NAME")"
if [ -z "$RULE_ID" ]; then
  code="$(api POST /automation_rules "$RULE")"
  [ "$code" = "200" ] || die "could not create the automation rule (HTTP $code: $(head -c 300 "$BODY"))"
  log "created the automation rule '$RULE_NAME'"
else
  code="$(api PATCH "/automation_rules/$RULE_ID" "$RULE")"
  [ "$code" = "200" ] || die "could not update the automation rule (HTTP $code: $(head -c 300 "$BODY"))"
  log "the automation rule '$RULE_NAME' is in place"
fi

# 4. The forwarding address -------------------------------------------------

# shellcheck disable=SC2016  # Ruby, not shell.
FORWARD_RUBY='
channel = Channel::Email.find_by!(email: ENV.fetch("THQ_INBOX_EMAIL"))
wanted = ENV.fetch("THQ_FORWARD_TO")
channel.update_columns(forward_to_email: wanted) unless channel.forward_to_email == wanted
puts "THQ_FORWARD_TO=#{channel.reload.forward_to_email}"
'
output="$(rails_runner "$FORWARD_RUBY" -e "THQ_INBOX_EMAIL=$INBOX_EMAIL" -e "THQ_FORWARD_TO=$FORWARD_TO")"
forward="$(printf '%s\n' "$output" | sed -n 's/^THQ_FORWARD_TO=//p' | tail -n 1)"
[ "$forward" = "$FORWARD_TO" ] || runner_failed "the inbox forwarding address is '${forward:-unknown}', not $FORWARD_TO"
log "the inbox takes mail for $FORWARD_TO"

# The ingress has to take the credential: a POST with no message is refused
# for the missing field once the password matched, and is a 401 when it did
# not. A 404 means Action Mailbox is not on the sendgrid ingress.
code="$(status_of -X POST -H "Authorization: $INGRESS_AUTH" \
  "$CHATWOOT_URL/rails/action_mailbox/sendgrid/inbound_emails")"
[ "$code" != "401" ] || die "the Action Mailbox ingress refused the Basic auth credential"
[ "$code" != "404" ] || die "the Action Mailbox ingress is not on sendgrid (HTTP 404); check RAILS_INBOUND_EMAIL_SERVICE"

# 5. sweep/.env.chatwoot ----------------------------------------------------

write_keys "$ENV_FILE" \
  "CHATWOOT_INGRESS_AUTH=\"$INGRESS_AUTH\"" \
  "CHATWOOT_API_TOKEN=$TOKEN" \
  "CHATWOOT_ADMIN_EMAIL=$ADMIN_EMAIL" \
  "CHATWOOT_ADMIN_PASSWORD=$ADMIN_PASSWORD" \
  "CHATWOOT_INBOX_ID=$INBOX_ID"
log "wrote CHATWOOT_INGRESS_AUTH, CHATWOOT_API_TOKEN, CHATWOOT_ADMIN_EMAIL, CHATWOOT_ADMIN_PASSWORD and CHATWOOT_INBOX_ID to $ENV_FILE"

log "the readback also needs CHATWOOT_API_TOKEN=$TOKEN, which is in $ENV_FILE"

echo "URL: $CHATWOOT_URL"
echo "Login: $ADMIN_EMAIL / $ADMIN_PASSWORD"
echo "Add to .env: CHATWOOT_INGRESS_AUTH=\"$INGRESS_AUTH\""
