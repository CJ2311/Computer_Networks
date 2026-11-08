# Backend deployment and migration safety

No command here was executed against host PostgreSQL. `inventory_db`, `inventory_checkout_test` and host
`inventory_nfc_demo` must never be reset or used for destructive tests. Application migrations require separate approval
before real deployment. Use the disposable harness to validate first; never run the legacy host integration suite here.

## Migration architecture

The original seven revisions in `backend/alembic/versions/` are unchanged. The original `alembic_nfc_demo.ini` and staged
runner remain pinned to those revisions and their twelve baseline tables. Their database/identity/lock/ownership guards
remain active. A fixed baseline table allowlist prevents new model registration from silently expanding checkpoints.

New revision `e8a4c2d190ab`, descending from `d6f2b8a931c0`, lives in `backend/alembic/application_versions/`.
Ordinary Alembic now discovers both directories. `alembic_nfc_application.ini` uses the same strict dedicated NFC
environment and both version directories. It requires an external connection, online identity configuration and exact
NFC database/session/effective-user/schema/search-path validation; it has no ordinary-engine or credential fallback.

The revision adds external identity mappings, lending policies, explicit individual loan destinations, quantity loans,
stock ledger and overdue-reminder tables. It takes ACCESS EXCLUSIVE on stock before recording opening balances and
installing capture, preserving a consistent opening snapshot. History capture has protected NOLOGIN ownership and
fixed search paths. It records every stock mutation, rejects missing operation context, and denies history rewrites,
source deletion/truncation and inconsistent quantity custody. No role memberships or attributes are changed.

A trusted existing deployment administrator is required for ownership separation, not a superuser grant to the
migrator. The extension checks that administrator authority exists, protected roles remain isolated and runtime schema
CREATE is absent. New tables, stock and new functions are owned by the existing protected owner. Runtime gets only
needed SELECT/INSERT/UPDATE privileges, SELECT-only identities/ledger, and no history ownership, trigger control,
DELETE or TRUNCATE. Metadata-audit writer ownership/privilege validation remains unchanged. Runtime core table grants
are applied explicitly; users remain SELECT-only. Unknown default ACLs make extension verification roll back.

## Approved NFC initialization sequence (later, after approval)

1. Inspect secrets/configuration and take any required backups. Set the application explicitly to the approved target;
   do not rely on `inventory_db` fallback. Stop or drain old application writers for the upgrade.
2. Use the existing interactive `scripts/deploy_nfc_demo.py` process to initialize an empty approved database through
   `d6f2b8a931c0`. See `scripts/NFC_DEPLOYMENT.md`. Do not use it to replay a database already on the extension revision.
3. Apply the extension using an interactive terminal:

   ```text
   python scripts/deploy_nfc_application.py --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --administrator <trusted-login> --schema public
   ```

   The runner requires typed `EXTEND inventory_nfc_demo`, a hidden administrator password and the exact completed
   baseline or extension revision. Passwords never appear in CLI arguments or URLs. It verifies the actual connection,
   obtains the existing transaction advisory lock before checkpoints/DDL, invokes Alembic normally, then verifies
   identities, ownership, ACLs, trigger predicates/properties/functions and revision before committing. Failures roll
   back the migration and marker together. Re-running at the exact extension revision verifies without replay.
4. Provision local users and verified issuer/subject mappings through a trusted administrative workflow, not runtime
   role grants. Configure college auth, CORS and explicit application database name/password using deployment secrets.
5. Verify the application as inventory_app, then enable traffic. Never grant ALL to work around a preflight failure.

The original NFC runner deliberately rejects the extension revision; use the extension runner for its verification.
Transaction advisory locks coordinate cooperative migration runners only; they do not fence arbitrary administrator
SQL, guarantee multi-stage exclusivity during password entry, or protect against infrastructure administrator tampering.
Each stage/extension holds its own lock on the same transaction that commits its DDL. Lost connections roll back their
own transaction. After ambiguous commits, reconnect, check actual revision/catalog and rerun the appropriate verifier.

The extension is intentionally not destructively reversible: downgrade raises an error. Use a separately reviewed
forward migration or restore plan. Future changes to protected stock/history objects need the trusted administrator.
Actor attribution trusts application-supplied transaction context, as in the existing metadata audit. A compromised
runtime credential can forge new operations/actor assertions, but cannot rewrite protected ledger history. Keep database
access private and rotate credentials. Other production database names need their own explicitly approved deployment identity procedure; do not weaken NFC
restrictions to accommodate them. Ordinary Alembic retains its existing behavior, including its development default.

## Disposable integration testing

See `infra/nfc_postgres/README.md`. Use a fresh unique Compose project and explicit `NFC_DISPOSABLE_OPT_IN=YES_ISOLATED_PG18`.
PostgreSQL is 18.4 with network_mode none; the nonroot worker shares only that isolated loopback namespace. No host
ports, bind-mounted credentials/data, existing .env files or Docker socket are exposed. Dependencies install at build.
The worker runs original NFC and application tests using generated instance-local roles/credentials and identity checks
before resets. Only managed tmpfs data/identity storage is used. Check exact Compose/harness labels before cleanup.
Do not use `backend/tests/integration` against the protected host test database.

## Overdue email reminders

Disabled unless `OVERDUE_EMAIL_ENABLED=true`. Configure `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_FROM`,
`SMTP_TLS_MODE` (`starttls` or `implicit`), and optional paired `SMTP_USERNAME`/`SMTP_PASSWORD` through secrets.
TLS certificate verification is mandatory; no plaintext mode exists. SMTP/API logs must redact credentials and tokens.
After approved deployment, an external scheduler may invoke:

```text
python -m scripts.send_overdue_reminders --database inventory_nfc_demo --limit 100
```

The environment must explicitly set matching `INVENTORY_DATABASE_NAME` and application credentials. The command checks
actual database, session/effective user, schema and effective search path; it rejects protected dev/test names. It never
runs at application import/startup. No actual emails are sent by automated tests.

One durable reminder is queued per loan. Enqueue is advisory-lock serialized; claims use row locks/SKIP LOCKED. A claim
commits UNCERTAIN before the SMTP attempt outside database transactions. Acknowledged sends become SENT. Crashes,
timeouts, recipient refusal or lost acknowledgements leave UNCERTAIN and are not automatically replayed. Operators
must reconcile mail-server records before any approved manual requeue. This prevents automatic duplicates at the cost
of potentially missed reminders; SMTP cannot provide exactly-once delivery. A loan may be returned after claim and
before delivery, so messages ask students to contact staff if already returned. Provider acceptance is not proof of
inbox delivery. Monitor pending/uncertain counts and schedule the worker externally.
