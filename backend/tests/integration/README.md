## Phase 3 checkout, return and maintenance integration

No migration or model change is needed. Endpoint paths and roles are preserved:
ADMIN/FACULTY/LAB_ASSISTANT issue loans; only FACULTY/LAB_ASSISTANT receive returns.
Services refresh persisted activation and role records, including after lock waits.
No new maintenance routes or frontend changes are included.

Standalone writes require a fresh, active, Engine-bound Session with no root
transaction, SAVEPOINT, failed transaction, or pending ORM work. Entry rejection
never commits or rolls back the caller's transaction, including already-flushed
ORM writes and direct SQL. Connection-bound Sessions are not supported for writes.
Authentication continues using get_db; checkout/return POSTs receive a distinct
Session through get_inventory_write_db. Persisted activation/roles are still checked
inside the write transaction. Callers pass an actor reference/ID-bearing User,
not a User loaded by first querying the write Session. Read services may continue
using read transactions. Do not commit/rollback an unknown transaction to satisfy
this contract; acquire a separate fresh Session instead.
The wrapper explicitly begins and captures its root SessionTransaction, commits
that object only, and rolls it back only when it is still the Session's root.
A transaction identity token replaces the old boolean. Recursive services,
premature commits, and replaced/ended roots are rejected. BaseException cleanup
also covers interruptions. A replacement transaction is never committed or rolled
back by the wrapper; its caller must close/clean up the Session.
Never compose standalone committing services. Return planning accepts already
locked custody records and queries maintenance before any ORM mutation. Persistence
uses guarded parameterless flushes: returned checkout -> held asset -> maintenance
request/version -> immutable event -> validated response -> one commit. Failure at
any stage rolls back the owned transaction. Unrelated pending ORM changes cause
rejection. Session-local events block unapproved autoflush/commit-time flushing;
connection-local SQL guards validate the stage's table, one primary-key record,
and pending object before DML. This catches late before_flush cascades even when
callbacks run after the initial pending-state check. Relationship changes are
unsupported in these scalar-only workflow stages. before_commit callbacks cannot
introduce an implicit flush. Hooks are removed after each operation, without
changing global Engine/Mapper dispatchers. These guards are for trusted inventory
code, not a sandbox against arbitrary Python callbacks issuing raw SQL or COMMIT.
The SQL guard deliberately supports current single-table, single-UUID-id ORM
writes only; bulk writes and relationship-based persistence require a separately
reviewed contract.
Verification retains COMPLETED request -> event -> asset release ordering, with
the existing deferred current-event foreign key unchanged.

Lock order is asset -> checkout -> maintenance request. Locked records use
populate_existing to refresh the identity map. Checkout explicitly rejects a hold,
active maintenance, unavailable or non-working assets, and inconsistent active
loans. PostgreSQL clock_timestamp() is read after lock acquisition for checkout
issuance and deadline validation, then checked again before commit. This is the
last decision point, not a guarantee against clock changes or delay inside COMMIT.
The return timestamp is one database instant reused for its report/event times.
Deadlocks, serialization failures and lock timeouts return sanitized retryable
409 responses after rollback; other DBAPI failures return sanitized 503 responses.

DAMAGED creates DAMAGE; UNDER_REPAIR creates MALFUNCTION, both OPEN without
starting repair. A WORKING return with an unexplained hold or prior non-working
condition creates INSPECTION. Existing OPEN requests are reused without changing
their original description, reporter, report timestamp or assignment, incrementing
the locked version and appending RETURN_OBSERVED. New requests have a REPORTED
event with return context. The event note starts with JSON checkout_id/condition context, followed by verbatim
return notes (avoiding JSON expansion past the audit note length limit). checkout_id is descriptive context,
not a database-enforced borrowing foreign key. Original borrowing fields remain
unchanged and original loan notes are retained with an appended return annotation.
Borrowing notes themselves are not an immutable database audit log.

