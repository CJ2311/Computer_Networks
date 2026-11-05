# Phase 4A Task 3A: finalized metadata contract and checkout history

Design/test refinement only. Option A (checkout-time equipment names and asset
codes) is approved. No application code, models, PATCH endpoints, database roles,
or migrations are implemented or changed by this document/task.

## Metadata API contract

Proposed PATCH /equipment/{equipment_id} and PATCH /assets/{asset_id}: 200 using
existing entity response snapshots; missing entity 404; invalid/empty body 422;
unauthenticated/invalid authentication 401; inactive/unauthorized staff 403;
uniqueness or workflow-state conflicts 409.

Equipment editable: name, category, manufacturer, model_number, description,
default_location, purchase_price. Immutable: id, tracking_type, created_at.
Asset editable: asset_code, nfc_tag_id, serial_number, location. Immutable/workflow
controlled: id, equipment_id, availability_status, condition_status,
maintenance_hold, created_at. Unknown fields, actor/audit fields, snapshot fields
and quantity are rejected. No stock management or update fan-out is introduced.

Future EquipmentUpdate/AssetUpdate use model_fields_set and exclude_unset=True,
never exclude_none: omission preserves values; explicit null clears nullable
columns only. Required name/category/default_location and asset_code/location
cannot be null/blank. Reject {}. Reuse registration trimming, length limits
(150/100 equipment strings; asset code 50, location 150, NFC/serial 100), nullable
nonnegative Decimal(12,2), optional blank-label/identifier normalization to null.
Description preserves existing rules (trimmed, empty text allowed). Identifier
case/internal spaces and existing uniqueness scopes are preserved: global asset
code/NFC, serial per equipment_id; multiple null identifiers remain allowed.

Normalized no-op: 200, no UPDATE/audit. Mixed requests audit only changed fields.
Any request containing location, even unchanged, requires AVAILABLE + WORKING +
maintenance_hold=false and no inconsistent active borrowing/maintenance. Otherwise
409. Other metadata corrections may occur while borrowed/held/reserved/retired;
workflow state and original borrowing/maintenance records remain untouched.
Updating default_location never relocates existing assets.

## Approved Option A: checkout-time identifiers

Confirmed current implementation: EquipmentCheckout has only the asset FK and no
label snapshots. create_checkout locks the asset but not its parent equipment.
Borrowing-history SQL joins Equipment.name and EquipmentAsset.asset_code, so it
currently displays live labels. That projection must change before metadata PATCH
is exposed; reconstructing names from later audit events is not Option A.

Future additive checkout columns (proposal only):

| Column | Meaning |
| --- | --- |
| equipment_name_at_checkout | VARCHAR(150), exact stored name captured for this loan |
| asset_code_at_checkout | VARCHAR(50), exact stored code captured for this loan |
| identifier_snapshot_status | NOT NULL: CAPTURED or LEGACY_UNKNOWN |

CAPTURED requires both strings non-null. LEGACY_UNKNOWN requires both null.
Preserve exact stored spelling, case and whitespace, including legacy source
labels; snapshot capture must not silently normalize source data or add new
checkout eligibility rules. Stable asset UUIDs remain authoritative when codes
are later reused. equipment_id remains non-editable; no reparenting is introduced.

The owned checkout transaction locks/refetches the asset FOR UPDATE, then its
parent equipment FOR SHARE (FOR UPDATE is acceptable but more restrictive),
before active-checkout/maintenance locks. Both locks survive through commit.
FOR KEY SHARE is insufficient to protect a non-key name change. Read database
time only after locks. A BEFORE INSERT checkout trigger derives the two labels
from those exact trusted rows under the same locks, marks CAPTURED, and rejects
non-null forged labels or a caller-requested legacy status. API input schemas
must reject all snapshot fields. The capture trigger is SECURITY INVOKER and uses
schema-qualified objects; it needs no elevated audit-insert privilege. It never
trusts caller-provided label strings. This is the ordinary current-time checkout
path, not a backdated-import API. Current checked_out_at/return/deadline semantics
must otherwise remain intact.

A separate BEFORE UPDATE guard rejects changes to the three snapshot columns
with 55000. Returns may still change their existing return fields/notes without
changing snapshots. No new metadata event is generated for checkout capture.
Return and checkout POST response field sets need not expand for this change.

Borrowing list/detail keep asset_code/equipment_name keys but project only the
stored snapshot columns, never live labels or COALESCE(current_label). Add
identifier_snapshot_status. Entity IDs, student SQL ownership restrictions,
authorized filters, ordering, pagination and database-clock overdue rules remain
unchanged. A later checkout captures then-current labels; an earlier active or
returned loan retains its own originals across renames, returns and reborrowing.

Legacy rows without reliable original labels become LEGACY_UNKNOWN with both
labels null. Do not backfill current names as historical facts or change the
original loan fields. History response label fields must therefore become nullable
with explicit provenance; clients must handle unknown labels without substituting
current ones. Old labels cannot be recovered without independent evidence. Any
later reconciliation is a separately approved privileged operation, not PATCH or
return behavior. This is a necessary API compatibility change, not implemented now.

## Ownership and compatible lock ordering

Only active ADMIN/FACULTY/LAB_ASSISTANT; never trust caller role attributes.
Persisted authorization is checked in the owned write transaction before and
after waits. Reuse the fresh write Session dependency, separate from auth reads.
Reject caller roots/SAVEPOINTs/failed Sessions/pending work/external Connection
bindings without changing caller transactions. Do not lock users before entities.
Proposed services retain update_equipment(db, equipment_id, update_data, actor)
and update_asset(db, asset_id, update_data, actor).

| Operation | Row lock order |
| --- | --- |
| Equipment metadata update | equipment FOR UPDATE only; never lock assets/loans/requests |
| Asset metadata update | asset -> active checkout -> active maintenance request |
| Checkout with Option A | asset -> equipment FOR SHARE -> active checkout -> maintenance request |
| Return / maintenance | existing asset -> checkout -> maintenance request, unchanged |

No path may lock equipment then wait for an asset. Snapshot triggers re-lock only
already held rows; audit triggers do not lock sibling entities. Location decisions
use refreshed post-lock state. Checkout first means relocation 409; relocation
first may commit before checkout. Maintenance first blocks relocation; relocation
first may precede the hold. Names/codes may be corrected after checkout, but its
snapshots never change. Shared parent reads allow checkouts of different assets;
name updates wait for those reads. Same-entity PATCHes serialize; disjoint fields
survive, and same-field updates use last successful writer wins (no stale-client
ETag/version contract yet). All lock failures roll back the owned transaction;
known deadlocks/timeouts/serialization failures remain sanitized retryable 409.
READ COMMITTED is the baseline; stronger isolation may legitimately require retry.

Plan changes before dirtying ORM objects. Set transaction-local actor context,
guarded scalar entity flush (including server audit) -> response validation ->
owned commit. Keep all Phase 3 flush and root-identity guards. No nested commit,
second internal write Session, selective flush, or post-commit refresh. Unknown
commit acknowledgement: sanitized 503, check records/audit before retry; never
promise rollback or automatically retry. Discard Sessions after cleanup failures.
Commit and HTTP delivery are not atomic.

## Final audit table and generation design

Task 3B-2 implements metadata_audit_events / MetadataAuditEvent:

| Column | Constraint |
| --- | --- |
| id | database-generated UUID primary key |
| actor_id | NOT NULL UUID FK users.id ON DELETE RESTRICT |
| entity_type / entity_id | NOT NULL EQUIPMENT or ASSET and stable UUID |
| equipment_id / asset_id | Typed RESTRICT FKs; exactly one non-null, matches type and entity_id |
| action | NOT NULL UPDATE |
| previous_values / new_values | NOT NULL JSONB objects; same nonempty changed keys; values differ |
| occurred_at | NOT NULL timestamptz, database clock_timestamp() |
| entity_revision | bigint >=1; unique(type, entity_id, revision) |

Only changed allowlisted metadata, JSON null for SQL null, exact two-decimal price
strings, no profiles/credentials/workflow or checkout snapshots. Allocate revision
max+1 while the entity UPDATE row lock is held; use the unique revision index for
history ordering despite clock ties/changes, plus an actor/time/id index.

AFTER ROW UPDATE generators metadata_audit_equipment_change() and
metadata_audit_asset_change() compare allowlisted OLD/NEW values, recompute diff
and insert one event in the same statement/transaction. Errors abort both. Skip
registration INSERT, no-op, and workflow-only changes *before* demanding actor
context; checkout/return/maintenance therefore continue without metadata context.
Generators validate TG_OP/TG_WHEN/TG_LEVEL and exact TG_RELID against trusted,
migration-bound relation OIDs. Names, TG_TABLE_SCHEMA, or trigger depth alone
cannot authorize a source. Do not follow caller search_path or row-supplied names.

## Actor attribution and privileges

The service derives actor UUID from authenticated identity, re-reads eligibility
after entity locks, and always overwrites transaction-local
inventory.metadata_actor_id via set_config(..., true) before metadata writes.
The generator reads current_setting(..., true), treats missing/empty/malformed,
unknown, inactive, or non-staff actors as 42501, and checks the real qualified users
table. The FK preserves that actor reference. API actor/diff/time/revision fields
are forbidden. Context rolls back/clears at transaction end and is not reused.

A custom GUC is an application assertion, NOT an unforgeable identity credential.
The shared runtime login can set GUCs; a compromised backend/runtime credential
can impersonate another eligible actor for a real metadata update. This design
trusts the authenticated server and does not claim protection from that compromise.
No new login/authentication mechanism or secret is introduced in this task.

