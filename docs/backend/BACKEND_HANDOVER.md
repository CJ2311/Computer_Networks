# Backend handover

## 1. Project overview

The Computer Networks Inventory Management System manages laboratory equipment,
individual assets, quantity stock, student borrowing, maintenance and inventory
audits. Students are borrower records; active FACULTY and LAB_ASSISTANT users
operate the application. ADMIN and STUDENT cannot authenticate to business APIs.

## 2. Technology and architecture

Python 3.12, FastAPI, Pydantic, SQLAlchemy, Psycopg, PostgreSQL and Alembic form
the backend. Dependency versions are pinned in `requirements.txt`. Routers validate
requests and permissions; services enforce workflows and own write transactions;
models and database constraints/triggers protect custody and immutable history.
PostgreSQL runtime access uses `inventory_app`, separate from the trusted deployment
administrator and protected NOLOGIN history owner. NFC UIDs identify objects and
never replace staff authorization. Hardware communicates with FastAPI, not PostgreSQL.

## 3. Folder structure

| Path | Purpose |
| --- | --- |
| `backend/app/main.py` | FastAPI entry point and middleware |
| `backend/app/auth/` | Staff, college, scanner and student-write authentication |
| `backend/app/routers/`, `schemas/` | Routes and typed JSON contracts |
| `backend/app/services/`, `models/` | Transactions, workflows and persistence |
| `backend/alembic/versions/`, `application_versions/` | Baseline/NFC and application migration graph |
| `backend/tests/` | Database-free tests; exclude nested `integration/` |
| `infra/nfc_postgres/` | Isolated, disposable PostgreSQL verification harness |
| `scripts/` | Guarded deployment, scanner provisioning and reminders |
| `docs/backend/` | API, authentication, deployment and NFC guidance |

## 4. Local setup

Use a working Python 3.12 installation. From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If an existing virtualenv reports a missing interpreter, recreate it using an
installed interpreter after preserving any local environment requirements.
Configure secrets privately; never commit `.env` or copy another installation's
credentials. Setup commands here are instructions for a later authorized setup.

## 5. Configuration

The application loads the repository `.env` without overriding existing process
environment values. Restart workers after configuration changes.

| Variable | Requirement/default |
| --- | --- |
| `INVENTORY_APP_PASSWORD` | Required runtime database password; no default |
| `INVENTORY_DB_HOST`, `INVENTORY_DB_PORT` | `localhost`, `5432`; explicitly select approved target |
| `INVENTORY_DATABASE_NAME` | Defaults to `inventory_db`; set explicitly for deployment |
| `APP_ENV`, `DEV_AUTH_ENABLED` | Production: `production`, `false` |
| `AUTH_MODE` | Explicit `college_introspection` or internal/demo `opaque_token`; default denies access |
| `COLLEGE_INTROSPECTION_URL`, `COLLEGE_ISSUER`, `COLLEGE_AUDIENCE` | College-approved HTTPS endpoint and exact claims |
| `COLLEGE_CLIENT_ID`, `COLLEGE_CLIENT_SECRET` | Secure introspection client credentials |
| `COLLEGE_REQUIRED_SCOPES` | Optional space-separated scope requirements |
| `AUTH_TOKEN_SHA256_MAP` | Opaque-mode JSON digest-to-college-ID mapping; no raw tokens |
| `CORS_ALLOWED_ORIGINS` | Comma-separated exact browser origins; default empty, no wildcard |
| `INVENTORY_MAX_REQUEST_BODY_BYTES` | Positive integer; default 8388608; device scans additionally capped at 2048 |
| `STUDENT_NFC_SCANNER_DIGESTS` | Dedicated student-writer UUID-to-SHA-256 JSON map |
| `STUDENT_NFC_PAYLOAD_KEY` | Separate canonical unpadded base64url 32-byte server secret |
| `STUDENT_NFC_WRITE_PROFILE` | Approved hardware/memory/encoding profile; leave unset until approved |
| `OVERDUE_EMAIL_ENABLED` | Default disabled; exact `true` enables SMTP delivery |
| `SMTP_HOST`, `SMTP_FROM` | Required when email enabled |
| `SMTP_PORT`, `SMTP_TLS_MODE` | Defaults 587 and `starttls`; `implicit` also supported |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Optional paired credentials |

See [deployment](DEPLOYMENT.md) for college introspection timeout/concurrency/rate
settings. Scanner device credentials and session bindings live in the database;
they are not staff bearer tokens or the student-writer allowlist.

