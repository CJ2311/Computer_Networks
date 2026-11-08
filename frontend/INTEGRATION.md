# Frontend integration requirements

Use [backend API documentation](../docs/backend/API.md) and `/openapi.json` for verified contracts.
No frontend implementation was changed by the backend work.

- College IT must register the browser client and approve its OAuth/PKCE flow. Send access tokens in Authorization:
  Bearer headers over HTTPS. The API exposes `/auth/me`, not college login/logout endpoints. Keep tokens out of URLs,
  browser bundles and localStorage by default. Do not use development headers in deployed clients.
- Read local role/activation from `/auth/me`; still handle server 401/403. Students may view only their own individual
  and quantity borrowing histories. Staff can resolve an active student using exact college ID at `/students/lookup`.
- Equipment/assets lists remain arrays with bounded limit/offset. Quantity history, maintenance and audit lists use
  page envelopes. Quantity history supports overdue filtering and reports current-overdue/returned-late booleans.
- Register one equipment/asset or submit atomic batches of 1-100. A failed batch registers nothing. Do not automatically
  replay registration after a timeout; reconcile using identifiers. NFC tags identify assets and never authenticate.
- Individual and quantity loans are separate APIs. Quantity create requires a fresh client UUID idempotency key.
  Repeat only identical requests with that key after network uncertainty. Return requires a new UUID key and an explicit
  WORKING condition after inspecting all units. Partial/damaged bulk returns are not supported; do not fake a working
  return or adjust stock to hide a custody problem. Individual damaged returns use the existing maintenance workflow.
- Supply timezone-aware deadlines. Omitted destinations mean onsite lending; away-from-recorded-location lending
  requires an explicit faculty/admin equipment policy. This does not implement assigned-lab authorization.
- Stock adjustments require an expected balance, signed change and reason; always send a stable UUID key. A replay
  returns its original result: refresh current stock/ledger afterward. 409 requires reload/reconciliation; 503 without
  an original retry key requires manual reconciliation, not an automatic replay.
- Use expected versions for maintenance notes/transitions and audit closure. Reuse original audit scan UUIDs for retries.
  Keep independent repair verification, staff-only controls and faculty-only audit finalization visible in the UI.
- Set explicit CORS_ALLOWED_ORIGINS on the backend. Authorization/Content-Type are allowed; credentialed cookies and
  X-Dev-User are not a deployed browser contract. `/health` is liveness, not database readiness.

Ready for frontend integration against the disposable verified backend. Real college SSO, assigned-lab isolation,
partial/damaged quantity custody and operational deployment sign-off remain separate blockers; do not advertise them.
