# NFC inventory audits

Revision d6f2b8a931c0 follows c4e8a17d92f6. This module audits individually
tracked assets. Quantity stock is excluded.

## Endpoints and permissions

| Endpoint | Active roles |
| --- | --- |
| POST /inventory-audits | ADMIN, FACULTY, LAB_ASSISTANT |
| GET /inventory-audits | ADMIN, FACULTY, LAB_ASSISTANT |
| GET /inventory-audits/{id} | ADMIN, FACULTY, LAB_ASSISTANT |
| POST /inventory-audits/{id}/scans | ADMIN, FACULTY, LAB_ASSISTANT |
| GET /inventory-audits/{id}/scans | ADMIN, FACULTY, LAB_ASSISTANT |
| GET /inventory-audits/{id}/expected-assets | ADMIN, FACULTY, LAB_ASSISTANT |
| GET /inventory-audits/{id}/report | ADMIN, FACULTY, LAB_ASSISTANT |
| POST /inventory-audits/{id}/finalize | FACULTY |
| POST /inventory-audits/{id}/cancel | ADMIN, FACULTY |

Students have no audit access. Services revalidate persisted staff activation
and role after audit lock waits. Authentication uses the existing dependencies.
Staff visibility is laboratory-wide, consistent with borrowing history.
Assigned-lab permissions remain future work.

Creation accepts name, location, and optional notes. Location matching is exact
and case-sensitive after request whitespace trimming. A structured location
registry would require a separate design. Reads accept limit 1–100 and
nonnegative offset with deterministic ordering. The list also filters by
status and location.

## Snapshot and scan semantics

One PostgreSQL statement joins all individually tracked assets with equipment
names, active checkout IDs, and active maintenance IDs/versions. It captures
one MVCC view with a database statement timestamp. All individually tracked
identities are frozen so out-of-location equipment can be recognized. Only
assets at the audit location contribute to expected counts. Snapshot rows
retain identifiers, condition, availability, maintenance hold, and custody
references. The audit and snapshots are inserted atomically and then sealed.
Creation returns version 2.

NFC resolution uses this snapshot throughout the audit. A newly registered or
newly assigned tag is UNKNOWN_TAG. Changes to a snapshotted asset are reported
at reconciliation. Legacy source identifiers are preserved exactly. Submitted
tags follow registration whitespace trimming, so legacy whitespace identifiers
may require separately authorized metadata correction before starting an audit.

A scan accepts a UUID idempotency_key, nfc_tag_id, and observed_location.
Each physical observation gets a new key. Network retries reuse their key.
The key is unique per audit. A replay requires the same authenticated actor and
normalized payload and returns the original immutable scan, even after closure.
Different payload/actor reuse returns 409. Scan responses are 200 for both
new observations and replays.

| Result | Meaning |
| --- | --- |
| MATCHED | An expected asset observed at its snapshotted location |
| MISPLACED | An expected asset observed elsewhere |
| UNEXPECTED | A known snapshotted asset belongs outside the audit location |
| UNKNOWN_TAG | A tag absent from the frozen individually tracked inventory |
| DUPLICATE | A new physical observation of an already observed tag |

Duplicate detection includes unknown tags. Duplicates retain their own
location, actor, timestamp, and reference to the first observation. The first
observation determines the base finding. A later corrective scan never silently
replaces it. The API rejects client classifications and actor overrides.
NFC identifies equipment and grants no authorization. Hardware attestation,
device authentication, and offline queue synchronization remain outside this API.

## Reconciliation and finalization

An open report previews findings without persisting them. Its version is the
concurrency token for finalization/cancellation. New physical scans increment
that version; network replays do not.

Unobserved expected assets yield NOT_VERIFIED, EXPECTED_ON_LOAN, or
EXPECTED_MAINTENANCE using snapshot custody. An observed asset with current
checkout/maintenance restrictions yields CUSTODY_REVIEW. Changes after the
snapshot yield STATE_CHANGED with snapshot and observed values. One live
inventory statement compares identifiers, custody IDs, and maintenance versions
so returns, reborrowing, and maintenance progress can be distinguished.

Faculty finalization accepts expected_version and optional notes. It persists
findings and freezes the audit. A stale version returns 409 and requires
reloading the report. Faculty may acknowledge findings that still need follow-up.
Final notes provide context. Structured discrepancy resolution would require
a future append-only workflow.

Reconciliation records inventory at reconciled_at. Checkout/maintenance can
continue after that observation, including before commit. The report records
an explicitly timed observation. Closed reports use persisted findings and
never substitute current equipment labels or custody data.

Cancellation requires an expected version and nonblank reason. It retains the
snapshot and observations, actor, and database timestamp. Cancelled reports
set reconciliation_complete=false and do not imply unscanned assets were
verified. Audits never mark assets permanently missing, relocate equipment,
change borrowing records, or alter operational condition/availability.

## Transactions and database safeguards

Writes use the existing fresh-session owned transaction wrapper and guarded
scalar flushes. Responses are validated before commit. Parent audit row locks
serialize scans, retries, findings, and closure. Reports use a shared parent
lock to stabilize audit observations. Operational inventory queries take no
asset/equipment/custody locks. Foreign-key locks may wait for operational
transactions, which never depend on an audit lock.

Known deadlocks/timeouts use the existing sanitized retryable 409 handling.
Uncertain commits return 503 and require checking existing records before
retrying. Audit creation has no idempotency key; check the list before retrying
an uncertain creation.

Unique constraints protect snapshot identities/tags, retry keys, first physical
observations, and findings. Composite foreign keys and triggers enforce correct
audit/asset/scan membership, including null unknown-tag references. Triggers
validate classification and staff roles, seal snapshots, require reconciliation
before completion, and block child updates/deletes/truncation. Closed parent
records cannot change. Downgrade refuses to discard populated audit history.

Trigger functions are SECURITY INVOKER, use pg_catalog, pg_temp search paths,
and qualify application relations. No new roles are created. The migration
removes PUBLIC access to the new tables and grants the existing runtime role
SELECT/INSERT, with UPDATE only on the parent. It rejects unexpected default
grants and runtime ownership. Runtime must retain no schema CREATE, privileged
memberships, table ownership, or ability to disable triggers.

The isolated test exception verifies the dedicated database, login, and
generated schema before retaining the single-owner test layout. That layout
cannot prove production role separation.

The authenticated backend, object owners, and deployment administrators remain
trusted. A compromised runtime credential can create open observations as an
eligible staff member. These checks do not establish hardware or human identity.
Frontend rendering must escape user-entered notes. No credentials are stored
in these audit tables.

## Testing and deployment

Database-free tests cover validation, classification, findings, transaction
ownership, authorization, SQL generation, and OpenAPI. PostgreSQL tests cover
lifecycle, snapshots, drift, retries, constraints, concurrency, rollback,
uncertain commit, and immutable reports. The shared fixture applies the new
revision only in its identity-verified, generated test schema.

The maintenance foundation downgrade test first removes the empty dependent
audit foundation, then restores it after exercising the older revision. This
accounts for new snapshot foreign keys to maintenance history.

Use scripts/run_integration_tests.ps1 for PostgreSQL verification. Missing
dedicated credentials leave execution pending. Ordinary Alembic commands remain
prohibited because env.py targets inventory_db. Production deployment requires
separate migration approval and privilege review.

Snapshot creation and reconciliation materialize the individually tracked
catalogue in memory and flush one row at a time to preserve transaction guards.
Responses are paginated. Larger inventories may require a separately reviewed
streaming/bulk persistence design.
