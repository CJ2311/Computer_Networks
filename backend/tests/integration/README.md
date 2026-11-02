# PostgreSQL checkout integration tests

These tests require a separately provisioned PostgreSQL 18 database named
`inventory_checkout_test`, with username `inventory_checkout_test_login` on
localhost port 5432. URL query options are rejected. They do not create a database or fall back to
`inventory_db`. Missing configuration produces explicit skips; an unsafe URL or
an unreachable configured database fails setup.

Required setup by the database administrator:

1. Create the dedicated database `inventory_checkout_test` and a dedicated test
   login with CONNECT and CREATE privileges on that database. The test login
   should have no access to development or production databases.
2. Set `INVENTORY_TEST_DATABASE_URL` in the current process using your secret
   management mechanism. Its format is
   `postgresql+psycopg://inventory_checkout_test_login:URL_ENCODED_PASSWORD@127.0.0.1:5432/inventory_checkout_test`.
   Do not put credentials in source control or modify the application's `.env`.
3. From the repository root, run:

   ```powershell
   .venv\Scripts\python.exe -m pytest backend/tests/integration -v -rs -p no:cacheprovider
   ```

Each integration case creates a unique `checkout_it_<uuid>` schema, applies the
three unchanged migration revision functions, inserts fictional records, and
drops only that generated schema after the test. Committed writes never target
the application's configured engine. Interrupted runs can leave generated
schemas in the dedicated test database for administrator cleanup.

The fixtures require the exact database and login names, verify both current_user
and session_user, and reject elevated role attributes, INHERIT, or any role
memberships. Before schema creation they attempt a development-database connection
using ONLY the dedicated test login, with read-only mode forced and no SQL issued.
Only a server authorization denial (SQLSTATE 28000 or 42501) passes this check;
successful connections, wrong passwords, and network errors fail setup.

An autouse module fixture blocks connect() and raw_connection() on the existing
application engine, including SessionLocal's bound engine, for these tests. The
block is restored afterwards. No application business logic or authentication
mechanism is changed. This protects the existing engine, not arbitrary new engines
that future code might construct. No current integration fixture constructs a
development engine except the explicit negative connection probe above.

Do not enable --showlocals, SQL echo, environment dumps, or shell tracing: the URL
necessarily exists in process memory. Application imports still load .env through
existing configuration; supply INVENTORY_APP_PASSWORD=unused-integration-placeholder
in the test process before importing the application to avoid loading its actual
development password. This placeholder is not used to connect anywhere.

Provisioning must wait for approval. Use an administrator account for provisioning,
never for the tests. Required test privileges are LOGIN plus CONNECT and CREATE
on the test database, with no role memberships or elevated attributes. The test
database should remain administrator-owned, and PUBLIC privileges on that new
database and its public schema should be revoked. No development ACL changes are
required or authorized.

Before provisioning, inspect whether PUBLIC has CONNECT on inventory_db. If it
does, database grants alone cannot deny that access to the new login: NOINHERIT
and a per-role REVOKE do not remove PUBLIC privileges. Stop and obtain approval
for a narrowly scoped connection-rule solution or a separate PostgreSQL instance.
Do not change pg_hba.conf or development permissions automatically. After setup,
verify a test-database connection succeeds and the same login's inventory_db
connection fails specifically with authorization denial before running the suite.

Do **not** use the ordinary `alembic upgrade head` command to prepare this test
database: the current `backend/alembic/env.py` hardcodes `inventory_db` and does
not consume the test URL. The fixtures intentionally avoid that environment.

Coverage includes migrated schema comparison; HTTP checkout and persistence for
all three authorized staff roles; unavailable assets; missing, inactive, and
wrong-role borrowers; missing/invalid authentication headers; student issuers;
inactive issuers; rollback after a real foreign-key failure at commit; and two
independent sessions contending for the same PostgreSQL asset row lock. The
concurrency test requires both sessions to appear in `pg_stat_activity` waiting
on locks, then checks for exactly one success, one conflict, and one persisted
checkout. Tests use the existing auth dependencies, with development auth
enabled for the test process and only the database dependency redirected.

The original `test_dev_auth.py` still contains one read-only lookup of the
development `DEV-ADMIN` account; it is not a checkout integration test. Use the
integration-only command to run exclusively against the dedicated test database.
