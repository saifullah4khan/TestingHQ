#!/usr/bin/env bash
# Set up the Zammad in sweep/compose/zammad.yml for a TestingHQ sweep.
#
#   docker compose -f sweep/compose/zammad.yml up -d
#   bash sweep/setup/zammad.sh
#
# Run from the repository root. Starts nothing itself, and is safe to run again:
# every step checks what is already there first.
#
#   1. Waits for Zammad to answer (SWEEP_HEALTH_URL in sweep/profiles/zammad.env).
#   2. On a fresh system, runs Zammad's auto wizard through its API: the wizard
#      file goes into the railsserver container and
#      GET /api/v1/getting_started/auto_wizard/<token> applies it. It creates
#      the admin, and the email channel: POP3 from zammad-mail-in for inbound,
#      SMTP to zammad-mail-out for outbound, filing into the Users group, with
#      support@zammad.test as its address. On a system that was set up some
#      other way, the admin and the channel are put in place with a rails
#      runner instead.
#   3. Creates an API token for the admin with ticket.agent, unless the one in
#      sweep/.env.zammad still works.
#   4. Writes ZAMMAD_TOKEN, ZAMMAD_AUTH and ZAMMAD_ADMIN_PASSWORD to
#      sweep/.env.zammad, leaving every other line of it alone.
#
# Needs bash, curl, docker compose and Python 3 (PYTHON overrides which one).
set -euo pipefail

COMPOSE_FILE="sweep/compose/zammad.yml"
PROFILE="sweep/profiles/zammad.env"
ENV_FILE="sweep/.env.zammad"
ZAMMAD_URL="${ZAMMAD_URL:-http://127.0.0.1:3380}"
ADMIN_LOGIN="admin@zammad.test"
SUPPORT_ADDRESS="support@zammad.test"
# Must match MP_POP3_AUTH on zammad-mail-in in the compose file.
POP3_USER="zammad"
POP3_PASSWORD="thq-sweep-pop3"
TIMEOUT="${ZAMMAD_SETUP_TIMEOUT:-900}"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "setup/zammad: run this from the repository root ($COMPOSE_FILE not found)" >&2
  exit 1
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi

log() { echo "setup/zammad: $*" >&2; }
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
    value = eval(sys.argv[2], {"d": d})
except Exception:
    value = None
print("" if value is None else (json.dumps(value) if isinstance(value, (dict, list, bool)) else value))
' "$BODY" "$1"
}

rails_runner() {
  compose exec -T "$@"
}

# 1. Health ----------------------------------------------------------------

HEALTH_URL="$(read_key "$PROFILE" SWEEP_HEALTH_URL)"
HEALTH_URL="${HEALTH_URL:-$ZAMMAD_URL/}"
log "waiting up to ${TIMEOUT}s for $HEALTH_URL (first boot runs the database setup, which takes minutes)"
deadline=$(( $(date +%s) + TIMEOUT ))
until [ "$(status_of "$HEALTH_URL")" = "200" ]; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    die "Zammad did not answer at $HEALTH_URL within ${TIMEOUT}s; see: docker compose -f $COMPOSE_FILE logs zammad-init zammad-railsserver"
  fi
  sleep 5
done

# 2. Admin and channel -----------------------------------------------------

ADMIN_PASSWORD="$(read_key "$ENV_FILE" ZAMMAD_ADMIN_PASSWORD)"
if [ -z "$ADMIN_PASSWORD" ]; then
  # Zammad's default policy: ten characters, two upper and two lower case, a digit.
  ADMIN_PASSWORD="Thq-$("$PYTHON" -c 'import secrets; print(secrets.token_hex(8))')-ZZaa9"
fi

admin_ok() {
  [ "$(status_of -u "$ADMIN_LOGIN:$ADMIN_PASSWORD" "$ZAMMAD_URL/api/v1/users/me")" = "200" ]
}