A hold with cancelled history and no active request requires an explicit optional
ReturnCreate.recovery_of_request_id. It must name a cancelled request on the same
held asset that has not already been replaced. Recovery creates linked OPEN
INSPECTION and must complete the normal independent verification workflow.
Unknown/ambiguous provenance returns 409; the service never chooses a parent.
Already-replaced recovery parents require faculty reconciliation of the chain.
An active loan with IN_PROGRESS/AWAITING_VERIFICATION maintenance returns 409
without rewriting history. An inconsistent loan with RESERVED is also rejected;
there is no reservation ownership/provenance field to justify changing it.
Standalone maintenance continues preserving RESERVED through verification.
Existing legacy RESERVED records require separately authorized reconciliation.

Frontend consumers must accept MAINTENANCE return availability and the additive
maintenance_hold/maintenance_request_id response fields. Asset responses expose
maintenance_hold and the availability enum includes MAINTENANCE. Independent repair
verification, audit immutability and all database constraints remain unchanged.
Staff access remains laboratory-wide pending lab-specific authorization.

Ownership tests are in test_inventory_transaction_ownership.py (real SQLAlchemy
Sessions over a recording DBAPI stub, without an actual database) and
integration/test_inventory_transaction_ownership_postgres.py (persisted state).
Existing mock-based policy tests use inventory_session_stub.py; production guards
are not weakened. PostgreSQL helpers label after_begin rather than starting a
caller-owned transaction, and deterministic lock races acquire locks inside the
service-owned root. Stale-cache tests explicitly end their own setup read
transaction with expire_on_commit=False before invoking a standalone write.
Phase 3 tests are in test_inventory_workflows.py (unit and integration). Older
RESERVED return expectations deliberately change to MAINTENANCE. Fabricated users
are now rejected with 403 before writes; real FK rollback is injected separately.
Run the secure PostgreSQL runner below for the entire integration regression suite.
Do not interpret collection-only validation or missing-credential skips as passes.

# PostgreSQL checkout integration tests

## Phase 2 maintenance service logic

`backend/app/services/maintenance.py` implements standalone report, assignment,
start, repair submission, independent PASS/FAIL verification, cancellation, and
linked INSPECTION recovery operations. It also provides authorized read-only
request and paginated event views. There are no maintenance routes, frontend
changes, or integrations with checkout/return services in this phase.

Services take an authenticated User identity from a trusted caller and re-read
the persisted role/activation; they do not replace authentication. STUDENT and
inactive accounts have no maintenance access. Active FACULTY, LAB_ASSISTANT, and
ADMIN can report and read. Only FACULTY/ADMIN assign, verify, and cancel.
Assignees must be active FACULTY/LAB_ASSISTANT; only that assigned user can start
or submit repairs. ADMIN cannot perform repairs. Verification is denied to the
repair-completion actor; absence of an independent verifier leaves the hold in
place. Staff access is laboratory-wide until lab-specific scopes are introduced.

Writes own the supplied clean, non-nested Session's transaction, commit once, and
roll back on validation/database/commit failures. Unrelated pending ORM changes
are rejected before the operation starts, without discarding the caller's work.
Use a fresh session for each operation. Future checkout/return integration must
provide an explicitly shared transaction boundary, not nest these committing
operations inside an existing service.

Every mutation refreshes locked rows in asset -> active checkout -> request order,
rechecks authorization after lock waits, uses PostgreSQL clock_timestamp(), and
persists the corresponding immutable event/version atomically. Required version
arguments are positive incrementable PostgreSQL integers; stale versions return
409. Deadlock, serialization, and lock-timeout conflicts roll back and return a
sanitized retryable conflict; there are no automatic retries. Results are frozen
snapshots captured under the locks before commit, avoiding post-commit lazy reads.

OPEN -> IN_PROGRESS -> AWAITING_VERIFICATION -> COMPLETED is the successful path.
FAIL returns AWAITING_VERIFICATION to IN_PROGRESS, preserves the previous attempt
in events, and keeps the hold. Assignment/reassignment is allowed only in OPEN
or IN_PROGRESS; a same-assignee no-op is rejected. Repair and verification require
physical custody and no active loan. Cancellation preserves asset state and hold.
Recovery creates a new OPEN INSPECTION request linked to the same asset's cancelled
parent and must pass all work/verification stages; it never releases equipment.
Original descriptions and supplied note formatting are preserved.

