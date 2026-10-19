#!/usr/bin/env bash
# Prepare the osTicket in sweep/compose/osticket.yml for the sweep.
#
#   docker compose -f sweep/compose/osticket.yml up -d
#   bash sweep/setup/osticket.sh
#
# Run from the repository root. Starts nothing. Safe to run again: it reuses
# the key and the reader password already in sweep/.env.osticket.
#
#   1. waits for osTicket to answer (the image installs it on first start);
#   2. finds the address osTicket sees the host's requests come from, by
#      sending one request with a key that does not exist and reading the
#      address osTicket logged for the 401 (ost_syslog.ip_address, which is
#      the same REMOTE_ADDR the key check compares). Behind Docker's port
#      publishing that is a gateway address, not 127.0.0.1, and it differs
#      between Docker Engine and Docker Desktop, so it is read, not guessed.
#      If nothing was logged, the compose network's gateway is used;
#   3. creates an API key bound to that address with "Can Create Tickets", by
#      SQL into ost_api_key, and checks a request with it is not refused;
#   4. turns on the outbound mail loop reads back: the SMTP account the
#      image seeds for the mail-out sidecar (it is created inactive, and
#      osTicket then falls back to sendmail, which has nowhere to go), and
#      the new-ticket and new-message auto-responders (off on a fresh
#      install), so osTicket answers ordinary mail and loop can see whether
#      it also answers machine mail;
#   5. creates a SELECT-only database user for the readback adapter;
#   6. writes OSTICKET_API_KEY and OSTICKET_MYSQL_DSN to sweep/.env.osticket.
set -euo pipefail

# Git Bash on Windows rewrites arguments that look like paths, and nothing
# passed to docker here is a host path, so docker runs with that turned off.
# Only docker: curl needs the rewrite, which is what turns its `-o /dev/null`
# into NUL, and without it every request fails with curl error 23.

COMPOSE_FILE="sweep/compose/osticket.yml"
PROFILE="sweep/profiles/osticket.env"
ENV_FILE="sweep/.env.osticket"
BASE_URL="http://127.0.0.1:3200"
API_URL="$BASE_URL/api/tickets.email"
DB_PORT_ON_HOST=3206
DB_NAME="osticket"
PREFIX="ost_"
READER="thq_readback"
KEY_NOTE="testinghq sweep"
NETWORK="thq-osticket_default"
TIMEOUT="${OSTICKET_SETUP_TIMEOUT:-900}"

if [ ! -f "$COMPOSE_FILE" ] || [ ! -f "$PROFILE" ]; then
  echo "osticket setup: run from the repository root (no $COMPOSE_FILE here)" >&2
  exit 1
fi

# shellcheck source=sweep/profiles/osticket.env
. "$PROFILE"

docker() {
  MSYS_NO_PATHCONV=1 command docker "$@"
}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# Run SQL as root in the database container; tab-separated, no column names.
# The root password is expanded inside the container, from its own environment.
sql() {
  # shellcheck disable=SC2016
  compose exec -T db sh -c 'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B "$0"' \
    "$DB_NAME" <<<"$1" | tr -d '\r'
}

container_env() {
  compose exec -T osticket printenv "$1" | tr -d '\r'
}

# The value of KEY in sweep/.env.osticket, or nothing.
env_value() {
  if [ -f "$ENV_FILE" ]; then
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
  fi
}

random_hex() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

deadline=$((SECONDS + TIMEOUT))
wait_for() {
  local what="$1"
  shift
  until "$@" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "osticket setup: gave up after ${TIMEOUT}s waiting for $what." >&2
      echo "  docker compose -f $COMPOSE_FILE logs osticket" >&2
      exit 1
    fi
    sleep 5
  done
}

post_status() {
  curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "X-API-Key: $1" -H 'Content-Type: message/rfc822' \
    --data-binary '' "$API_URL"
}

# 1. osTicket installed and answering.
echo "osticket setup: waiting for $SWEEP_HEALTH_URL"
wait_for "osTicket to answer at $SWEEP_HEALTH_URL" curl -fsS -o /dev/null "$SWEEP_HEALTH_URL"
wait_for "the osTicket tables" sql "SELECT COUNT(*) FROM ${PREFIX}config"

# 2. The address osTicket sees.
probe="THQPROBE$(random_hex 8)"
status="$(post_status "$probe")"
if [ "$status" != "401" ]; then
  echo "osticket setup: a request with an unknown API key got $status, expected 401" >&2
  exit 1
fi
client_ip="$(sql "SELECT ip_address FROM ${PREFIX}syslog WHERE title LIKE 'API Error (401)%' ORDER BY log_id DESC LIMIT 1")"
if [ -z "$client_ip" ]; then
  client_ip="$(docker network inspect "$NETWORK" --format '{{(index .IPAM.Config 0).Gateway}}' | tr -d '\r')"
  echo "osticket setup: no 401 in ost_syslog; binding the key to the network gateway $client_ip"