wizard_file() {
  ADMIN_LOGIN="$ADMIN_LOGIN" ADMIN_PASSWORD="$ADMIN_PASSWORD" WIZARD_TOKEN="$1" \
  SUPPORT_ADDRESS="$SUPPORT_ADDRESS" POP3_USER="$POP3_USER" POP3_PASSWORD="$POP3_PASSWORD" \
  "$PYTHON" -c 'import json, os
e = os.environ
print(json.dumps({
    "Token": e["WIZARD_TOKEN"],
    "Users": [{
        "login": e["ADMIN_LOGIN"], "email": e["ADMIN_LOGIN"],
        "firstname": "Sweep", "lastname": "Admin", "password": e["ADMIN_PASSWORD"],
    }],
    "Groups": [{"name": "Users", "users": [e["ADMIN_LOGIN"]], "email_address_id": 1}],
    "Channels": [{
        "id": 3, "area": "Email::Account", "group": "Users", "active": True,
        "options": {
            "inbound": {"adapter": "pop3", "options": {
                "host": "zammad-mail-in", "port": "1110", "ssl": "off",
                "ssl_verify": False, "user": e["POP3_USER"], "password": e["POP3_PASSWORD"],
            }},
            "outbound": {"adapter": "smtp", "options": {
                "host": "zammad-mail-out", "port": "1025", "ssl": False,
                "enable_starttls_auto": False, "ssl_verify": False,
            }},
        },
    }],
    "EmailAddresses": [{
        "id": 1, "channel_id": 3, "name": "Zammad Sweep", "email": e["SUPPORT_ADDRESS"],
    }],
    "Settings": [{"name": "product_name", "value": "Zammad (TestingHQ sweep)"}],
}, indent=2))
'
}

# The same end state for a system the wizard can no longer run on: the admin
# with this password, and the channel and address, created where missing.
RUNNER_SCRIPT='
pw = ENV.fetch("THQ_ADMIN_PASSWORD")
login = ENV.fetch("THQ_ADMIN_LOGIN")
user = User.find_by(login: login) || User.new(login: login, email: login, firstname: "Sweep", lastname: "Admin")
user.password = pw
user.roles = Role.where(name: %w[Admin Agent])
user.active = true
user.save!
group = Group.find_by(name: "Users") || Group.first
user.group_names_access_map = { group.name => ["full"] }
user.save!
channel = Channel.where(area: "Email::Account").detect { |c| c.options.dig(:inbound, :options, :host) == "zammad-mail-in" }
channel ||= Channel.create!(
  area: "Email::Account", group_id: group.id, active: true,
  options: {
    inbound:  { adapter: "pop3", options: { host: "zammad-mail-in", port: "1110", ssl: "off", ssl_verify: false, user: ENV.fetch("THQ_POP3_USER"), password: ENV.fetch("THQ_POP3_PASSWORD") } },
    outbound: { adapter: "smtp", options: { host: "zammad-mail-out", port: "1025", ssl: false, enable_starttls_auto: false, ssl_verify: false } },
  },
)
address = EmailAddress.find_by(email: ENV.fetch("THQ_SUPPORT_ADDRESS")) || EmailAddress.new(email: ENV.fetch("THQ_SUPPORT_ADDRESS"), name: "Zammad Sweep")
address.channel_id = channel.id
address.active = true
address.save!
group.update!(email_address_id: address.id) if group.email_address_id.nil?
puts "ok channel=#{channel.id}"
'

run_runner() {
  log "putting the admin and the email channel in place with a rails runner"
  rails_runner \
    -e THQ_ADMIN_LOGIN="$ADMIN_LOGIN" -e THQ_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    -e THQ_POP3_USER="$POP3_USER" -e THQ_POP3_PASSWORD="$POP3_PASSWORD" \
    -e THQ_SUPPORT_ADDRESS="$SUPPORT_ADDRESS" \
    zammad-railsserver bundle exec rails r "$RUNNER_SCRIPT" >&2
}

