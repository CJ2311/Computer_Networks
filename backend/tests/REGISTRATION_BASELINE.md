# Phase 4A registration validation and transaction safety

## Current contract (Task 2)

POST /equipment and POST /assets retain their paths, success responses and role
permissions. Active ADMIN, FACULTY and LAB_ASSISTANT accounts may register;
students, inactive accounts and unauthenticated requests are rejected. Authentication
continues using get_db; the POST write Session uses get_inventory_write_db and is
separate from authentication. Staff ID, active state and role are rechecked against
persisted records inside the write transaction. GET endpoints are unchanged.

AssetCreate now strips surrounding whitespace and forbids extra fields. Required
asset_code/location reject blanks, with maximum lengths 50/150. Optional NFC tag
IDs and serial numbers have maximum length 100; stripped empty strings become
NULL. Nonblank identifiers retain case and internal spaces. Clients must omit id,
created_at, availability_status, condition_status, maintenance_hold and any other
unknown fields; supplying them returns 422, even when the value equals a default.
The existing AVAILABLE/WORKING/false defaults remain server/model controlled.
EquipmentCreate's existing text, tracking-type, precision and price validations
remain intact.

Database uniqueness is unchanged: asset_code and nfc_tag_id are globally unique;
serial_number is unique per equipment_id. The same serial number across different
equipment definitions remains allowed. Multiple NULL optional identifiers remain
allowed. Surrounding spaces no longer distinguish newly registered identifiers. No
migration or data cleanup/reclassification of existing identifiers is performed;
legacy values containing surrounding spaces may need separate reconciliation.

## Ownership and persistence

Registration was checked against the Phase 3 guard: both models use one table,
one UUID primary key and scalar-only inserts, so guarded parameterless flushes are
supported. Authentication's existing read transaction cannot be adopted. Both
services now require a fresh, active, Engine-bound write Session, reject existing
roots/SAVEPOINTs/failed Sessions/pending work without ending caller transactions,
and reuse the Phase 3 transaction decorator and actual root-identity ownership.

Direct callers must supply a User ID reference as the actor argument; do not load
that actor by first querying the write Session. Its caller-supplied role is not
trusted. Responses are Pydantic snapshots rather than returned ORM instances:
authorize -> asset equipment/tracking check (when applicable) -> add -> guarded
flush -> response validation -> commit the owned root. There is no post-commit
refresh. Internal SQLAlchemy defaults are loaded during flush before validation.
No stock/quantity borrowing, update endpoints, or relationship writes are added.

Pre-commit insertion or response failures roll back only the owned root.
Integrity conflicts produce sanitized 409; missing equipment is 404; equipment
not tracked INDIVIDUAL is 400; invalid HTTP payloads are 422. Unexpected DBAPI
failures produce sanitized 503 without raw SQL, parameters or connection details.
The shared decorator adds an optional uncertain_commit_detail used only by
registration; existing Phase 3 callers retain their existing default behavior.

A failed COMMIT acknowledgement can mean either no commit or a successful server
commit. Registration reports 503: 'Registration outcome could not be confirmed.
Check existing records before retrying.' Rollback is attempted only if the captured
root still belongs to the Session. A successfully ended root is not rolled back,
and the API does not claim rollback is guaranteed. No automatic retry is performed.
Equipment names are not unique, so retrying blindly may create another definition;
request idempotency/reconciliation identifiers would require separate design.
Request Sessions are closed on exit; discard Sessions after cleanup failures.
Database commit and response delivery cannot be made atomic by this change.

## Tests and historical corrections

- test_registration.py covers schemas, service rollback, response-before-commit,
  persisted authorization, unknown/state fields, HTTP error mapping, owner-entry
  rejection, unrelated pending objects and commit acknowledgement failures.
- registration_helpers.py provides fictional payloads and ASGI transport, with
  separate overrides for authentication and inventory write dependencies.
- integration/test_registration_postgres.py uses the existing dedicated fixture
  for real constraints/defaults, nullable/normalized uniqueness, rollback after
  INSERT/response failure, caller-owned SQL/ORM transactions, persisted role changes,
  and both real committed/uncommitted outcomes of injected acknowledgement loss.
- Existing Phase 3 ownership, audit ordering and concurrency regressions remain
  part of the full suites.

The original baseline's explicitly named known_gap characterizations are updated
to assert corrected validation and rollback. The old post-commit refresh failure
case is replaced by pre-commit failure rollback plus explicit uncertain-commit
coverage; it is not hidden by assuming every error means the insert rolled back.
Mocks validate service decisions, not database persistence; PostgreSQL cases make
separate persisted-state assertions. Former optional blank strings now normalize
to NULL and former ignored extra fields now intentionally fail with 422.

## Safe verification

Database-free execution blocks application engine connect/raw_connection, sets a
placeholder INVENTORY_APP_PASSWORD before importing app configuration, disables
pytest plugin autoload, excludes integration tests and excludes the deliberate
development lookup test_get_current_dev_admin.

Use only the secure runner in integration/README.md for PostgreSQL. Its target
can remain backend/tests/integration for the full regression suite or be narrowed
to backend/tests/integration/test_registration_postgres.py. Credentials are prompted
securely and never saved. The unchanged fixtures enforce inventory_checkout_test /
inventory_checkout_test_login, block the application engine and clean up generated
schemas. No ordinary Alembic commands or development database access are needed.
If dedicated credentials are unavailable, execution is pending; collection is
not evidence of passing PostgreSQL tests.

## Verification recorded for Task 2

- Complete database-free regression suite: 547 passed, 0 failed; the one deliberate
  development database lookup was deselected. SQLAlchemy deprecation warnings
  were treated as errors.
- Syntax: all 51 backend Python files passed. Application import and OpenAPI
  generation passed while application database connections were blocked.
- PostgreSQL: 341 cases collected, including 66 registration cases; none executed
  in this run because dedicated credentials were unavailable. Run the existing
  secure integration runner to verify actual PostgreSQL behavior.
