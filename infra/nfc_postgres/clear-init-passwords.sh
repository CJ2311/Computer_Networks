# Sourced by the official entrypoint after initialization SQL, not executed in a
# child process. Keep this file non-executable (0644) and the only init hook.
# The entrypoint's temporary server stop uses pg_ctl, not authenticated psql.
# Nothing after this hook may require initialization credentials.
unset PGPASSWORD POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE
