#!/usr/bin/env bash
# Set up the FreeScout stack started from sweep/compose/freescout.yml.
#
# Run from the repository root, after `docker compose -f
# sweep/compose/freescout.yml up -d`:
#
#   bash sweep/setup/freescout.sh
#
# Safe to run again: every step checks what is already there.
#
#   1. Waits for FreeScout to answer (its first boot migrates the database
#      and can take several minutes).
#   2. Makes sure the admin user exists (the image creates it on a fresh
#      database; freescout:create-user is the fallback).
#   3. Creates or updates the mailbox support@freescout.test, fetching over
#      IMAP from the dovecot service and sending through the deliver service,
#      with an auto-reply on. FreeScout encrypts the IMAP password with its
#      app key, so the mailbox is saved through FreeScout's own Mailbox class.
#   4. Fetches once by hand to prove the IMAP settings work. After that the
#      image's scheduler fetches every minute.
#   5. Creates a read-only database user for the readback, with a fresh
#      random password, and writes FREESCOUT_MYSQL_DSN to sweep/.env.freescout.
#   6. Empties the outbound mail sink, so loop reads this run's mail only.
set -euo pipefail

COMPOSE_FILE="sweep/compose/freescout.yml"
ENV_FILE="sweep/.env.freescout"
SINK_DIR="sweep/.state/freescout"

URL="http://127.0.0.1:3400"
HEALTH_URL="${URL}/login"
TIMEOUT="${FREESCOUT_SETUP_TIMEOUT:-900}"

ADMIN_EMAIL="admin@freescout.test"
ADMIN_PASS="${FREESCOUT_ADMIN_PASS:-thq-freescout-admin}"
DB_ROOT_PASS="${FREESCOUT_DB_ROOT_PASS:-thq-freescout-root}"
DB_NAME="freescout"

MAILBOX_EMAIL="support@freescout.test"
IMAP_HOST="dovecot"
IMAP_USER="support"
# Must match the static password in sweep/compose/freescout/dovecot.conf.
IMAP_PASS="thq-freescout-imap"
SMTP_HOST="deliver"

READBACK_USER="thq_readback"
READBACK_HOST="127.0.0.1"
READBACK_PORT="3406"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "run this from the repository root: ${COMPOSE_FILE} not found" >&2
    exit 1
fi

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

# Artisan and PHP inside the FreeScout container, as the web server's user so
# nothing in storage/ ends up owned by root.
in_freescout() {
    compose exec -T -u nginx -w /www/html freescout "$@"
}

sql() {
    compose exec -T -e MYSQL_PWD="${DB_ROOT_PASS}" db \
        mariadb --user=root --batch --skip-column-names "${DB_NAME}" -e "$1"
}

wait_for() {
    local what="$1"
    shift
    local deadline=$((SECONDS + TIMEOUT))
    until "$@" >/dev/null 2>&1; do
        if ((SECONDS >= deadline)); then
            echo "gave up after ${TIMEOUT}s waiting for ${what}" >&2
            echo "check: docker compose -f ${COMPOSE_FILE} logs freescout" >&2
            exit 1
        fi
        sleep 5
    done
}

answers() {
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "${HEALTH_URL}")"
    [[ "${code}" == "200" ]]
}

migrated() {
    [[ -n "$(sql "SELECT 1 FROM mailboxes LIMIT 0; SELECT 'ok';")" ]]
}

echo "waiting for FreeScout at ${HEALTH_URL} (first boot takes a few minutes)"
wait_for "the database" sql "SELECT 1"
wait_for "FreeScout's migrations" migrated
wait_for "FreeScout to answer" answers

# --- 2. the admin user -------------------------------------------------------

if [[ "$(sql "SELECT COUNT(*) FROM users WHERE email = '${ADMIN_EMAIL}'")" == "0" ]]; then
    echo "creating admin ${ADMIN_EMAIL}"
    in_freescout php artisan freescout:create-user --no-interaction \
        --role=admin --firstName=Sweep --lastName=Admin \
        --email="${ADMIN_EMAIL}" --password="${ADMIN_PASS}" >/dev/null
fi

# --- 3. the mailbox ----------------------------------------------------------

echo "configuring mailbox ${MAILBOX_EMAIL}"
mailbox_out="$(
    compose exec -T -u nginx -w /www/html \
        -e THQ_MAILBOX_EMAIL="${MAILBOX_EMAIL}" \
        -e THQ_IMAP_HOST="${IMAP_HOST}" \
        -e THQ_IMAP_USER="${IMAP_USER}" \
        -e THQ_IMAP_PASS="${IMAP_PASS}" \
        -e THQ_SMTP_HOST="${SMTP_HOST}" \
        freescout php <<'PHP'
