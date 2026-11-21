# Student NFC registration backend

Students remain non-login borrower identities in `users` (`role=STUDENT`). This
feature adds card associations; it does not change checkout/return APIs, equipment
scanning or staff-only authentication. Card UID and payload are not login credentials
and must never become a checkout authorization factor by themselves.

## Configuration and hardware approval

The default is **fail closed**: initiation/verification is unavailable until an
operator supplies all three settings through the application's protected environment:

- `STUDENT_NFC_SCANNER_DIGESTS`: JSON object mapping a dedicated student-write scanner
  UUID to its lowercase SHA-256 credential digest (64 hex characters). At most 100
  scanners/16 KiB of configuration. Removing an entry revokes that scanner. Use a
  newly generated independent 32-byte CSPRNG key per device, encoded as canonical
  unpadded URL-safe Base64 (43 characters); the digest is SHA-256 of the ASCII
  encoded credential, as with existing scanner authentication. The configuration
  contains digests only, not plaintext device keys.
- `STUDENT_NFC_PAYLOAD_KEY`: a separate canonical Base64 32-byte server-only CSPRNG
  key used to derive repeatable pending payload challenges. Never give this key to
  a device/browser or reuse a scanner credential. Reusing a configured student
  scanner key is rejected. Use a proper deployment secret store; never commit keys.
- `STUDENT_NFC_WRITE_PROFILE`: an approved profile identifier, 1–64 ASCII letters,
  digits, dots, underscores or hyphens. **Leave unset until college IT/hardware
  owners approve the actual card type, authorized writable memory area, capacity,
  encoding and read-back procedure.** This is an explicit operator assertion of
  approval, not automatic hardware discovery.

No scanner is provisioned by this change. Existing equipment-scanner registration,
equipment credentials, staff OAuth tokens and capture-session secrets do not grant
student-write authority. Operators must independently distribute dedicated keys
to compatible authorized student-write hardware over a trusted channel. All workers
must share the same server key/profile and device-digest allowlist; coordinate
updates/revocation across workers. Key rotation while pending requires attempts to
expire and restart rather than reusing reconstructed payloads from a changed key.

The existing ESP32 equipment firmware is unchanged and **cannot perform this
student-write verification protocol**. Physical writing is deliberately absent.
TLS with certificate verification is required when transferring credentials or
challenges. Do not log Authorization, JSON request/response bodies, card UIDs or
prepared payloads in proxies, infrastructure or debugging tools.

## Staff contracts

Only active FACULTY and LAB_ASSISTANT staff authenticate to these endpoints using
the existing staff authentication. ADMIN and STUDENT remain denied. All bodies
reject unexpected fields. Borrower-name/registration validation is reused: trimmed
name (1–150 characters), trimmed ASCII registration number (1–50, uppercase).
Card UIDs are trimmed, uppercase hex of exactly 4, 7 or 10 bytes; leading zeroes
are preserved, punctuation/internal spaces are invalid.

`POST /student-nfc/registrations`:

```json
{
  "student_name": "Example Borrower",
  "registration_number": "REG-123",
  "card_uid": "04A1B2C3",
  "request_id": "<client-generated UUID retained for retries>",
  "scanner_id": "<dedicated student-write scanner UUID>"
}
```

Returns HTTP 200 with `registration_id`, `student_id`, `card_uid`, `status`,
`created_at`, `verified_at`, `expires_at`, `write_profile` and `payload`.
The borrower is resolved/created using the existing authorized database routine
within the same owned transaction as NFC association creation. A conflict rolls
back any new borrower. Existing identities, names, UUIDs and checkout records are
never overwritten. A student can have one associated card; a card one associated
student, including while PENDING_WRITE/FAILED. Replacement/reassignment is outside
this API and requires a separately reviewed policy.

The payload is ASCII `CNINV1:<random attempt nonce UUID>:<43-character HMAC proof>`
(87 bytes), containing no student UUID, name or registration number. It is an
application payload, not an NDEF record, card-memory map or existing card format.
An approved writer must account for any additional encoding overhead and verify
that the college-authorized region can hold it. Never overwrite sector keys,
manufacturer blocks, institutional applications or unknown card contents.

Repeated pending requests with the same request UUID, student/card, initiating
staff, scanner and profile return the same association and challenge within the
five-minute attempt lifetime. Competing requests conflict (409). Once failed or
expired, restarting the same association rotates its random attempt nonce/challenge
even if the client repeats the old request UUID. The stable registration/student
UUIDs and original creation timestamp remain. An already ACTIVE matching
association returns its status without another write payload.