## 6. Database and migration overview

The current application head is `f6c1a8d420be`. The original staged demo runner
remains pinned to `d6f2b8a931c0`. Application extensions add operations, missing
equipment, borrower resolution, scanner ingestion, history guards, the NFC merge
`a9c2e4f610bd`, and the final NFC lock-time correction.

Follow [deployment](DEPLOYMENT.md) and [staged setup](../../scripts/NFC_DEPLOYMENT.md)
with an independently authorized administrator. The guarded runners support only
the explicit approved `inventory_nfc_demo` target and verify identity, ownership,
grants and routine bodies. Initialize an empty approved target with the demo runner,
then use the application runner for extensions/verification. Do not substitute a
generic `alembic upgrade head`, `create_all`, role grants or a reset. Extension
downgrades are deliberately refused; plan backups and reviewed forward recovery.
Host `inventory_db`, `inventory_checkout_test` and `inventory_nfc_demo` are protected.

## 7. Starting FastAPI

After approved database setup, runtime configuration and staff provisioning:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Use `--reload` only for local development. Production needs a supervised process,
TLS termination, private database access, explicit CORS, proxy body/time/rate limits
and approved network exposure. `/health` is liveness only and does not query the DB.

## 8. API documentation

On the local server, open `http://127.0.0.1:8000/docs`, `/redoc`, or `/openapi.json`.
OpenAPI contains exact field types, required fields, response models and filters;
[API.md](API.md) explains workflows and retry rules. Documentation routes are
enabled by FastAPI and are not staff-authenticated business routes.

## 9. Authentication

Send `Authorization: Bearer <staff access token>`. College introspection verifies
issuer, audience, expiry and scopes, then resolves a pre-provisioned local identity.
Provider roles do not grant permissions. See [AUTHENTICATION.md](AUTHENTICATION.md).
There is no backend college login/logout or staff self-registration API.
Local development requires both development flags, a loopback request and
`X-Dev-User: DEV-FACULTY` or `DEV-ASSISTANT` mapped to an existing eligible user.
Never expose development authentication through a proxy.

## 10. Scanner and student NFC workflows

Equipment scanners use `Authorization: Scanner <uuid>.<device-secret>` with
`POST /device-scans`. A trusted operator provisions the scanner and a short-lived
staff-owned capture session. The device receives the session UUID; only the staff
dashboard receives its capture secret. Staff polls `POST /scan-sessions/{id}/events`
with bearer authentication and `X-Scan-Session-Token`. Persist device sequence
numbers across restarts and retry uncertain scans with identical IDs/payloads.
Scans only resolve existing equipment; they never issue a loan automatically.
See [SCANNER_PROVISIONING.md](SCANNER_PROVISIONING.md).

Student registration is a separate protocol: staff creates/resumes a pending card
association, a dedicated approved writer writes and reads back the supplied payload,
then authenticates as `StudentNFC` to verify. Only verified unexpired evidence makes
the association ACTIVE. Staff lookup returns only ACTIVE associations. Five-minute
expiry is checked after relevant row locks; retries cannot activate expired evidence.
See [STUDENT_NFC.md](STUDENT_NFC.md). Equipment firmware does not implement this
student-write protocol. Physical card compatibility and read-back remain hardware work.

## 11. Inventory and lending

Register equipment as individual or quantity tracked; register physical assets only
for individual tracking. Bulk registration accepts 1-100 items atomically. Metadata
updates are audited; quantity changes use stock adjustments and immutable ledger.
Resolve a borrower using `/students/resolve`, then explicitly issue `/checkouts`
with asset/student IDs and future due time, or `/quantity-loans` with equipment,
quantity and a retained UUID idempotency key. Offsite destinations require faculty
policy approval. Staff must explicitly inspect and confirm returns.

Individual returns record WORKING, DAMAGED or UNDER_REPAIR and preserve maintenance
holds. Quantity returns require all units WORKING and a UUID retry key; partial or
damaged bulk returns are unsupported. Refresh state after 409 or a successful replay.
Individual checkout and bulk registration have no general idempotency key; reconcile
uncertain outcomes before resubmitting. History cannot be edited or deleted.

## 12. Maintenance, audits and reporting