<?php
// Boot FreeScout and save the mailbox through its Mailbox class, so the IMAP
// password is encrypted with the app key exactly as the settings page does
// it, and the mailbox observer creates the folders.
require '/www/html/vendor/autoload.php';
$app = require '/www/html/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

use App\Mailbox;

$email = getenv('THQ_MAILBOX_EMAIL');
$mailbox = Mailbox::where('email', $email)->first();
if (!$mailbox) {
    $mailbox = new Mailbox();
    $mailbox->email = $email;
}
$mailbox->name = 'TestingHQ sweep';

$mailbox->in_protocol = Mailbox::IN_PROTOCOL_IMAP;
$mailbox->in_server = getenv('THQ_IMAP_HOST');
$mailbox->in_port = 143;
$mailbox->in_username = getenv('THQ_IMAP_USER');
$mailbox->in_password = getenv('THQ_IMAP_PASS');
$mailbox->in_encryption = Mailbox::IN_ENCRYPTION_NONE;
$mailbox->in_validate_cert = false;

$mailbox->out_method = Mailbox::OUT_METHOD_SMTP;
$mailbox->out_server = getenv('THQ_SMTP_HOST');
$mailbox->out_port = 25;
$mailbox->out_encryption = Mailbox::OUT_ENCRYPTION_NONE;

// An auto-reply, so `loop` has something to see: the reply to an
// ordinary message, and none to an auto-responder's.
$mailbox->auto_reply_enabled = true;
$mailbox->auto_reply_subject = 'We received your message';
$mailbox->auto_reply_message = 'Thank you for writing. This is an automatic reply.';

$mailbox->save();
echo 'MAILBOX_ID='.$mailbox->id.PHP_EOL;
PHP
)"
mailbox_id="$(printf '%s\n' "${mailbox_out}" | sed -n 's/^MAILBOX_ID=\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
if [[ -z "${mailbox_id}" ]]; then
    echo "could not create the mailbox; FreeScout said:" >&2
    printf '%s\n' "${mailbox_out}" >&2
    exit 1
fi

# --- 4. one fetch by hand ------------------------------------------------------

echo "fetching mailbox ${mailbox_id} once over IMAP"
fetch_out="$(in_freescout php artisan freescout:fetch-emails --mailboxes="${mailbox_id}" 2>&1 || true)"
# FetchEmails reports a failure as "[<time>] Error: ..." and a missing folder
# as "[<time>] IMAP folder ...", so subjects of fetched mail cannot trip this.
if printf '%s\n' "${fetch_out}" | grep -qE '\] (Error: |IMAP folder )'; then
    echo "FreeScout could not fetch from ${IMAP_HOST}:" >&2
    printf '%s\n' "${fetch_out}" >&2
    exit 1
fi

# --- 5. the readback user ------------------------------------------------------

readback_pass="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
sql "CREATE USER IF NOT EXISTS '${READBACK_USER}'@'%' IDENTIFIED BY '${readback_pass}';
ALTER USER '${READBACK_USER}'@'%' IDENTIFIED BY '${readback_pass}';
GRANT SELECT ON ${DB_NAME}.conversations TO '${READBACK_USER}'@'%';
GRANT SELECT ON ${DB_NAME}.threads TO '${READBACK_USER}'@'%';
GRANT SELECT ON ${DB_NAME}.mailboxes TO '${READBACK_USER}'@'%';
GRANT SELECT ON ${DB_NAME}.attachments TO '${READBACK_USER}'@'%';
FLUSH PRIVILEGES;" >/dev/null

dsn="mysql://${READBACK_USER}:${readback_pass}@${READBACK_HOST}:${READBACK_PORT}/${DB_NAME}"

touch "${ENV_FILE}"
chmod 600 "${ENV_FILE}"
kept="$(grep -v '^FREESCOUT_MYSQL_DSN=' "${ENV_FILE}" || true)"
{
    if [[ -n "${kept}" ]]; then
        printf '%s\n' "${kept}"
    fi
    printf 'FREESCOUT_MYSQL_DSN=%s\n' "${dsn}"
} > "${ENV_FILE}.tmp"
mv "${ENV_FILE}.tmp" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# --- 6. the outbound sink --------------------------------------------------------

mkdir -p "${SINK_DIR}"
: > "${SINK_DIR}/outbound.jsonl"

echo "URL: ${URL}"
echo "Login: ${ADMIN_EMAIL} / ${ADMIN_PASS}"
echo "Add to .env: FREESCOUT_MYSQL_DSN=${dsn}"
