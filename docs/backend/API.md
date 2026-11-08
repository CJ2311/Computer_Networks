# Backend API contract

The authoritative typed request/response contract is `/openapi.json` (`/docs` for interactive documentation).
All business endpoints require authentication. Only `/` and `/health` are public liveness endpoints.
See [authentication](AUTHENTICATION.md), [deployment](DEPLOYMENT.md), and [frontend integration](../../frontend/INTEGRATION.md).

Staff below means ADMIN, FACULTY or LAB_ASSISTANT. Existing narrower permissions remain unchanged.

| Methods | Route | Permission / behavior |
|---|---|---|
| GET | `/auth/me` | Authenticated persisted identity |
| GET, POST | `/equipment` | All roles read; staff register; list `limit/offset` |
| GET, PATCH | `/equipment/{id}` | All roles read; staff update audited metadata |
| POST | `/equipment/bulk` | Staff; `{items:[EquipmentCreate...]}`, 1-100, atomic |
| GET | `/equipment/{id}/stock` | All roles; quantity equipment only |
| POST | `/equipment/{id}/stock/adjustments` | Staff; expected quantity, signed change, reason, optional UUID idempotency key |
| GET | `/equipment/{id}/ledger` | Staff; immutable quantity history, ordered by per-equipment revision, paginated array |
| GET, POST | `/equipment/{id}/loan-policy` | All roles read; ADMIN/FACULTY set strict boolean `allow_offsite` |
| GET, POST | `/assets` | Staff; list/register; list `limit/offset` |
| POST | `/assets/bulk` | Staff; `{items:[AssetCreate...]}`, 1-100, atomic |
| GET, PATCH | `/assets/{id}` | Staff; read/update audited metadata; guarded relocation |
| GET | `/assets/lookup?nfc_tag_id=...` | Staff; NFC identification, not authentication |
| GET | `/students/lookup?college_id=...` | Staff; exact active-student lookup; ID/name/college ID only |
| GET, POST | `/checkouts` | History: staff or own student scope; issue: staff |
| GET | `/checkouts/{id}` | Staff/owning student; foreign/missing student records are both 404 |
| GET | `/checkouts/{id}/destination` | Same borrowing scope; null for legacy/no explicit destination |
| POST | `/checkouts/{id}/return` | FACULTY/LAB_ASSISTANT; preserves maintenance/custody safeguards |
| GET, POST | `/quantity-loans` | History: staff/own student; issue: staff; GET `limit/offset/overdue` |
| GET | `/quantity-loans/{id}` | Staff/owning student; foreign/missing records are both 404 |
| POST | `/quantity-loans/{id}/return` | FACULTY/LAB_ASSISTANT; UUID key and explicit `condition_status: WORKING` |
| GET, POST | `/maintenance` | Staff; list/report; list `limit/offset/status/asset_id` |
| GET | `/maintenance/{id}`, `/maintenance/{id}/events` | Staff; events paginated |
| POST | `/maintenance/{id}/notes`, `/maintenance/{id}/recover` | Staff; notes append immutable versioned events |
| POST | `/maintenance/{id}/assign`, `/maintenance/{id}/verify`, `/maintenance/{id}/cancel` | ADMIN/FACULTY; independent verification |
| POST | `/maintenance/{id}/start`, `/maintenance/{id}/submit` | Assigned FACULTY/LAB_ASSISTANT |
| GET, POST | `/inventory-audits` | Staff; list/create frozen inventory snapshot |
| GET | `/inventory-audits/{id}`, `/{id}/report`, `/{id}/expected-assets` | Staff; reports/findings paginated |
| GET, POST | `/inventory-audits/{id}/scans` | Staff; UUID retry key; immutable scan classification |
| POST | `/inventory-audits/{id}/finalize` | FACULTY only; expected version |
| POST | `/inventory-audits/{id}/cancel` | ADMIN/FACULTY; expected version |

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
location differing from the asset's recorded location requires a faculty/admin-enabled equipment policy. Quantity
loans default to the equipment's default location under the same rule. This checks declared destination, not physical
movement, and does not confer access to another lab. Existing audit/maintenance services remain individual-asset based.

409 means conflict/reload; 422 invalid input; 401 invalid credentials; 403 denied; 503 unavailable/uncertain outcome.
Never automatically retry a non-idempotent mutation after 503. Bulk registration failures roll back the whole batch;
registration is not an idempotent operation. Audit scan keys retain their existing replay semantics.
