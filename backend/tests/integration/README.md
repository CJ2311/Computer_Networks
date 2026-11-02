# PostgreSQL checkout integration tests

## Equipment returns

`POST /checkouts/{checkout_id}/return` accepts an explicit `condition_status`
(`WORKING`, `DAMAGED`, or `UNDER_REPAIR`) and optional `notes` (up to 1000
characters). Use `UNDER_REPAIR` to report malfunctioning equipment. Only active
FACULTY and LAB_ASSISTANT users may record returns; students and administrators
are denied. The receiving user and return timestamp are server-controlled.

A successful return responds with HTTP 200, the existing checkout fields, the
reported condition, resulting availability, and `is_overdue`. Lateness means
`returned_at > due_at`; equality is on time. PostgreSQL `clock_timestamp()` is
read after locks are acquired, so the recorded time includes time spent waiting.
Original borrower, issuer, deadline, checkout ID, and checkout time are retained.
Original notes remain intact with a labeled JSON return entry appended.

The service locks the asset before the checkout and refreshes locked ORM state.
Both records must still describe an active BORROWED loan before either is changed.
Missing loans return 404. Duplicate returns and inconsistent asset/loan states
return 409. Condition updates and the return are committed in one transaction.

WORKING returns become AVAILABLE. DAMAGED or UNDER_REPAIR returns become RESERVED.
**RESERVED is only a temporary maintenance hold and must eventually be replaced
with a dedicated maintenance availability state.** The checkout service rejects
both this hold and non-working conditions; this workflow never releases a held
asset automatically.

**Notes are only temporary historical storage. Structured condition history is
required for future maintenance and auditing.** Each return entry captures the
reported condition, receiver, database timestamp, and optional notes, preserving
that information even when the asset condition later changes. Notes are not an
immutable audit log. No schema or migration changes are needed for this version.

The integration suite includes return persistence/history, reborrowing, condition
holds, overdue calculation, authorization, payload validation, duplicate returns
before/after reborrowing, real foreign-key rollback, simultaneous returns, and
return/checkout races. Race tests require PostgreSQL to confirm both workers are
waiting on the asset lock and assert that return time is after lock release.

Run database-free unit tests separately from `test_get_current_dev_admin`, which
reads development data. Do not run the full suite when development access is
prohibited. The secure runner below continues to run integration tests only.

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
3. From an interactive PowerShell terminal in the repository root, use this
   runner. It prompts without echo, keeps the URL only in the child process,
   refuses insecure password-input fallback, and runs only the integration suite:

   ```powershell
   $runner = @'
   import getpass
   import os
   import warnings

   os.environ['INVENTORY_APP_PASSWORD'] = 'unused-integration-placeholder'
   os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
   os.environ.pop('PYTEST_ADDOPTS', None)
   os.environ.pop('PYTEST_PLUGINS', None)

   from sqlalchemy.engine import URL

   warnings.simplefilter('error', getpass.GetPassWarning)
   try:
       password = getpass.getpass('Dedicated test database password: ')
   except (getpass.GetPassWarning, EOFError):
       raise SystemExit('Secure password input unavailable; no tests run.')
   if not password:
       raise SystemExit('Empty password; no tests run.')

   os.environ['INVENTORY_TEST_DATABASE_URL'] = URL.create(
       'postgresql+psycopg',
       username='inventory_checkout_test_login',
       password=password,
       host='127.0.0.1',
       port=5432,
       database='inventory_checkout_test',
   ).render_as_string(hide_password=False)
   del password

   import pytest

   try:
       result = pytest.main([
           'backend/tests/integration',
           '-v', '-rs', '--tb=short',
           '-o', 'addopts=',
           '-p', 'no:cacheprovider',
       ])
   finally:
       os.environ.pop('INVENTORY_TEST_DATABASE_URL', None)

   raise SystemExit(result)
   '@

   & '.\.venv\Scripts\python.exe' -c $runner
   ```

Each integration case creates a unique `checkout_it_<uuid>` schema, applies the
three unchanged migration revision functions, inserts fictional records, and
drops only that generated schema after the test. Committed writes never target
the application's configured engine. Interrupted runs can leave generated
schemas in the dedicated test database for administrator cleanup.

The fixtures require the exact database and login names, verify both current_user
and session_user, and reject elevated role attributes, INHERIT, or any role
memberships. The suite never attempts a connection to inventory_db. The database
administrator must verify the test login's development-database connection denial
outside the suite; that verification has been reported complete for this setup.

An autouse module fixture blocks connect() and raw_connection() on the existing
application engine, including SessionLocal's bound engine, for these tests. The
block is restored afterwards. No application business logic or authentication
mechanism is changed. This protects the existing engine, not arbitrary new engines
that future code might construct. All engines constructed by these fixtures target
the validated test database.

Do not enable --showlocals, SQL echo, environment dumps, or shell tracing: the URL
necessarily exists in process memory. Application imports still load .env through
existing configuration; supply INVENTORY_APP_PASSWORD=unused-integration-placeholder
in the test process before importing the application to avoid loading its actual
development password. This placeholder is not used to connect anywhere.

Provisioning and connection-rule changes are outside this suite. Use an
administrator account for provisioning, never for the tests. Required test privileges are LOGIN plus CONNECT and CREATE
on the test database, with no role memberships or elevated attributes. The test
database should remain administrator-owned, and PUBLIC privileges on that new
database and its public schema should be revoked. No development ACL changes are
required or authorized.

Before provisioning, inspect whether PUBLIC has CONNECT on inventory_db. If it
does, database grants alone cannot deny that access to the new login: NOINHERIT
and a per-role REVOKE do not remove PUBLIC privileges. Stop and obtain approval
for a narrowly scoped connection-rule solution or a separate PostgreSQL instance.
Do not change pg_hba.conf or development permissions automatically. The
administrator's prior connection-denial verification is a prerequisite, not an
operation performed by this runner or these fixtures.

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