fi
if ! [[ "$client_ip" =~ ^[0-9A-Fa-f:.]+$ ]]; then
  echo "osticket setup: could not work out the client address (got '$client_ip')" >&2
  exit 1
fi

# 3. The API key.
api_key="$(env_value OSTICKET_API_KEY)"
if ! [[ "$api_key" =~ ^[0-9A-F]{32}$ ]]; then
  api_key="$(random_hex 16 | tr 'a-f' 'A-F')"
fi
sql "DELETE FROM ${PREFIX}api_key WHERE notes = '$KEY_NOTE' AND apikey <> '$api_key';
INSERT INTO ${PREFIX}api_key
  (isactive, ipaddr, apikey, can_create_tickets, can_exec_cron, notes, updated, created)
VALUES (1, '$client_ip', '$api_key', 1, 0, '$KEY_NOTE', NOW(), NOW())
ON DUPLICATE KEY UPDATE isactive = 1, ipaddr = VALUES(ipaddr),
  can_create_tickets = 1, notes = VALUES(notes), updated = NOW();" >/dev/null
# An empty body with a good key is a 400 (no message to read), never a 401.
status="$(post_status "$api_key")"
if [ "$status" = "401" ]; then
  echo "osticket setup: osTicket refused the new key from $client_ip (401)." >&2
  echo "  Check ost_syslog for the address it logged and rerun." >&2
  exit 1
fi

# 4. Outbound mail and the auto-responders.
sql "UPDATE ${PREFIX}email_account SET active = 1, updated = NOW()
  WHERE type = 'smtp' AND email_id = (SELECT value FROM ${PREFIX}config
    WHERE namespace = 'core' AND \`key\` = 'default_email_id');
UPDATE ${PREFIX}config SET value = '1', updated = NOW()
  WHERE namespace = 'core'
    AND \`key\` IN ('ticket_autoresponder', 'message_autoresponder');" >/dev/null
smtp_on="$(sql "SELECT COUNT(*) FROM ${PREFIX}email_account WHERE type = 'smtp' AND active = 1")"
if [ "$smtp_on" = "0" ]; then
  echo "osticket setup: no active SMTP account for the default email; osTicket's replies will not reach mail-out" >&2
  exit 1
fi

# 5. The readback's database user.
dsn="$(env_value OSTICKET_MYSQL_DSN)"
reader_pw="$(printf '%s' "$dsn" | sed -n "s#^mysql://$READER:\([0-9a-f]\{32\}\)@.*#\1#p")"
if [ -z "$reader_pw" ]; then
  reader_pw="$(random_hex 16)"
fi
sql "CREATE USER IF NOT EXISTS '$READER'@'%' IDENTIFIED BY '$reader_pw';
ALTER USER '$READER'@'%' IDENTIFIED BY '$reader_pw';
GRANT SELECT ON \`$DB_NAME\`.* TO '$READER'@'%';
FLUSH PRIVILEGES;" >/dev/null
compose exec -T db mariadb -h 127.0.0.1 -u"$READER" -p"$reader_pw" -N -B "$DB_NAME" \
  -e "SELECT COUNT(*) FROM ${PREFIX}ticket" >/dev/null
dsn="mysql://$READER:$reader_pw@127.0.0.1:$DB_PORT_ON_HOST/$DB_NAME"

# 6. sweep/.env.osticket, keeping any line that is not ours.
tmp="$ENV_FILE.tmp"
{
  if [ -f "$ENV_FILE" ]; then
    grep -v -E '^(OSTICKET_API_KEY|OSTICKET_MYSQL_DSN)=' "$ENV_FILE" || true
  fi
  printf 'OSTICKET_API_KEY=%s\n' "$api_key"
  printf 'OSTICKET_MYSQL_DSN=%s\n' "$dsn"
} >"$tmp"
mv "$tmp" "$ENV_FILE"

for python in "${PYTHON:-}" python3 python; do
  if [ -n "$python" ] && command -v "$python" >/dev/null 2>&1; then
    if ! "$python" -c 'import pymysql' >/dev/null 2>&1; then
      echo "osticket setup: the readback adapter needs PyMySQL: $python -m pip install PyMySQL"
    fi
    break
  fi
done

echo "osticket setup: API key bound to $client_ip, readback user $READER, wrote $ENV_FILE"
echo "URL: $BASE_URL/scp/"
echo "Login: $(container_env ADMIN_USER) / $(container_env ADMIN_PASS)"
echo "Add to .env: OSTICKET_API_KEY=$api_key OSTICKET_MYSQL_DSN=$dsn"