Production roles (provisioned separately by a deployment administrator):

| Role | Allowed / prohibited |
| --- | --- |
| inventory_metadata_audit_owner (NOLOGIN) | Owns audit table/protection functions; runtime cannot assume it |
| inventory_metadata_audit_writer (NOLOGIN) | Owns only the two SECURITY DEFINER generators; narrow SELECT on audit/eligibility data, INSERT on audit, schema USAGE; no table ownership, UPDATE/DELETE/TRUNCATE, DDL or role administration |
| Existing runtime login | Existing entity/workflow access plus required SELECT/UPDATE for row locking; audit SELECT only; no audit INSERT/UPDATE/DELETE/TRUNCATE/TRIGGER, generator EXECUTE, trusted-schema CREATE, ownership or privileged-role membership |
| Deployment administrator | Provisions/verifies owners and grants separately; never used for application requests |

Generator execution switches current_user to the restricted writer role. The audit
BEFORE INSERT guard is SECURITY INVOKER: require the catalog-verified generator
owner identity plus the expected nested trigger context. Do not make this guard
SECURITY DEFINER, which would erase the caller-identity check. Depth is a secondary structural check and does not identify the calling
trigger. Production authorization rests on the verified ACL and role boundary. No writable GUC flag can enable insertion.
The generators' fixed source-OID checks and revocation of EXECUTE/TRIGGER/DDL stop
ordinary runtime callers reattaching them to lookalike/temp tables. Direct INSERT
under an ordinary identity is rejected with 42501; audit UPDATE/DELETE/TRUNCATE
always raise 55000. Deny fabricated history as well as modifications to real rows.

Use SET search_path = pg_catalog, pg_temp, putting temp explicitly last, and fully
qualify every application object/type/function with trusted schema names captured
by deployment. Any unavoidable dynamic identifiers are safely quoted and values
bound, never sourced from the request. Restrict function privileges in the same
DDL transaction that creates them: revoke default PUBLIC EXECUTE, preserve only
trusted deployment/owner access, and review inherited/default grants. No trusted
object/schema may be writable by runtime. Owners must be NOLOGIN, non-superuser,
non-BYPASSRLS/non-replication, without runtime membership/SET ROLE routes. A trusted
owner/superuser can alter or disable protections; this is outside the guarantee.

## Future migration, rollout and verification boundary

Snapshot revision **b7d2f90c4e61** follows **a3c7e91b6d20**. Audit revision
**c4e8a17d92f6** follows the snapshot revision and adds only audit infrastructure.
Existing rows are untouched. The audit downgrade refuses populated history under
exclusive locks. No retrospective metadata-audit backfill.

Do not expose mutable names/codes until snapshot capture, immutable snapshot
storage and history projection are deployed together. Coordinate/quiesce checkout
and metadata writes during cutover so the old unlocked parent-name read cannot
race an enabled metadata updater. Provision role separation only with separate
approval; do not change credentials, runtime grants or existing test-role checks
as a shortcut. Existing maintenance verification and audit constraints stay intact.

The strict isolated test login owns generated schemas and has no role memberships.
It can test DML guards with its privileged single-owner profile but CANNOT prove
production owner/writer/runtime separation or assign owners it cannot assume.
Production ACL catalog checks require the separately provisioned configuration
fixture metadata_audit_runtime_roles; remain pending until explicitly approved.
No test creates roles or alternate credential connections. A future pre-upgrade
fixture checkout_before_snapshot_migration must delegate the existing identity-
verified test_database and unique-schema machinery, seed loans before the new
revision, then apply it; never disable guards to fake legacy rows. Its Engine URL
must match the already verified dedicated URL before connection.

The Task 3B-3 PATCH components are explicitly pending; future complete components with missing snapshot/audit tables
fail setup. Database-free tests include existing immutable ReturnCreate checks;
future snapshot/history/ACL/concurrency cases remain pending. PostgreSQL tests use
only the existing safe runner and inventory_checkout_test isolation. The Task 3A design refinement did not run a database or migration. Existing registration/Phase 3 tests
are not weakened; a future implementation must deliberately update schema-shape
expectations for the added snapshot constraint/history provenance field.

