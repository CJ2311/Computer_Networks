# Backend API contract

The authoritative typed request/response contract is `/openapi.json` (`/docs` for interactive documentation).
Staff business endpoints require an active FACULTY or LAB_ASSISTANT identity. STUDENT and ADMIN cannot
authenticate in any staff mode; students remain borrower records. Device ingestion and student-write verification
use separate device credentials. `/` and `/health` are public liveness endpoints; FastAPI also exposes
`/docs`, `/redoc` and `/openapi.json` without staff authentication. Liveness does not check database readiness.
See [authentication](AUTHENTICATION.md), [deployment](DEPLOYMENT.md), and [frontend integration](../../frontend/INTEGRATION.md).

Staff below means FACULTY or LAB_ASSISTANT. Existing narrower permissions remain unchanged.

| Methods | Route | Permission / behavior |
|---|---|---|
| GET | `/auth/me` | Authenticated persisted identity |
| GET, POST | `/equipment` | Staff read/register; list `limit/offset` |
| GET, PATCH | `/equipment/{id}` | Staff read/update audited metadata |
| POST | `/equipment/bulk` | Staff; `{items:[EquipmentCreate...]}`, 1-100, atomic |
| GET | `/equipment/{id}/stock` | Staff; quantity equipment only |
| POST | `/equipment/{id}/stock/adjustments` | Staff; expected quantity, signed change, reason, optional UUID idempotency key |
| GET | `/equipment/{id}/ledger` | Staff; immutable quantity history, ordered by per-equipment revision, paginated array |
| GET, POST | `/equipment/{id}/loan-policy` | Staff read; FACULTY set strict boolean `allow_offsite` |
| GET, POST | `/assets` | Staff; list/register; list `limit/offset` |
| POST | `/assets/bulk` | Staff; `{items:[AssetCreate...]}`, 1-100, atomic |
| GET, PATCH | `/assets/{id}` | Staff; read/update audited metadata; guarded relocation |
| GET | `/assets/lookup?nfc_tag_id=...` | Staff; NFC identification, not authentication |
| GET | `/students/lookup?college_id=...` | Staff; normalized active-student lookup; ID/name/college ID only |
| POST | `/students/resolve` | FACULTY/LAB_ASSISTANT; resolve or create an active borrower; always 200 on success |
| GET, POST | `/checkouts` | Staff-only history and issue |
| GET | `/checkouts/{id}` | Staff-only; missing records return 404 |
| GET | `/checkouts/{id}/destination` | Same borrowing scope; null for legacy/no explicit destination |
| POST | `/checkouts/{id}/return` | FACULTY/LAB_ASSISTANT; preserves maintenance/custody safeguards |
| GET, POST | `/quantity-loans` | Staff history/issue; GET `limit/offset/overdue` |
| GET | `/quantity-loans/{id}` | Staff; missing records return 404 |
| POST | `/quantity-loans/{id}/return` | FACULTY/LAB_ASSISTANT; UUID key and explicit `condition_status: WORKING` |
| GET, POST | `/maintenance` | Staff; list/report; list `limit/offset/status/asset_id` |
| GET | `/maintenance/{id}`, `/maintenance/{id}/events` | Staff; events paginated |
| POST | `/maintenance/{id}/notes`, `/maintenance/{id}/recover` | Staff; notes append immutable versioned events |
| POST | `/maintenance/{id}/assign`, `/maintenance/{id}/verify`, `/maintenance/{id}/cancel` | FACULTY; independent verification |
| POST | `/maintenance/{id}/start`, `/maintenance/{id}/submit` | Assigned FACULTY/LAB_ASSISTANT |
| GET, POST | `/inventory-audits` | Staff; list/create frozen inventory snapshot |
| GET | `/inventory-audits/{id}`, `/{id}/report`, `/{id}/expected-assets` | Staff; reports/findings paginated |
| GET, POST | `/inventory-audits/{id}/scans` | Staff; UUID retry key; immutable scan classification |
| POST | `/inventory-audits/{id}/finalize` | FACULTY only; expected version |
| POST | `/inventory-audits/{id}/cancel` | FACULTY; expected version |