DAMAGE and MALFUNCTION use the existing DAMAGED non-working classification;
INSPECTION preserves the current condition. Repair start sets UNDER_REPAIR;
submission reports WORKING provisionally but retains the hold. PASS records the
verifier and service timestamps and clears the hold only after the COMPLETED
request and audit event have been flushed, satisfying Phase 1's database guards.
Mutations are staged using parameterless Session.flush(): later-stage objects
remain unchanged until their turn. Each flush rejects unrelated pending ORM work,
and a final clean-session check prevents commit from implicitly flushing it.
On PASS the order is request completion -> audit event -> asset hold release;
the audit-event FK remains deferred and must still be satisfied at commit.
Selective Session.flush(objects=...) is not used (deprecated in SQLAlchemy 2.1).
Service integration tests treat SQLAlchemy deprecation warnings as failures.
Existing RESERVED availability is always preserved conservatively, including
ambiguous legacy reservations. Retired/missing assets require separate
reconciliation; maintenance cannot silently restore them.

Reporting while BORROWED retains the active checkout and availability. Standalone
maintenance lifecycle operations never edit loans. Phase 3 return integration
uses the transactionless helpers described below within the return transaction.

test_maintenance_service.py runs the real lifecycle, authorization, reservation/
loan preservation, audit, rollback, and concurrent report/assignment/verification/
cancel-start cases. Race tests observe both workers blocked in PostgreSQL before
releasing the asset lock. Read service tests use a read-only transaction. Use the
existing secure runner below; missing dedicated credentials leave these tests
pending and never justify accessing inventory_db.

## Phase 1 maintenance database foundation

Revision `a3c7e91b6d20`, following `ef14e8f62260`, adds maintenance requests,
append-only events, an asset maintenance_hold flag (default false), and the
MAINTENANCE availability value. Condition and availability remain separate.
No legacy availability/condition values are reclassified. If existing AVAILABLE
assets are non-working, upgrade fails transactionally and requires separately
approved reconciliation; it does not silently change those assets.

Phase 1 is the database foundation; Phase 2 adds the service logic described above.
No maintenance API endpoints exist. Phase 3 integrates checkout and return services;
a separately approved API phase is still required for maintenance lifecycle access.

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
authorization are implemented in the Phase 2 service; database protections remain
unchanged.

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
reported condition, resulting availability, `maintenance_hold`, nullable
`maintenance_request_id`, and `is_overdue`. Lateness means
`returned_at > due_at`; equality is on time. PostgreSQL `clock_timestamp()` is
read after locks are acquired, so the recorded time includes time spent waiting.
Original borrower, issuer, deadline, checkout ID, and checkout time are retained.
Original notes remain intact with a labeled JSON return entry appended.

The service locks the asset before the checkout and refreshes locked ORM state.
Both records must still describe an active BORROWED loan before either is changed.
Missing loans return 404. Duplicate returns and inconsistent asset/loan states
return 409. Condition updates and the return are committed in one transaction.

WORKING returns without unresolved maintenance become AVAILABLE. DAMAGED or
UNDER_REPAIR returns set maintenance_hold and MAINTENANCE availability, creating
or reusing an OPEN maintenance request. WORKING returns with unresolved defects
stay held pending independent verification. RESERVED is no longer assigned by
returns; genuine and ambiguous legacy reservations are never automatically
reclassified. An active loan paired with RESERVED is rejected for reconciliation.

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
inactive issuers; persisted issuer/receiver authorization; injected real foreign-key rollback; and two
independent sessions contending for the same PostgreSQL asset row lock. The
concurrency test requires both sessions to appear in `pg_stat_activity` waiting
on locks, then checks for exactly one success, one conflict, and one persisted
checkout. Tests use the existing auth dependencies, with development auth
enabled for the test process and only the database dependency redirected.

The original `test_dev_auth.py` still contains one read-only lookup of the
development `DEV-ADMIN` account; it is not a checkout integration test. Use the
integration-only command to run exclusively against the dedicated test database.
