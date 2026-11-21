# Backend deployment and migration safety

No command here was executed against host PostgreSQL. `inventory_db`, `inventory_checkout_test` and host
`inventory_nfc_demo` must never be reset or used for destructive tests. Application migrations require separate approval
before real deployment. Use the disposable harness to validate first; never run the legacy host integration suite here.

## Migration architecture

The current application head is **`f6c1a8d420be`**. The final isolated verification
reported by the project owner passed **312 tests, 0 failed, 0 skipped, exit code 0**,
including fresh migrations and populated upgrades. Disposable cleanup succeeded;
protected host databases were untouched. No database commands were run for handover.
See [BACKEND_HANDOVER.md](BACKEND_HANDOVER.md) for local setup, startup, configuration,
architecture and operational responsibilities.

The original seven baseline revisions in `backend/alembic/versions/` are unchanged; the directory also contains
the additive student NFC revision `e4b7c9d210af`. The original `alembic_nfc_demo.ini` and staged
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

   The target is now `f6c1a8d420be`, reached after operations, missing incidents,
   borrower registration, scanner ingestion, history guards and NFC merge
   `a9c2e4f610bd`. The final forward-only revision updates the NFC routine to sample
   the database clock after row-lock waits, preventing expired challenge activation.
   The runner's accepted prior checkpoints are explicit; an arbitrary intermediate
   revision is not permission to bypass its guards. The demo runner remains pinned
   to `d6f2b8a931c0` and must not be replayed on an extended database.
4. Provision local users and verified issuer/subject mappings through a trusted administrative workflow, not runtime
   role grants. Configure college auth, CORS and explicit application database connection settings using deployment secrets:
   - `INVENTORY_DB_HOST`: PostgreSQL server host (defaults to `localhost`).
   - `INVENTORY_DB_PORT`: PostgreSQL port (integer 1–65535, defaults to `5432`).
   - `INVENTORY_DATABASE_NAME`: Target database name (defaults to `inventory_db`).
   - `INVENTORY_APP_PASSWORD`: Runtime password for the `inventory_app` PostgreSQL user.
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

Revision `f1a6b8c920de` adds checkout and missing-incident history guards after
`c7d3e91024ab`. The extension runner also accepts predecessor `e8a4c2d190ab` and the pinned
NFC baseline. Installation locks both affected tables against concurrent writes, installs
the guards in the migration transaction, and preserves existing rows and grants. Plan a
write-maintenance window. Downgrade is rejected.

Checkout identity, issue/deadline timestamps, snapshots and original notes are immutable.
The single return transition appends the existing return-note entry. Missing reports are
immutable and can be reconciled once with the next version. Terminal rows are immutable.
Deployment verification checks both new triggers, protected function ownership/ACLs, and
the exact installed function bodies. Existing custody and audit triggers remain active.

See `infra/nfc_postgres/README.md`. Use a fresh unique Compose project and explicit `NFC_DISPOSABLE_OPT_IN=YES_ISOLATED_PG18`.
PostgreSQL is 18.4 with network_mode none; the nonroot worker shares only that isolated loopback namespace. No host
ports, bind-mounted credentials/data, existing .env files or Docker socket are exposed. Dependencies install at build.
The worker runs original NFC and application tests using generated instance-local roles/credentials and identity checks
before resets. Only managed tmpfs data/identity storage is used. Check exact Compose/harness labels before cleanup.
Do not use `backend/tests/integration` against the protected host test database.

## HTTP request and input limits

`INVENTORY_MAX_REQUEST_BODY_BYTES` sets the maximum HTTP body size in bytes. It defaults
to 8,388,608 bytes (8 MiB), allowing 100 equipment records with maximum-length descriptions
and JSON-escaped Unicode text. Set a positive decimal integer through deployment
configuration and restart API workers to apply it. Invalid values fail startup.

The ASGI limiter counts actual received bytes before API routing, authentication database
queries, JSON parsing or writes. It returns `413 {"detail":"Request body too large"}`
when the limit is exceeded, including for chunked bodies, absent Content-Length, or an
understated length. A declared size above the limit is rejected without reading the body.
Malformed or duplicate Content-Length headers return 400. Accepted JSON bodies are buffered
within the limit and replayed to FastAPI. Size enforcement runs before CORS preflight
handling. Rejections include Access-Control-Allow-Origin and Vary for explicitly allowed
origins, so browser clients can read the error. Normal preflight handling follows CORS policy.

Equipment descriptions accept up to 4,000 Unicode characters for creation, bulk creation
and metadata updates. Report category and search filters accept up to 100 characters.
Exceeding a field limit returns 422 before the service writes. Historical response text
continues to be readable. Existing limits on 100-record batches, checkout/return notes,
maintenance notes and NFC identifiers continue to apply.

Configure the reverse proxy with matching body limits, request/header/read timeouts,
connection-rate limits and slow-client controls. Body accounting uses the bytes delivered
by ASGI.

## College introspection admission controls

