# PostgreSQL checkout integration tests

## Phase 1 maintenance database foundation

Revision `a3c7e91b6d20`, following `ef14e8f62260`, adds maintenance requests,
append-only events, an asset maintenance_hold flag (default false), and the
MAINTENANCE availability value. Condition and availability remain separate.
No legacy availability/condition values are reclassified. If existing AVAILABLE
assets are non-working, upgrade fails transactionally and requires separately
approved reconciliation; it does not silently change those assets.

This is database foundation only. No maintenance services, API endpoints,
assignment policy, or lifecycle commands exist yet. Do not enable operational
maintenance use until checkout/return integration and authorization are implemented.
Existing returns still use the temporary RESERVED behavior described below.
Returns attempting to clear an already-set hold fail closed rather than release
equipment; the later integration phase must provide the full return workflow.

Maintenance requests carry the original issue, asset, reporter/time, current
status/assignee, start/repair/verification/service/cancellation fields, recovery
parent, and version. A composite foreign key requires recovery to reference the
same asset; a trigger requires a cancelled parent. A partial unique index allows
only one OPEN, IN_PROGRESS, or AWAITING_VERIFICATION request per asset.

Events carry the actor/time, positive sequence, type/note, and before/after
status, assignment, condition, availability, and hold snapshots. Repair notes are
append-only event notes. A deferred current-version foreign key requires each
request insertion or version increment to have its matching event by commit.
Original report fields and terminal requests are immutable; events cannot be
updated/deleted; neither history table can be deleted from or truncated through
ordinary DML. This protects retained history, not against a privileged owner
deliberately disabling safeguards. Exact event payload semantics and full lifecycle
authorization will be implemented in the service phase.

The application must lock asset -> active checkout -> maintenance request before
future mutations. Database request guards also lock the asset to check its hold;
direct request UPDATEs that disregard that ordering can deadlock and must roll
back. No active request may be inserted without a hold, and a hold cannot be
cleared while an active request remains. These guards do not implement repair or
verification services and never write checkout records.

Downgrade takes transaction-scoped exclusive locks and refuses to proceed if
either maintenance table contains any row, or any asset has a maintenance hold
or MAINTENANCE availability. It never deletes history or reclassifies assets to
make downgrade succeed. Run both upgrade and downgrade in a PostgreSQL transaction.

New tables/functions revoke PUBLIC access. The migration neither creates roles
nor grants access to production/development accounts. A deployment administrator
must separately verify the migration owner is not the runtime login or a role
the runtime can assume. The runtime needs SELECT/INSERT/UPDATE on
maintenance_requests, SELECT/INSERT on maintenance_events, and its existing
appropriate asset/user privileges. It must have no UPDATE/DELETE/TRUNCATE rights
on events, no DELETE/TRUNCATE rights on requests, and no ownership/DDL privileges.
Review inherited/default grants too; owner-level fixture tests cannot verify
production privilege separation. Trigger functions use invoker rights and a
pg_catalog-only search path; references use the triggering table's quoted schema.

Shared conftest.py fixtures retain the exact dedicated database/login, local
connection, restricted-role, and development-engine-blocking checks. Each schema
now applies all four revision functions, including this foundation revision,
without executing env.py. test_maintenance_foundation.py covers migration round
trips, legacy preservation, constraints/indexes, audit retention, recovery,
deferred audit atomicity, rollback, duplicate concurrency, and downgrade refusal.
Use the secure runner below to execute these tests; never ordinary Alembic commands.

## Borrowing history and overdue tracking

`GET /checkouts` lists borrowing records. `GET /checkouts/{checkout_id}` retrieves
one record. Both require existing authentication and an active STUDENT, FACULTY,
LAB_ASSISTANT, or ADMIN account. Students are restricted to their own borrower ID
in SQL before ordering/pagination. A foreign `student_id` filter returns 403;
another student's checkout ID returns the same 404 as a nonexistent ID.

Faculty, lab assistants, and administrators currently have laboratory-wide read
access. **This is a temporary authorization limitation: lab-specific access must
be implemented once assigned-lab scopes exist.** No department or lab boundary
is inferred from the issuing staff member. This does not change permissions for
creating checkouts or recording returns.

List filters combine with AND:

| Parameter | Meaning |
| --- | --- |
| equipment_id | Equipment definition UUID |
| asset_id | Physical asset UUID |
| student_id | Borrower UUID; students may specify only their own |
| status | BORROWED or RETURNED |
| overdue | true: currently overdue; false: everything not currently overdue |
| checked_out_from | Inclusive checkout timestamp, with timezone |
| checked_out_before | Exclusive checkout timestamp, with timezone |
| limit | Default 25; range 1 through 100 |
| offset | Default 0; nonnegative, at most PostgreSQL's signed 64-bit maximum |

Dates normalize to UTC. The start must precede the end when both are supplied.
Malformed filters, unknown query parameters, and invalid pagination return 422.
`status=RETURNED&overdue=true` produces an empty list. A valid filter with no
matching records also produces an empty list; no borrower existence lookup is
performed. Sorting is checked_out_at DESC, id DESC, including ties.

Responses expose loan/asset/equipment identifiers, asset code, equipment name,
borrower and staff UUIDs, status, and timestamps. User profiles, names, college
IDs, email addresses, and free-form checkout/return notes are not exposed.
List responses contain items, limit, offset, has_more, and as_of; detail responses
contain a single item with as_of. The service fetches at most limit + 1 rows and
does not issue an unrestricted total-count query.

Each request reads PostgreSQL clock_timestamp() once and reuses that instant as
as_of in all SQL calculations and filters:

- is_currently_overdue: BORROWED and due_at < as_of.
- was_returned_late: RETURNED and returned_at > due_at.

Equality is on time. Historical late returns never enter the currently overdue
filter. The existing return endpoint's is_overdue field retains its original
meaning and schema. No overdue flags are stored or updated by these GETs.

These endpoints use SELECTs without row locks, flushes, or commits, with autoflush
suppressed. Integration GET sessions additionally enforce a read-only transaction
to catch accidental writes. Tests cover two authenticated students, all authorized
staff roles, filters, pagination, data minimization, one clock lookup per request,
exact deadline boundaries, timezone equivalence, and unchanged database records.

Offset pagination is deterministic for a fixed dataset but is not a snapshot
across requests; concurrent inserts can shift pages. Large offsets may become
expensive. Cursor pagination and query-plan-driven indexes are future work if
data volume warrants them. Existing migrations and schemas are unchanged.

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
four migration revision functions (three unchanged originals and the maintenance
foundation revision), inserts fictional records, and
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
