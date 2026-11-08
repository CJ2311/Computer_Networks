#!/bin/sh
set -eu
umask 077
# The generated file is authoritative; never let an inherited libpq password
# override the initialization password selected by the upstream entrypoint.
unset PGPASSWORD POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE
# Fixed vocabulary only: never copy command output, arguments or environment here.
# Container writable layer survives exit and is readable with docker cp (no logs).
stage=opt-in
status=/run/nfc-startup-status
mark() { printf 'stage=%s\n' "$stage" > "$status"; }
trap 'rc=$?; printf "stage=%s exit=%s\n" "$stage" "$rc" > "$status"' 0
mark
[ "${NFC_DISPOSABLE_OPT_IN:-}" = "YES_ISOLATED_PG18" ] || exit 70
# Refuse reuse: a new Compose project/identity volume is mandatory for each run.
stage=reuse-check; mark
[ ! -e /run/nfc-harness/run_id ] || exit 71
[ ! -e /run/nfc-harness/admin_password ] || exit 71
stage=password-write; mark
od -An -N48 -tx1 /dev/urandom | tr -d ' \n' > /run/nfc-harness/admin_password
# POSIX sh has no pipefail: also reject a failed/short entropy read.
stage=password-validate; mark
password=$(cat /run/nfc-harness/admin_password)
[ "${#password}" -eq 96 ] || exit 72
case "$password" in *[!0123456789abcdef]*) exit 72 ;; esac
unset password
stage=run-id-write; mark
cat /proc/sys/kernel/random/uuid > /run/nfc-harness/run_id
stage=identity-permissions; mark
# A dedicated group shared with the non-root worker needs read, never write.
# PostgreSQL owns the files so the upstream privilege drop retains read access.
chown postgres:18084 /run/nfc-harness /run/nfc-harness/admin_password /run/nfc-harness/run_id
chmod 0750 /run/nfc-harness
chmod 0440 /run/nfc-harness/admin_password /run/nfc-harness/run_id
# The tmpfs hides image ownership; the entrypoint chowns only PGDATA, not
# its ancestors. With umask 077 both ancestors must belong to postgres.
stage=data-directory; mark
[ "${PGDATA:-}" = /var/lib/postgresql/18/docker ] || exit 73
mkdir -p /var/lib/postgresql/18
chown postgres:postgres /var/lib/postgresql /var/lib/postgresql/18
chmod 0700 /var/lib/postgresql /var/lib/postgresql/18
export POSTGRES_USER=nfc_controller
export POSTGRES_DB=postgres
export POSTGRES_PASSWORD_FILE=/run/nfc-harness/admin_password
export POSTGRES_INITDB_ARGS='--auth-host=scram-sha-256 --auth-local=scram-sha-256'
export POSTGRES_HOST_AUTH_METHOD=scram-sha-256
# Upstream loads _FILE and sets PGPASSWORD only for its initialization sequence.
# The final sourced initialization hook clears password variables before handoff.
stage=run-id-read; mark
run_id=$(cat /run/nfc-harness/run_id)
stage=postgres-entrypoint; mark
exec /usr/local/bin/docker-entrypoint.sh postgres \
  -c listen_addresses=127.0.0.1 \
  -c "cluster_name=nfc-disposable-$run_id" \
  -c log_statement=none -c log_min_error_statement=panic \
  -c log_parameter_max_length_on_error=0