In `AUTH_MODE=college_introspection`, `/auth/me` and all protected inventory endpoints
share the same introspection guard. Existing TLS, issuer/audience/expiry/scope validation,
local identity provisioning and local role checks still apply. No successful or failed
token results are cached, and no bearer tokens or token digests are retained by the guard.

| Setting | Default | Meaning |
| --- | --- | --- |
| `COLLEGE_INTROSPECTION_TIMEOUT_SECONDS` | 5 | Positive socket connect/read timeout, at most 60 seconds |
| `COLLEGE_INTROSPECTION_MAX_CONCURRENT` | 8 | Maximum in-flight provider calls per worker; no waiting queue |
| `COLLEGE_INTROSPECTION_GLOBAL_PER_MINUTE` | 600 | Shared token-bucket capacity and refill per minute |
| `COLLEGE_INTROSPECTION_PEER_PER_MINUTE` | 60 | Per-peer token-bucket capacity and refill per minute |
| `COLLEGE_INTROSPECTION_MAX_PEERS` | 1024 | Maximum tracked peer buckets per worker |

All integer settings must be between 1 and 2,147,483,647. Invalid configuration fails closed with 503 and
no provider call. Settings load on first college authentication attempt; restart workers
after changes. Each bucket starts full and refills continuously, allowing bursts up to
its configured capacity. Every attempt counts before bearer parsing, including missing,
malformed, inactive and rotated tokens. Throttled peers cannot contact the provider.
429 responses include `Retry-After`; concurrency saturation returns 503 with a short
`Retry-After`. Provider errors/timeouts return a generic 503 without credentials or
network diagnostics. Invalid credentials retain 401; unprovisioned/inactive users retain
403. Failed calls always release concurrency slots, and no calls are automatically retried.

Peer buckets use the ASGI connection client IP, never forwarding headers read by this
code. Non-IP/missing peers share one bucket. Configure Uvicorn/proxy forwarding trust
only for controlled proxy addresses: upstream rewriting of the ASGI client address is
outside this guard. Clients behind a shared NAT/proxy can share one peer budget. The
peer map rejects new peers with 429 when full; it removes only buckets idle for at least
60 seconds (fully refilled), so eviction cannot reset an active attacker's quota.

These controls are process-local, not distributed. Multiple workers/replicas multiply
aggregate provider concurrency and request budgets; tune limits against the college's
provider quota and expected authenticated API traffic (each protected request introspects).
The shared budget protects against rotating source IPs, but abusive traffic can exhaust
capacity for legitimate users. Use trusted ingress rate limits for campus-wide fairness
and protection before application worker threads are occupied. The urllib timeout is a
socket-operation timeout, not a total wall-clock deadline: system DNS resolution and a
slow trickling response may take longer. Bound DNS/network behavior and total upstream
request duration at deployment. The 16 KiB provider-response cap remains in place.

## Borrower registration migration

Application revision `b2d4e6f810ac` extends `f1a6b8c920de` on the path to current
head `f6c1a8d420be`. Use the existing guarded application
extension deployment process; baseline migrations and the NFC-only runner remain pinned.
The baseline checkpoint now uses revision-specific users metadata: old revisions still
require NOT NULL email and exclude the new staff-email constraint. Current ORM metadata
is not mutated by baseline checks. The normalized registration expression index belongs
to the application migration and is verified explicitly by the application verifier.

The forward-only migration takes ACCESS EXCLUSIVE on users, checks normalized registration
collisions across all roles, makes email nullable with `role='STUDENT' OR email IS NOT NULL`,
and adds a case-insensitive, whitespace-trimmed unique registration index. Raw college-ID
and email uniqueness remain. Existing rows and foreign keys are never rewritten. A collision
aborts before schema changes; reconcile through an approved identity-preservation plan,
never automatic merges/deletes. Reserve a write-maintenance window for locking/index build.

`borrower_resolve_or_create(uuid,uuid,text,text)` is SECURITY DEFINER with a fixed
`pg_catalog,pg_temp` search path, schema-qualified tables and protected existing NOLOGIN
owner `inventory_metadata_audit_owner`. Only inventory_app receives EXECUTE; PUBLIC and
other non-owner roles do not. Runtime users INSERT/UPDATE/DELETE/TRUNCATE remains denied.
The owner receives only the additional users SELECT/INSERT columns the routine needs,
with no UPDATE, DELETE, privilege delegation or role changes. The routine rechecks the
persisted active FACULTY/LAB_ASSISTANT actor, validates inputs, hardcodes STUDENT/active/NULL
email, and can only insert or return matching borrowers. It never updates identities or
provisions credentials. Actor UUID attribution trusts the server runtime credential, as
existing custody operations do; it is not independent proof of an HTTP login.

The unique expression index plus conflict-safe insertion serialize concurrent creation.
The routine makes at most three attempts; serialization/race failures require a new
transaction. Application responses use uppercase ASCII registrations. SQL normalization
uses an explicit Unicode boundary-whitespace set matching Python input stripping; this
also catches legacy case/whitespace collisions without changing stored data. Name matching
does not correct case/spelling or merge identities. Student activation continues to mean
borrowing eligibility, not permission to authenticate. Optional reminders skip missing email.