## Borrower resolution for checkout

Students are borrower records, never application login identities. Faculty or authorized
lab assistants submit `POST /students/resolve` with their normal Authorization bearer
header and JSON:

```json
{"student_name":"Jane Doe","registration_number":"reg-123"}
```

Successful creation and resolution both return HTTP 200:

```json
{"student_id":"00000000-0000-4000-8000-000000000001","student_name":"Jane Doe","registration_number":"REG-123","created":true}
```

`created` is false for an existing borrower. Send the returned `student_id` to the
unchanged `POST /checkouts` or `POST /quantity-loans` request. Resolving a borrower does
not issue equipment or modify stock. A later checkout failure may leave an unused
borrower identity; retrying resolution with the same name/registration safely reuses it.

Both inputs are trimmed. Names require letters, no control characters and at most 150
characters. Registration numbers contain 1–50 ASCII letters/digits plus `.`, `_`, `/`,
or `-`, start with a letter/digit, and normalize to uppercase. Name matching is exact
after trimming, including letter case and interior spaces. A different name, inactive
borrower, or staff/administrator registration collision returns generic 409; no profile
is overwritten. Legacy registration numbers outside this ASCII pattern remain stored and usable through
existing UUID-based checkout contracts but require a separately reviewed correction to
use this new ASCII registration interface. Legacy stored spelling, UUIDs and emails are
not rewritten; the response registration number is canonical uppercase and the name is
the stored name. `/students/lookup` uses the same Unicode edge-whitespace trimming
and PostgreSQL `C`-collation uppercase comparison, while returning the preserved
stored `college_id`, name and UUID. Its existing query length/NUL validation and
staff authorization remain unchanged. Unknown or ineligible borrowers return 404;
ambiguous normalized identities return generic 409 rather than selecting a UUID.
Lookup is read-only and never creates, updates or merges borrower records.