Staff reports maintenance and appends notes. Faculty assigns, verifies or cancels;
the assigned worker starts/submits repairs. Independent verification and version
checks protect transitions. Recovery and returns preserve custody/repair safeguards.
Inventory audits freeze expected individual assets, classify scans and require
faculty finalization/cancellation. Missing incidents have explicit report/reconcile
operations and immutable events. `/reports/inventory/{overview,categories,stock,missing,maintenance}`
provides read-only operational summaries with bounded filters/pages where applicable.

The reminder script is a separate operator job, not an API background scheduler.
Delivery requires configured SMTP and a valid borrower email; newly resolved borrowers
have NULL email. An uncertain SMTP attempt requires reconciliation, not automatic resend.

## 13. Troubleshooting and limits

- Startup password error: inject `INVENTORY_APP_PASSWORD`; check explicit target settings.
- 401/403: check token mode, local identity mapping, activity and staff role; never grant ADMIN as a workaround.
- 409: refresh version/custody/stock; retain original retry keys and compare the payload.
- 422: inspect OpenAPI field/enum validation and reject unsupported extra input fields.
- 413/400: check body size or malformed/duplicate Content-Length; align proxy limits.
- 503: check provider/configuration/database availability privately; reconcile uncertain writes before retrying.
- NFC 503: confirm approved profile and separate writer/server secrets; never enable an unreviewed card profile.
- Empty scanner page: check session ownership, expiry, capture secret, cursor and 120-second event TTL.
- Windows subprocess decoding: runner captures both pipes as UTF-8 with replacement for malformed bytes.

Redact tokens, session headers, UIDs, payloads and bodies from logs. Do not expose
PostgreSQL to devices. Authorization is laboratory-wide: location text does not
implement assigned-lab isolation. There is no automated device/browser pairing,
secret recovery, student card reassignment, physical-write attestation or UID
anti-cloning guarantee. Rate controls inside a worker are not cluster-wide controls.

## 14. Database-free testing and safety

Never run an unrestricted recursive pytest invocation on this checkout: nested
integration suites require explicit isolated infrastructure authorization. For a
database-free configuration/runner smoke check from the root:

```powershell
$env:INVENTORY_APP_PASSWORD = 'unused-unit-placeholder'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_disposable_docker_runner.py backend/tests/test_database_configuration.py backend/tests/test_cors.py backend/tests/test_backend_handover_docs.py
```

Unit fixtures prohibit application-engine connections; runner tests fake Docker.
Do not run Docker, migrations, provisioning, reminders or any PostgreSQL suite for
handover documentation checks. Future integration runs require separate authorization
and the reviewed disposable harness, never host databases or legacy integration scripts.

## 15. Verified results

The final isolated PostgreSQL verification reported by the project owner succeeded:
**312 passed, 0 failed, 0 skipped, exit code 0**. Fresh migrations through
`f6c1a8d420be` and populated upgrades were verified, along with student NFC, scanner
security, inventory, checkout/return, maintenance, reporting and concurrency.
Disposable containers, volumes and networks were cleaned up successfully; protected
host databases were untouched. These are supplied completed-run results, not a new
integration run during handover. See the final handover report for current database-free results.

Handover database-free verification on 2026-11-21: **127 passed, 1 deselected,
exit code 0** across runner/encoding, database configuration, CORS, documentation,
disposable infrastructure and NFC timing checks. Database connections and network
access were explicitly blocked. The project virtualenv references a missing Python
3.12 executable, so verification used installed Python 3.13 with available local
dependencies. An initial run had 127 passes and one environment-related import
failure: `test_handover_module_dependencies_import_without_database_or_docker`
requires the Python 3.12 Pydantic extension and was then explicitly deselected.
Restore the Python 3.12 environment to rerun that check and the full unit suite;
the handover run does not claim full unit-suite verification.

## 16. Remaining integration responsibilities

Frontend: implement IT-approved college OAuth/PKCE and token handling, staff UI,
borrower resolution, explicit checkout/return confirmation, scanner polling/cursor
handling, NFC registration states, validation/conflict/retry handling and reports.
Hardware: securely provision credentials/session IDs, persist scan sequence numbers,
verify TLS, handle transport retries, and validate approved student-card memory,
capacity, encoding and independent physical read-back before enabling writing.
Operations/college IT: provision verified staff mappings, approve identity/card
policies, distribute secrets, configure TLS/proxy supervision/backups/monitoring and
schedule reminders if required. These deployment/integration dependencies remain;
the supplied final integration run reports no failing backend test blocker.