Deployment verification checks routine signature, exact body, owner, search path, behavior
flags, EXECUTE ACLs, effective owner/runtime column privileges, nullable email, retained
uniqueness and exact registration-index expression. F1 trigger checks remain unchanged.
Run the database-free borrower/migration tests first, then disposable PostgreSQL tests for
fresh and populated upgrades, concurrency, history preservation and direct-SQL denials.
No production deployment or verification has been performed as part of authoring this change.

Verification commands for Windows PowerShell, from the repository root. The integration
command requires separate authorization and is not part of database-free handover checks.
Stop after any nonzero exit code. The Python subprocess deadlines are
external process timeouts; pytest's faulthandler option supplies stack diagnostics.

```powershell
$env:INVENTORY_APP_PASSWORD = 'unused-test-password'
$env:AUTH_MODE = ''
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.\.venv\Scripts\python.exe --% -c "import subprocess,sys; sys.exit(subprocess.run([sys.executable,'-X','faulthandler','-m','pytest','backend/tests/test_borrower_registration.py','-q','-o','faulthandler_timeout=120'],timeout=300).returncode)"
if ($LASTEXITCODE -ne 0) { throw 'Focused borrower verification failed; stop here' }
.\.venv\Scripts\python.exe --% -c "import subprocess,sys; sys.exit(subprocess.run([sys.executable,'-X','faulthandler','-m','pytest','backend/tests','--ignore=backend/tests/integration','-q','-o','faulthandler_timeout=120'],timeout=600).returncode)"
if ($LASTEXITCODE -ne 0) { throw 'Database-free regressions failed; stop here' }
.\.venv\Scripts\python.exe --% -c "import subprocess,sys; sys.exit(subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','scripts/run_disposable_docker_tests.ps1'],timeout=1800).returncode)"
if ($LASTEXITCODE -ne 0) { throw 'Disposable PostgreSQL verification failed; inspect output before retrying' }
```

The Docker command uses only the existing disposable harness, not host Alembic/psql.
Reinspect its no-port/tmpfs isolation before running. Its normal path performs labelled
project cleanup; an external timeout can interrupt that cleanup. If interrupted, stop,
retain the printed unique project ID, and inspect its labelled resources before any
separately reviewed cleanup. Never substitute a host database or automatically retry a
timed-out run. These shell environment overrides are test-only; restore deployment values
before launching the application. The new populated-upgrade and concurrency cases run
inside the disposable suite along with existing history-guard verification.

## Sensitive access logging

The default `uvicorn backend.app.main:app` entry point installs safe logging filters
before loading application dependencies. Raw `uvicorn.access` records are suppressed:
they contain full query strings. A single replacement INFO record on `uvicorn.error`
reports method, registered route template, HTTP status, elapsed milliseconds and whether
execution raised. It wraps the body limiter, so 400/413 errors are also recorded.
404/early-rejection paths use `<unmatched>`; parameter values and unrecognized paths are
never logged. Neither connection/proxy addresses nor request/response headers or bodies
are included. NFC tags, student query identifiers and bearer credentials are omitted.

Uvicorn exception records retain exception type and up to 12 code locations (basename,
line, function), omitting exception messages, chains, source lines and locals. This avoids
SQL parameter/credential leakage while retaining locations for diagnosis. Other server
ERROR messages are generic. HTTP status/response bodies and exception propagation are
unchanged. Uvicorn ASGI trace records, protocol debug records and WebSocket URL records
are suppressed, including when the server log level is lowered. Keep production at INFO;
do not enable SQLAlchemy `echo` or request/body logging in custom middleware or APM.
The database engine currently leaves SQL echo disabled and authentication returns
generic provider failures; the runtime contains no explicit token/identity print calls.

Keep the Uvicorn INFO handler enabled to retain replacement access records (`--no-access-log`
only disables Uvicorn's original access handler). Filters are also installed when the
application middleware is built to support programmatic development launches. A custom
logging configuration applied later can remove filters or add other sinks: call
`install_secure_logging()` after custom configuration and before accepting traffic.
Application filters cannot protect records emitted before the application loads,
independent loggers/servers, or third-party instrumentation that captures raw requests.
The normal reload/worker entry point imports the application and installs protections
in each serving process; avoid sensitive data in startup command arguments/config errors.

Reverse-proxy/load-balancer/WAF access logs must independently omit query strings,
Authorization/Cookie headers, bodies and dynamic identifier path segments. Do not use
full request-URI logging. Trust forwarding headers only from controlled proxy addresses;
the application access record does not identify clients from these headers. Configure
Docker stdout/stderr drivers, log shippers and APM with appropriate redaction, access
controls, retention and rotation. The disposable PostgreSQL harness disables database
Docker logging; this does not configure production container/proxy logging. Review
database statement/error logging separately because SQL parameters can contain personal
data. Reproduce failures with synthetic data instead of enabling raw tracebacks in
shared production logs.

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