Extra fields such as role, email, identity mappings or activity flags are forbidden.
New rows have STUDENT classification, active borrowing eligibility and NULL email, with
no credentials/mappings created. Input errors return 422, missing/invalid authentication
401 (or the configured mode's fail-closed response), disallowed application roles 403,
and database unavailability 503. On an uncertain commit, retry the same normalized
details; do not invent a new registration. Concurrent identical requests return one UUID
and one creation result; conflicting names cannot create duplicate borrowers. Serialization
conflicts may return 409 and require a fresh request.

## Quantity custody and ledger

Create a quantity loan with `idempotency_key`, `equipment_id`, `student_id`, strict positive `quantity`, timezone-aware
future `due_at`, and optional declared `destination`. Its UUID key is the loan ID. The original request fingerprint
makes identical retries safe even if default locations change; a changed request with the same key returns 409.
The database enforces that issue/return stock ledger entries commit with custody. History returns captured equipment
name/location, original quantity/deadline, current overdue and returned-late flags. There are no partial bulk returns.
A return requires all units inspected as WORKING; damaged/under-repair quantities are rejected, not silently restocked.
A separate bulk-condition/repair custody design is needed before accepting those returns through this API.

Stock adjustments should always include a client-generated UUID `idempotency_key`, `expected_quantity`, nonzero signed
`change` and meaningful `reason`. The optional key preserves old clients, but clients omitting it cannot safely replay
an ambiguous write. Retry the identical request with the original key; changed actor/payload returns 409. Replays return
the original recorded result, so refresh GET stock afterward. Each ledger entry includes actor, reason, previous/change/
new balance, revision and timestamp. Opening balances are recorded by migration without pretending to know past actors.
No public endpoint can rewrite ledger history. Inventory corrections use compensating adjustments.

Individual checkout remains an individual-asset workflow. An omitted destination means onsite lending; an explicit
location differing from the asset's recorded location requires a faculty-enabled equipment policy. Quantity
loans default to the equipment's default location under the same rule. This checks declared destination, not physical
movement, and does not confer access to another lab. Existing audit/maintenance services remain individual-asset based.

409 means conflict/reload; 422 invalid input; 401 invalid credentials; 403 denied; 503 unavailable/uncertain outcome.
Never automatically retry a non-idempotent mutation after 503. Bulk registration failures roll back the whole batch;
registration is not an idempotent operation. Audit scan keys retain their existing replay semantics.

## Additional endpoint families

| Methods | Route | Contract |
| --- | --- | --- |
| POST | `/assets/{id}/report-missing`, `/assets/{id}/reconcile-missing` | Staff; report/reconcile with retry/version contracts from OpenAPI |
| GET | `/assets/{id}/missing-incidents`, `/assets/{id}/missing-incidents/{incident_id}/events` | Staff; immutable incident history |
| GET | `/reports/inventory/overview`, `/reports/inventory/categories` | Staff; aggregate inventory and category summaries |
| GET | `/reports/inventory/stock`, `/reports/inventory/missing`, `/reports/inventory/maintenance` | Staff; filtered operational pages, default limit 25, maximum 100 |
| POST | `/device-scans` | Scanner credential; equipment resolution only |
| POST | `/scan-sessions/{session_id}/events` | Staff session owner plus capture secret; bounded event page |
| POST | `/student-nfc/registrations` | Staff; prepare/retry student card association, 200 |
| GET | `/student-nfc/registrations/{registration_id}` | Staff; status without payload |
| POST | `/student-nfc/cards/lookup` | Staff; ACTIVE association only |
| POST | `/student-nfc/registrations/{registration_id}/verify` | Dedicated StudentNFC device; read-back evidence, 200 |

See [student NFC](STUDENT_NFC.md) and [scanner provisioning](SCANNER_PROVISIONING.md)
for exact headers, JSON, expiry, sequence and replay rules. Scanner ingestion returns
`scan_id`, `sequence`, normalized `uid`, resolved `asset_id`, `received_at`, `expires_at`
and `duplicate`. Polling returns `items`, `after_sequence`, `limit`, `has_more`;
each item omits `duplicate`. Events expire after 120 seconds; poll limit is 1-50.

## Request, response and error conventions

Use JSON bodies, UUID strings and ISO 8601 timestamps. Lending due times must be
timezone-aware and future. OpenAPI is authoritative for each endpoint's schema;
do not assume every list has the same envelope. Quantity-loan pages contain
`items`, `limit`, `offset`, `has_more`; the ledger is an array. Defaults and allowed
filters are declared per route. Create equipment/assets/checkouts/quantity loans
returns 201; borrower/NFC resolution and returns use 200.

For individual issue, send `asset_id`, `student_id`, `due_at`, optional `destination`
and `notes` to `/checkouts`. The response includes `id`, those identity references,
`issued_by`, `checked_out_at`, `due_at`, nullable `returned_at`/`returned_to`, `status`
and `notes`. Return with `condition_status` (WORKING, DAMAGED or UNDER_REPAIR),
optional `recovery_of_request_id` and `notes`. The response adds condition,
availability, maintenance hold/request ID and overdue status. A damaged return
must not be presented as available inventory.

Quantity issue returns the UUID loan ID, equipment/student/issuer IDs, original
quantity, captured equipment name/destination, due/issue/return timestamps,
return actor/key, `is_currently_overdue` and `was_returned_late`. Return JSON is
`{"idempotency_key":"<retained UUID>","condition_status":"WORKING"}`.

HTTP exceptions generally use `{"detail":"message"}`; validation errors use
FastAPI's structured `detail` array. There is no universal custom error-code envelope.
404 means missing or deliberately hidden scope; 409 means conflict/stale state;
422 means schema/domain validation; 401/403 mean authentication/authorization failure;
413 means oversized body; malformed Content-Length yields 400. Scanner/provider
admission limits can yield 429; 503 means unavailable or potentially uncertain outcome.
Render generic server errors without exposing credentials, SQL or request bodies.
Only retry writes with documented idempotency semantics and the original payload/key.

Current application schema head: `f6c1a8d420be`. See
[handover](BACKEND_HANDOVER.md) for architecture, setup, verification and remaining
frontend/hardware responsibilities.
