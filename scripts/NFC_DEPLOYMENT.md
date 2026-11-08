# Staged NFC demonstration deployment

Implementation only: no database has been initialized by adding this runner.
Do not execute it until the live deployment and its administrator access have
been separately approved.

After approval, from the repository root in an interactive terminal:

```
.venv\Scripts\python.exe scripts/deploy_nfc_demo.py --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --schema public --administrator postgres
```

Replace the administrator login with the approved existing superuser. No password
argument is supported. The runner requires the exact typed confirmation
`INITIALIZE inventory_nfc_demo` before creating connections or acquiring locks.
It then prompts invisibly for the migrator password. The administrator password
is requested only when stage 2 is required, and that connection is disposed
immediately after the stage. Passwords are held in process/engine memory only;
Python cannot guarantee erasure of memory copies. Do not run with shell tracing,
a debugger, credential-capturing terminal tooling, or an untrusted Python process.
There is no .env loading, credential file generation, or command-line password.
The CLI suppresses logging and withholds driver exception details. A failed hidden
password prompt aborts rather than falling back to echoed input.

## Stages and reruns

- Empty approved schema: inventory_migrator upgrades to b7d2f90c4e61.
- b7d2f90c4e61: existing superuser upgrades to c4e8a17d92f6.
- c4e8a17d92f6: inventory_migrator upgrades to d6f2b8a931c0.
- d6f2b8a931c0: verify only; no migrations replayed or administrator prompt.
- Other revisions, empty version markers, extra objects, or missing/mismatched
  schema components: abort for administrator review, with no repair or stamp.

Each stage uses command.upgrade with alembic_nfc_demo.ini and a caller-owned
transaction. The environment retains its mandatory identity checks. Stage checks,
migrations, version updates and postchecks share the transaction: failure rolls
back that stage. Previously committed stages remain and can be resumed. The
runner never invokes upgrade functions directly, never changes role memberships
or attributes, and never provisions runtime grants or seed data.

## Connection identity and locking

Every stage checks the actual database, session and effective login, application
schema, effective search path, local server address/port, PostgreSQL >=18.4 within
major version 18, and read-committed isolation. The administrator must already be
a true superuser; inventory_migrator must not be one. Hostnames, remote targets,
protected databases and authentication-role switching are not supported.

Every stage acquires a nonblocking exclusive **transaction-level** PostgreSQL
advisory lock using pg_try_advisory_xact_lock and constant key 0x4E464344454D4F.
It is acquired inside the same connection.begin() transaction and on the same
connection as the revision checks, schema checks, migrations, postchecks and
commit. PostgreSQL releases it only when that transaction ends (commit, rollback,
or backend termination). The runner has no explicit unlock and no guardian
session. Initial and final checkpoint inspections also use this transaction lock.

The key is shared by all stages and identities in the approved database, so two
cooperative runner instances cannot execute migration stages concurrently.
A contender fails closed immediately; it does not read schema state or run
migrations after failing to acquire the lock. Retry after the active stage ends.
This serializes individual stages, not the complete three-stage deployment.
There is intentionally no reservation between transactions. Another instance may
complete a later stage between this runner's stages or during a password prompt.
Every stage revalidates its expected starting checkpoint under the lock. A stale
plan aborts without replaying migrations; rerun to resume from committed state.

All password prompts occur outside connections and transactions. Losing a prior
inspection connection during a prompt cannot invalidate protection of future
work: the administrator must acquire its own transaction lock. If its backend is
terminated during a stage, PostgreSQL aborts that transaction and releases its
lock together. If a network failure is not yet detected by the server, the old
backend can remain active, but still holds its own lock: another cooperative
runner cannot enter until that transaction finishes. It is not protected by an
independent connection whose death could release the lock prematurely.

A client disconnect or lost commit acknowledgement does not prove rollback:
the server may already have committed. The runner does not reconnect or retry
inside a failed stage. After reviewing server health, rerun to verify the actual
committed checkpoint. Earlier committed stages are never rolled back by a later
failure. Future revisions must preserve the enclosing transaction (no manual
commits or autocommit blocks); the current seven revisions are unchanged.

Advisory locking is cooperative. Ordinary Alembic, arbitrary SQL, administrators,
and applications that ignore the key remain outside this guarantee. Stop other
writers and deployment tools. The key is database-wide for this runner and does
not vary by application schema. It is not a cross-database or cross-cluster lock.

## Preconditions and verification limits

The approved schema must already exist, be trusted, and contain only migration
objects. On an empty target it must contain no tables, views, routines, sequences,
or custom types. The runner creates no schema and repairs no privileges. Existing
extensions that install objects into this schema cause rejection. Metadata-audit
roles and runtime restrictions must already satisfy the unchanged migration.
Do not fix a failed preflight by changing shared memberships or role attributes.

Checkpoint checks compare columns/types/nullability, primary keys, unique and
foreign-key column mappings, named check constraints, index names, routine
inventory, and enabled trigger bindings/events. Trigger checks also require no
WHEN predicates (tgqual must be NULL), no arguments or transition tables, no
constraint/deferred/partition-parent properties, and the exact approved UPDATE OF
column restriction (maintenance_hold only for the maintenance asset guard). Post-separation checks reuse the
migration's read-only ACL validators and inspect role/source restrictions.
These checks detect incomplete deployments; they are not a cryptographic proof
of all SQL definitions. Constraint expressions, function bodies, FK actions and
index expressions still rely on trusted administrator-controlled DDL and the
unchanged reviewed revisions. They must not be treated as tamper attestation.

The runner is pinned to these seven revisions. Later revisions need review and
an explicit stage plan. Changes to protected audit objects require the trusted
administrator again; do not transfer them back to inventory_migrator. Runtime
privilege provisioning and synthetic demo data are separate, unimplemented work.

Before live use, validate this procedure on a separate disposable PostgreSQL
18.4 instance, including default ACL compatibility and all ownership transfers.
Database-free tests cannot prove live ownership/ACL behavior. No tool here
connects to inventory_db or inventory_checkout_test.

## Required disposable-PostgreSQL validation before deployment approval

Database-free tests model contention and backend failures and exercise the real
SQLAlchemy transaction context using in-memory SQLite. They do not prove server
locking, backend termination, transactional DDL, or catalog reflection behavior.
On a separately authorized disposable PostgreSQL 18.4 instance, verify:

- Two real connections using different stage identities contend on the same key;
  the second cannot inspect checkpoints or migrate before the first commits.
- Terminate a stage backend mid-DDL: both the DDL/version changes and advisory
  lock disappear with its rollback; a new runner resumes from the prior stage.
- Drop the network and lose commit acknowledgement: a new runner either observes
  contention or verifies the actual committed checkpoint without replay.
- Pause at the administrator prompt while another runner advances the database;
  the stale runner aborts at its locked starting-checkpoint check.
- Reflection accepts all three authentic migration checkpoints. Replace a
  metadata trigger with WHEN(false), a column-restricted trigger, or deferred
  trigger and confirm verification rejects it. Restore only on the disposable DB.
- Exercise ownership transfers, default ACL rejection, successful final ACL
  verification, migration/postcheck failures and their transaction rollbacks.

None of these PostgreSQL integration checks has been executed as part of this
implementation. They require separate authorization and must not use development
or automated-testing databases on the shared server.
