# Secure scanner provisioning (Step 12C.2A)

This is an explicitly invoked operator tool, separate from FastAPI. It provisions
the scanner schema introduced at `c3a7e9b120df`, verified at current application
head `f6c1a8d420be`; it never runs
migrations, creates staff accounts, grants privileges, or changes inventory.
Only the already deployed `inventory_nfc_demo` database is supported.

## Commands

From the repository root, in a private interactive terminal:

```powershell
python scripts/provision_equipment_scanner.py register --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --administrator postgres --schema public

python scripts/provision_equipment_scanner.py start-session --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --administrator postgres --schema public --scanner-id <SCANNER_UUID> --staff-user-id <STAFF_UUID> --minutes 15

python scripts/provision_equipment_scanner.py renew-session --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --administrator postgres --schema public --scanner-id <SCANNER_UUID> --staff-user-id <STAFF_UUID> --expected-session-id <CURRENT_SESSION_UUID> --minutes 15

python scripts/provision_equipment_scanner.py revoke --database inventory_nfc_demo --server-address 127.0.0.1 --port 5432 --administrator postgres --schema public --scanner-id <SCANNER_UUID>
```

Replace angle-bracket placeholders with UUIDs and `postgres` with the independent
trusted deployment superuser actually used for this installation. The operator
must already know the authorized staff UUID; this tool does not enumerate users.
Only existing active FACULTY and LAB_ASSISTANT users can own a session. STUDENT,
ADMIN, inactive and nonexistent identities are rejected.

All operations require typing the exact operation/target confirmation printed by
the tool, followed by a hidden administrator password prompt. No password, device
key, or capture key argument or environment fallback exists. The tool verifies
the database, session/current role, superuser status, server IP/port, schema and
search path, READ COMMITTED isolation, PostgreSQL 18.4+ within major 18, complete
application revision and existing ownership/privilege/history protections.
It accepts explicit loopback IPs only, never a hostname or remote address.

Add `--dry-run` to validate identity, deployment and operation state within a
read-only transaction. Dry runs still require confirmation/password but generate
no UUIDs or secrets and perform no persistent writes. They cannot reserve a future
binding; a subsequent real invocation rechecks everything under locks.

## Credentials and handoff

Registration produces a random UUID and independent 32-byte CSPRNG device secret
encoded as canonical, unpadded URL-safe Base64 (43 characters). Only its SHA-256
digest is stored. After commit the terminal displays exactly one credential:

```text
Authorization: Scanner <scanner_uuid>.<device_secret>
```

Securely provision that device credential onto the intended ESP32 using the
hardware team's trusted setup process. Firmware changes are outside this step;
existing Serial-only firmware does not automatically receive credentials or send
HTTP. Never place a real credential in a source file, command line, Git, screenshot,
ticket, terminal transcript, or shared log. Keep database administrator credentials
off the ESP32. Terminal redirection/pipes are refused, but the operator must also
disable terminal recording, PowerShell transcription and screen capture. Python
cannot guarantee erasure of secrets from process memory; restrict debugging/core
dumps and operator-host access.

Session creation/renewal displays a new session UUID, owner UUID, expiry, and one
`X-Scan-Session-Token` value after commit. Supply **only the session UUID**, along
with the persistent device credential, to the ESP32 via a trusted setup channel.
Deliver the capture secret and session UUID privately to the selected staff
member's authenticated dashboard session. Do not give the capture secret to the
device or other users. There is no automatic browser pairing or delivery API yet;
these handoffs are manual and must not be treated as an implemented UI feature.

The existing contracts are unchanged:

- ESP32: `POST /device-scans`, device Authorization header, JSON
  `{"scan_id":"<new UUID>","capture_session_id":"<session UUID>","sequence":1,"uid":"04A1B2C3"}`.
  Sequence numbers increase across sessions, renewals and device restarts. Persist
  the high-water mark on the device; after a rejected scan do not reuse its sequence
  for a different payload. Retry an uncertain request with the original payload.
- Staff: `POST /scan-sessions/<session UUID>/events`, normal staff bearer
  authentication plus `X-Scan-Session-Token`, JSON
  `{"after_sequence":0,"limit":25}`. Existing owner/role/secret checks still apply;
  the token alone cannot grant access and there is no global latest-scan endpoint.
- Scans resolve existing equipment only. Staff separately resolves the borrower
  and explicitly confirms checkout through existing APIs.

## Lifecycle and failure handling

Sessions last 1–15 minutes (default 15), measured using the database clock. Starting
over an unexpired binding is refused. Renew using the current session UUID; this
replaces the binding and capture secret, including when transferring ownership to
another authorized staff member. Old secrets and delayed old-session device
payloads immediately cease to authorize access. Update the device session UUID and
staff capture credential after renewal. Retained events are unchanged but old
session events cannot be read using the replacement session. Existing 120-second
TTL and bounded lazy cleanup remain in force.

Revocation is idempotent: it disables the scanner and clears all five capture
fields. No reactivation/key-recovery command is provided. Replacing a revoked or
lost device requires a new registration and secure provisioning. No operation
resets sequence/rate counters, deletes scan events, modifies borrowers, equipment
or checkout history, or changes runtime role grants.

Provisioning uses the existing transaction-scoped deployment advisory lock and a
scanner row lock; staff eligibility is locked during binding. Concurrent deployment
or provisioning fails safely; renewal additionally compares the expected current
session UUID so stale commands cannot overwrite a new binding. The global lock is
intentional for this low-volume operator workflow. Driver connection, statement and
lock timeouts are inherited from the deployment runner (10s, 120s, 10s).

No credential is printed on failure or before a commit acknowledgement. Errors are
generic and omit driver text/parameters. **Do not automatically retry**: a lost
commit acknowledgement can mean the write succeeded but the credential was never
displayed. Have the trusted administrator reconcile registry state, revoke an
unrecoverable scanner or renew an unrecoverable binding before continuing. There
is no plaintext secret recovery. Keep the terminal available until output is shown;
a terminal failure after commit has the same recovery requirement.

## Deployment boundary

PostgreSQL remains localhost-only. ESP32 devices communicate with FastAPI over
Wi-Fi/LAN and never with PostgreSQL. Restrict the API listening interface/firewall
to the intended network. Plain HTTP is only suitable for a trusted local-network
demonstration; production requires TLS, including correct device certificate
validation. Proxies and infrastructure must redact authorization/session headers
and avoid body/credential capture. This CLI makes no firewall, database-listener,
TLS or firmware configuration changes.

## Verification

Database-free tests: `backend/tests/test_scanner_provisioning.py`. They mock all
connections and cover validation, digests, roles, locks, dry runs, renewal,
revocation, rollback/commit errors and terminal credential output.
Run with the project's Python environment and an external timeout, for example:

```text
python -m pytest backend/tests/test_scanner_provisioning.py backend/tests/test_scanner_ingestion.py backend/tests/test_nfc_deployment_runner.py
```

The new PostgreSQL tests are
`infra/nfc_postgres/tests/test_scanner_provisioning.py`. They are discovered by the
existing disposable integration harness and exercise the real routine/privilege
contracts, renewal, revocation, competing transactions and preservation. Run them
only through `scripts/run_disposable_docker_tests.ps1` after its isolation checks,
with an external process timeout. Never run the integration file directly against
a host database.

The project owner reports final isolated verification: **312 passed, 0 failed,
0 skipped, exit code 0**, including scanner security and fresh/populated upgrades
through `f6c1a8d420be`. Cleanup succeeded; protected host databases were untouched.
No PostgreSQL verification was rerun during handover. See [handover](BACKEND_HANDOVER.md).
