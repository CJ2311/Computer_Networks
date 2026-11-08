# Backend status

Updated 2026-11-08. Existing architecture retained. Ready for frontend contract integration against a disposable
verified backend; production college deployment remains conditional on IT/configuration and the limits below.

## Completed and verified

- Equipment/asset registration, metadata updates and atomic 1-100-record bulk registration reuse existing services.
- Authenticated inventory reads, bounded pagination, NFC lookup, active-student lookup and `/auth/me`.
- College access-token introspection boundary: explicit HTTPS endpoint/issuer/audience/client configuration, optional
  scopes, bounded verified-TLS calls, no redirects/fallback, protected issuer/subject-to-user mapping. Provider role
  claims are ignored; local roles/activation remain authoritative. IT endpoints/credentials are not invented.
- Existing individual checkout/return, own-student history, deadline/overdue logic and double-checkout protections.
- Quantity loans and full WORKING returns with captured names/destinations, original-request fingerprints, UUID retry
  keys, own-student history, overdue filters/flags, stock locks and deferred custody/ledger consistency checks.
- Immutable database-generated stock ledger: actor, reason, operation key, per-equipment revision and before/change/
  after quantities. A source-table lock protects migration opening balances. Runtime cannot rewrite history or disable
  capture. Client-keyed adjustments safely replay their original result, including across intervening balance changes.
- Faculty/admin offsite policy; explicit destinations use one shared lending rule for individual and quantity loans.
- Existing maintenance lifecycle/independent verification and immutable audit events, including versioned notes/listing.
- Existing NFC audit snapshots, retry keys, discrepancy reports and versioned closure; audit results preserve custody.
- Configurable overdue reminder queue/CLI, disabled by default. One automatic SMTP attempt per loan, concurrent claims,
  TLS-only transport and durable UNCERTAIN handling. No actual email is sent by automated tests.

## Database migrations and safety

New revision: `backend/alembic/application_versions/e8a4c2d190ab_operations.py`, parent `d6f2b8a931c0`.
It adds six tables: external_identities, equipment_loan_policies, loan_destinations, quantity_loans, stock_ledger,
overdue_reminders. It installs immutable ledger/custody triggers, takes protected ownership and applies narrow runtime
permissions. It changes no shared role attributes/memberships. Downgrade deliberately refuses destructive history loss.

The original seven migration files and dedicated NFC environment are unchanged. Ordinary Alembic sees the new extension;
the original NFC configuration/runner remains pinned to the reviewed seven-revision baseline. Its fixed table allowlist
prevents new ORM registration from altering baseline checkpoints. The new `alembic_nfc_application.ini`, application
safety helper and interactive extension runner reuse the strict dedicated environment and transaction advisory lock,
require a completed baseline and trusted administrator, verify actual identity/server/catalog/ACLs, and reject fallback.
The original runner intentionally rejects an already-extended database; use the extension verifier for that revision.

No host PostgreSQL connection, migration, reset, permission change or initialization occurred. Protected host databases
inventory_db, inventory_checkout_test and inventory_nfc_demo were untouched. All real database tests ran in new,
label-verified disposable PostgreSQL 18.4 instances with no network route/ports to host services. Generated credentials
stayed inside managed tmpfs storage. Both projects created for this work were cleaned up after label verification;
existing user containers were left running. Image/build caches may remain. No .env changes, commits or pushes.

## Validation

- Final database-free suite: **1,037 passed, 0 failed, 0 skipped**, exit code 0 (59.91 seconds).
- Final disposable PostgreSQL suite: **41 passed, 0 failed, 0 skipped**, exit code 0. This includes all original 23 NFC
  migration tests plus 18 application tests. An earlier disposable iteration also passed 37/37.
- PostgreSQL coverage: opening balance, protected ownership/ACLs, immutable ledger, repeat deployment verification,
  trigger predicate rejection, quantity issue/return retries, student isolation, shortage/rollback/backend termination,
  competing adjustments/issues, bulk asset/equipment registration, location policy, real maintenance notes,
  retry after default-location change, reminder success/uncertainty and concurrent reminder workers.
