# College authentication configuration

The backend verifies access tokens; it does not invent a college login URL, issue college tokens, or implement a browser SSO flow.
College IT must approve the identity integration before production use. Default authentication remains denied.

## Approved integration boundary

Set `APP_ENV=production`, `DEV_AUTH_ENABLED=false`, `AUTH_MODE=college_introspection` in deployment configuration.
Supply the following through the deployment secret/configuration system, never committed files or CLI arguments:

| Setting | Required value supplied by college IT |
|---|---|
| `COLLEGE_INTROSPECTION_URL` | Exact HTTPS RFC 7662 introspection endpoint, without embedded credentials/query/fragment |
| `COLLEGE_ISSUER` | Exact expected issuer claim |
| `COLLEGE_AUDIENCE` | Audience registered specifically for this inventory API |
| `COLLEGE_CLIENT_ID` | Authorized introspection service client |
| `COLLEGE_CLIENT_SECRET` | Service credential, injected securely |
| `COLLEGE_REQUIRED_SCOPES` | Optional space-separated required API scopes |

Client authentication uses RFC 6749 HTTP Basic with each component form-encoded. TLS uses the OS/Python trust store;
certificate verification cannot be disabled. Redirects are rejected. Requests time out after five seconds and responses
are limited to 16 KiB. This boundary does not support mTLS-only or JWT-client-assertion-only providers yet.
IT must confirm that the endpoint supports this contract; do not substitute an unverified college URL.

An accepted response requires HTTP 200, JSON `active: true`, exact `iss`, matching `aud` string/list, nonempty `sub`,
`token_type: Bearer`, finite future `exp`, valid optional `nbf`, and configured scopes. Missing claims fail closed.
Provider errors produce generic 503 responses without exposing tokens, response bodies or client credentials.
Invalid tokens return 401; unmapped/inactive users return 403. There is no development/opaque-token fallback.

## Identity provisioning and roles

A trusted administrator provisions `external_identities(issuer, subject, user_id)` after verifying the student's/staff
identity with IT. `issuer + subject` is unique and must reference an existing user. The runtime role has SELECT only
on this mapping; it cannot enroll identities, edit roles, or activate users. No automatic email-based account linking
or just-in-time role provisioning occurs. Tokens' role/department/email claims never confer local privileges.
Roles and activation are read from `users` on every request and rechecked by write services.

The college browser client still needs IT-approved OAuth client registration, redirect URIs, authorization-code/PKCE
flow and access-token storage policy. Those endpoints/credentials are external dependencies, not implementation defaults.
College logout/token revocation is handled by the provider; no backend login/logout endpoint is claimed.

## Existing modes

`AUTH_MODE=opaque_token` remains an explicit internal/demo adapter using `AUTH_TOKEN_SHA256_MAP` (SHA-256 digests to
existing college IDs). Provision per-user tokens from at least 32 random bytes, base64url without padding. It has no
automatic token expiry and is not the recommended college production identity solution. Never put raw tokens in source,
URLs, logs, browser bundles or localStorage by default. Configuration changes must reach every API process.
With no AUTH_MODE, fictional development headers require both `APP_ENV=development`, `DEV_AUTH_ENABLED=true` and a
loopback client. Never expose that mode through a proxy. College-mode failures cannot enter either alternative mode.

## Authorization limits

Staff policies remain laboratory-wide. No Lab/StaffLabMembership/Equipment.lab_id relation exists; location text is not
an authorization boundary. Assigned-lab enforcement requires an agreed lab model and roster/membership source, followed
by consistent filtering of every asset, loan, maintenance and audit operation. Do not deploy this as multi-lab isolation.
Students can read equipment and only their own individual/quantity borrowing histories. Staff-only lookup endpoints
expose active students' ID/college ID/name, without email. Frontend visibility never replaces backend authorization.