if admin_ok; then
  log "admin $ADMIN_LOGIN already set up"
else
  started="$(status_of "$ZAMMAD_URL/api/v1/getting_started")"
  setup_done="$(json_get 'd.get("setup_done")')"
  if [ "$started" = "200" ] && [ "$setup_done" != "true" ]; then
    log "fresh system: running the auto wizard"
    WIZARD_TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
    wizard_file "$WIZARD_TOKEN" | compose exec -T zammad-railsserver \
      sh -c 'mkdir -p /opt/zammad/tmp && cat > /opt/zammad/tmp/auto_wizard.json'
    code="$(status_of "$ZAMMAD_URL/api/v1/getting_started/auto_wizard/$WIZARD_TOKEN")"
    if [ "$code" != "200" ] || [ "$(json_get 'd.get("auto_wizard_success")')" != "true" ]; then
      log "the auto wizard failed (HTTP $code: $(head -c 300 "$BODY")); falling back"
      run_runner
    fi
  else
    log "system already set up, but $ADMIN_LOGIN cannot log in with the password in $ENV_FILE"
    run_runner
  fi
  admin_ok || die "cannot log in as $ADMIN_LOGIN after setup; see the zammad-railsserver logs"
fi

# The channel, whichever way the system was set up.
code="$(status_of -u "$ADMIN_LOGIN:$ADMIN_PASSWORD" "$ZAMMAD_URL/api/v1/channels_email")"
[ "$code" = "200" ] || die "GET /api/v1/channels_email answered $code"
has_channel="$(json_get 'any(c.get("area") == "Email::Account" and ((c.get("options") or {}).get("inbound") or {}).get("options", {}).get("host") == "zammad-mail-in" for c in d.get("assets", {}).get("Channel", {}).values())')"
if [ "$has_channel" != "true" ]; then
  log "no email channel reads zammad-mail-in yet"
  run_runner
fi

# 3. API token -------------------------------------------------------------

TOKEN="$(read_key "$ENV_FILE" ZAMMAD_TOKEN)"
if [ -n "$TOKEN" ] && [ "$(status_of -H "Authorization: Token token=$TOKEN" "$ZAMMAD_URL/api/v1/users/me")" = "200" ]; then
  log "the API token in $ENV_FILE still works"
else
  code="$(status_of -u "$ADMIN_LOGIN:$ADMIN_PASSWORD" -H "Content-Type: application/json" \
    -X POST "$ZAMMAD_URL/api/v1/user_access_token" \
    -d '{"name": "testinghq-sweep", "permission": ["ticket.agent"], "expires_at": null}')"
  TOKEN="$(json_get 'd.get("token")')"
  if [ "$code" != "200" ] || [ -z "$TOKEN" ]; then
    die "could not create an API token (HTTP $code: $(head -c 300 "$BODY"))"
  fi
  log "created an API token with ticket.agent"
fi

# The token has to be able to search; a search for a tag no run makes is empty.
code="$(status_of -H "Authorization: Token token=$TOKEN" \
  "$ZAMMAD_URL/api/v1/tickets/search?query=testinghq-preflight-probe&expand=true")"
[ "$code" = "200" ] || die "ticket search with the API token answered $code"

# 4. sweep/.env.zammad -----------------------------------------------------

write_keys "$ENV_FILE" \
  "ZAMMAD_TOKEN=$TOKEN" \
  "ZAMMAD_AUTH=Token token=$TOKEN" \
  "ZAMMAD_ADMIN_PASSWORD=$ADMIN_PASSWORD"
log "wrote ZAMMAD_TOKEN, ZAMMAD_AUTH and ZAMMAD_ADMIN_PASSWORD to $ENV_FILE"

echo "URL: $ZAMMAD_URL"
echo "Login: $ADMIN_LOGIN / $ADMIN_PASSWORD"
echo "Add to .env: ZAMMAD_AUTH=Token token=$TOKEN"