- Unit coverage includes college endpoint/configuration/claims/role mapping failures, SMTP mocks/TLS/header safety,
  post-send persistence failure, staff-route denial, schema validation, metadata import ordering and prior regressions.
- Python compileall and `git diff --check`: passed. No failed/skipped tests in the final successful runs.
- Legacy host PostgreSQL tests in backend/tests/integration were excluded. Windows unit tests need normal permissions
  for asyncio's internal socket pair; the application database engine remains blocked by an autouse fixture.

## API and deployment documentation

- [API routes, schemas and retry behavior](docs/backend/API.md)
- [Frontend integration requirements](frontend/INTEGRATION.md)
- [College authentication configuration and provisioning](docs/backend/AUTHENTICATION.md)
- [Migration, privilege, isolation, SMTP and deployment instructions](docs/backend/DEPLOYMENT.md)
- [Disposable infrastructure execution/cleanup](infra/nfc_postgres/README.md)

Frontend can integrate inventory, individual/quantity borrowing, history, maintenance, NFC audits, policy and ledger
contracts now against the verified isolated environment. Use OpenAPI, bearer headers, version checks and stable UUID
keys. Configure explicit CORS origins. Never advertise unimplemented SSO browser flow or assigned-lab isolation.

## Remaining blockers and security limits

1. College IT must approve RFC 7662 endpoint/claims/client authentication, issuer/audience/scopes, browser OAuth/PKCE
   client/redirects, provisioning and revocation procedures. No live college SSO test occurred. The optional opaque
   adapter remains explicit/demo-oriented, has no automatic expiry, and is not a college SSO fallback.
2. No lab-assignment relation exists. Assigned-lab authorization cannot be safely inferred from location strings.
   Staff remain laboratory-wide; multi-lab deployment requires a reviewed lab/membership model and consistent scoping.
3. Quantity returns currently accept only all inspected WORKING units. Partial returns and damaged/under-repair bulk
   custody need a separate condition/repair model; such returns are rejected rather than silently restocked. Individual
   damaged returns and maintenance remain supported. No physical NFC movement enforcement is claimed.
4. SMTP credentials/relay approval, external scheduling, monitoring and operator reconciliation of UNCERTAIN jobs
   remain deployment tasks. SMTP acceptance is not inbox delivery. At-most-one automatic attempt can miss a reminder
   after a crash; ambiguous jobs are never automatically resent. A return can race with an already-claimed reminder.
5. Real deployment requires explicit approval, backups/restore rehearsal, rollout/draining old writers, secrets/TLS,
   rate limiting and secret-safe access logs. Protected-object migrations require the trusted administrator. Advisory
   locks coordinate cooperative runners, not arbitrary administrator SQL. Catalog validation does not attest every SQL
   definition or protect against trusted infrastructure administrators. Actor attribution trusts the application
   connection to set operation context; a compromised runtime credential can forge new operations, though it cannot
   rewrite recorded ledger history. Keep DB access private and rotate credentials. Health remains liveness only.

## Files changed

New core files: `backend/app/auth/college.py`, `backend/app/models/operations.py`, `backend/app/routers/operations.py`,
`backend/app/schemas/operations.py`, services `quantity_loans.py`, `lending_policy.py`, `reminders.py`;
`backend/alembic/application_safety.py`, the new migration, `alembic_nfc_application.ini`,
`scripts/deploy_nfc_application.py`, `scripts/send_overdue_reminders.py`.
Modified integration points: auth dependencies/token modes; model registration; main/router/schema/service registration,
stock and checkout paths; ordinary Alembic version locations; the baseline runner's fixed table catalogue; worker image
allowlist/dependencies/diagnostics and infrastructure README. Existing transaction guards remain intact (comment updated).
New tests: college auth, application contracts, reminders and disposable application operations; existing regression,
metadata/import, stock, API and infrastructure tests updated for the explicit extension.
Documentation: this report, docs/backend/{API,AUTHENTICATION,DEPLOYMENT}.md and frontend/INTEGRATION.md.
Pre-existing uncommitted safety/auth/workflow changes were retained. See Git status for the full combined working tree.