References used to check the design: PostgreSQL 18 [definer-function safety](https://www.postgresql.org/docs/18/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY),
[trigger execution/atomicity](https://www.postgresql.org/docs/18/trigger-definition.html),
[trigger creation privileges](https://www.postgresql.org/docs/18/sql-createtrigger.html),
and [row lock conflicts](https://www.postgresql.org/docs/18/explicit-locking.html).

## Verification for this design refinement

Executed database-free suite: **563 passed, 57 skipped, 1 deselected** (10.35s).
Within the metadata contract module: **16 passed, 57 skipped**. Skips explicitly
represent unimplemented update/snapshot components, not verified behavior. The
deselected existing development-user lookup would access the development database.
Application Engine connect/raw_connection were blocked during execution; SQLAlchemy
deprecation warnings were treated as errors.

All three test/helper Python files passed syntax compilation. The metadata
PostgreSQL module collected **120 cases**; **none were executed**. Role-separation,
snapshot migration, trigger security, and concurrency guarantees remain proposals
until their dedicated PostgreSQL tests pass. No database was accessed, no migration
was run, and no production application file was modified.

## Task 3B-1 implementation boundary

Task 3B-1 splits the earlier combined migration proposal: snapshot-only revision
b7d2f90c4e61 follows a3c7e91b6d20. Task 3B-2 adds audit foundation in a later revision. PATCH remains pending. Snapshot capture/immutability, checkout parent locking,
and snapshot-only history projection are now implemented in source. No migration
has been executed by this task. The historical Task 3A verification above remains
a record of that design-only run, not evidence of current PostgreSQL verification.

The insert trigger is SECURITY INVOKER with trusted schema-qualified source
relations and pg_catalog, pg_temp search_path. Runtime needs SELECT and row-lock
UPDATE privileges on assets/equipment; trigger creation remains deployment-owned.
Runtime must not own tables/functions or have ALTER/DDL/trigger-disable privileges.
No roles or grants are provisioned here. Owners/superusers can bypass guards and
remain outside the protection boundary.

Deployment must coordinate checkout writes while upgrading, then deploy the new
history response contract: nullable asset_code/equipment_name and required
identifier_snapshot_status. All existing loans are classified LEGACY_UNKNOWN;
there is no reliable historical evidence source in this repository for backfill.
Downgrade takes an exclusive lock and refuses any populated checkout table.

Task 3B-1 verification: database-free regressions **572 passed, 0 failed,
51 skipped, 1 deselected** (8.52s). Skips are the still-unimplemented metadata
update contract; the deselected development-user lookup was excluded deliberately.
FastAPI import passed with development Engine connection methods blocked.
Integration collection: **477 cases**, including **16 snapshot-specific cases**;
all unexecuted because dedicated credentials were unavailable. No migration was
executed and neither development nor test databases were accessed.


## Task 3B-2 deployment and trust boundary

The JSONB diff keys are the changed field names. The generators compare typed
OLD/NEW values and record the exact stored values, including JSON null and
NUMERIC(12,2) rendered as a string. Normalizing future PATCH inputs is the
service's responsibility. A source row lock serializes revision allocation. The
qualified source relation is resolved to its OID before OLD/NEW data is used,
which rejects triggers attached to lookalike tables while allowing dump/restore
to assign new OIDs.

Apply this online revision in one transaction through the approved deployment
path with an administrator authorized to transfer object ownership. The revision
requires preprovisioned, separate NOLOGIN owner/writer roles and the existing
inventory_app login. These three roles must have no memberships or elevated
attributes. The runtime cannot own the database or the source tables, create in
the application schema, attach triggers to the sources, or modify the trusted
users table. The writer cannot own the database or create in the application
schema. The deployment administrator must maintain these restrictions, keep
staff identity provisioning separate, and review ambient grants, role changes,
and executable privileged functions. The current Alembic environment targets
the development database and is not an approved deployment path for this task.

Only the two generators execute as the audit writer. The migration grants the
writer eligibility-column reads on users, INSERT on audit history, and the
three audit-column reads needed to allocate revisions. The runtime receives
audit SELECT. Audit privileges granted through PUBLIC are revoked, and unsafe
effective runtime or writer grants on audit history abort the migration. The
migration grants schema USAGE to the two NOLOGIN roles. The empty downgrade
leaves the source/schema grants in place for administrator review.

The verified integration fixture opts into a single-owner layout only for the
dedicated database, test login, and generated schema. That layout tests guards
and PUBLIC revocations but cannot prove production privilege separation. The
custom GUC is still a server assertion. A SQL client with the shared runtime
credential can set another eligible staff UUID and perform a real metadata edit.
PostgreSQL validates the persisted role and active flag visible to the trigger.
Privileged administrators and object owners remain trusted. No PATCH endpoint
or production actor-setting code is introduced in this phase.


Production preflight also rejects incoming memberships into either dedicated
audit role, including INHERIT-only grants. Apply with a deployment superuser
because transferring ownership must not require a standing membership or schema
CREATE grant for the writer. After transfer, the migration verifies explicit
table, column, and function ACLs and effective runtime/writer permissions. No
third party may attach audit triggers or execute the audit functions. The writer
must own no other SECURITY DEFINER routines in the database. These checks run
at migration time. Subsequent ownership, grant, role, and trigger changes remain
the responsibility of trusted administrators.