`GET /student-nfc/registrations/{registration_id}` returns status fields only.
Expired pending attempts transition lazily to FAILED; they are never accepted as
active even before a status read. `POST /student-nfc/cards/lookup` with
`{"card_uid":"04A1B2C3"}` returns status fields for ACTIVE associations only.
Unknown/pending/failed card lookups return 404. POST keeps raw card UIDs out of
query strings/default access logs. No endpoint exposes student details anonymously.

## Authenticated read-back verification

After a compatible trusted writer has written and independently read back the
approved area, it submits:

```text
POST /student-nfc/registrations/<registration UUID>/verify
Authorization: StudentNFC <dedicated scanner UUID>.<dedicated device secret>
Content-Type: application/json
```

```json
{
  "card_uid": "04A1B2C3",
  "payload": "<exact ASCII payload actually read back from that card>",
  "write_profile": "<configured approved profile identifier>"
}
```

No `success` flag is accepted. The dedicated authenticated scanner must match the
scanner bound at initiation. The row is locked; registration, card UID, stored
expected payload digest, profile, pending state and expiry must all match before
activation. The database stores only SHA-256 of the expected challenge-bearing
payload, not its raw bytes. After activation the challenge cannot be used again:
replayed verification returns 409. Wrong-scanner registrations return 404; invalid
device credentials 401; mismatched/stale evidence 409; invalid schemas 422;
unconfigured approval/keys 503. Scanner success returns only
`{"registration_id":"<UUID>","status":"ACTIVE"}`—no borrower data.

Authentication plus fresh read-back evidence authenticates the reporting device;
it **does not remotely prove an actual electrical write or prevent a compromised
authorized writer from lying**. The writer must read the payload from the physical
expected card and enforce approved memory-area/card compatibility, rather than
echoing browser data. Hardware review/validation remains a release requirement.
No attestation or cryptographic anti-cloning property of ordinary RFID UIDs is
claimed. Student card cloning/rescanning must not authorize inventory mutations.

## Migrations, safety and rollout

The additive main revision is `e4b7c9d210af`, directly after `d6f2b8a931c0`.
It creates only the private `student_nfc_cards` table, constraints and narrowly
scoped SECURITY DEFINER routine `student_nfc_manage(text,uuid,uuid,jsonb)`.
Runtime `inventory_app` gets EXECUTE only, no table SELECT/INSERT/UPDATE/DELETE.
The owner stays the existing protected NOLOGIN role; search path is fixed and
the application deployment verifier checks routine body, privileges and storage.
No new privileges on users/equipment/checkouts/maintenance are introduced.

Because this repository also has an application-extension chain ending at
`c3a7e9b120df`, new merge revision `a9c2e4f610bd` joins that chain to the requested
main revision. The application chain includes that merge and now targets timing
correction `f6c1a8d420be`;
the baseline runner remains pinned to `d6f2b8a931c0`. Do not run a broad `upgrade
head` with the baseline configuration as a substitute for staged application
deployment. The additive migration requires the existing independent trusted
deployment superuser, not the runtime role. The usual application deployment
procedure supplies the prerequisite borrower creation routine and nullable student
email policy. This feature's main migration alone does not provide those extensions.

Fresh and populated application upgrades through `f6c1a8d420be` passed the final isolated verification. No host migration was performed during handover. Rollback requires a separately reviewed forward migration; associations are not deleted by downgrade.

## Verification status

Forward-only application revision `f6c1a8d420be` follows merge `a9c2e4f610bd`
and replaces only `student_nfc_manage`. It samples `clock_timestamp()` after the
registration row lock when initiating/retrying, and again after the relevant
lookup/verification lock before checking expiry. Waiting across expiry cannot
reuse a request-start timestamp to activate a stale challenge. The original
creation timestamp and all association data, ownership, grants, API contracts and
challenge/replay checks remain unchanged. Deployment verification rejects the
old routine body; the dedicated demo runner still targets `d6f2b8a931c0`.

Database-free timing/migration checks are in `backend/tests/test_student_nfc_timing.py`; service contracts are in `backend/tests/test_student_nfc.py`. The real lock/expiry, activation, conflict, replay, retry, concurrency and runtime-privilege tests are in `infra/nfc_postgres/tests/test_student_nfc.py`.

The project owner reports final isolated PostgreSQL verification: **312 passed, 0 failed, 0 skipped, exit code 0**, including fresh migrations through `f6c1a8d420be` and populated upgrades. Cleanup succeeded and protected host databases were untouched. This completed run supersedes earlier pending-verification notes; no PostgreSQL tests were rerun for handover. See [BACKEND_HANDOVER.md](BACKEND_HANDOVER.md).
