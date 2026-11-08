# Disposable PostgreSQL tests for the NFC deployment runner

Implementation only. Do not execute the commands below until separately approved.
Docker CLI was not found during implementation; the daemon and image availability
were not checked. No containers, volumes, PostgreSQL connections or integration
tests were started. PostgreSQL 18.4 is intentionally exact: never replace a failed
image pull with latest or point this harness at the existing server.

## Prerequisites

- Docker Engine/Desktop running Linux containers, Compose v2 with conditional
  depends_on support, and enough memory (approximately 1.5 GB plus images).
- Ability to build python:3.12-slim-bookworm and postgres:18.4-bookworm images.
  Image tags and Python dependencies are pinned to versions, not content digests;
  verify image provenance and pin approved digests before security-sensitive use.
- A trusted Docker host/context. A separate VM/daemon gives the strongest boundary.
- Network access for image pulls and pip during build only. Runtime is isolated.
- Run from the repository root. No external credentials or .env are needed.

## Isolation and opt-in

PostgreSQL uses network_mode: none: its network namespace has only loopback,
with no bridge interface, gateway or route to the host, LAN or internet. It
listens only on 127.0.0.1 inside that namespace. The worker uses
network_mode: service:postgres, sharing that same loopback-only namespace, so the
unmodified runner reaches the disposable server at 127.0.0.1:5432. Container
loopback is not the host's loopback. Compose declares no bridge network and
publishes no ports. No host networking, host gateway entries, host data mounts,
existing network, Docker socket, PostgreSQL credentials, or root .env are used.
Both services explicitly select their network mode, avoiding an implicit default
Compose network. The worker drops all capabilities and cannot configure a new
network interface or route. Docker/host administrators can still change container
configuration or enter namespaces; they remain trusted.

All required Python dependencies are installed by pip during image build. The
runtime entry point invokes only the installed Python test worker and requires
no downloads, DNS, external APIs or internet access. Image pulls and dependency
installation require build-time access and are separate from runtime isolation.
This configuration is statically checked; actual namespace/interface behavior
still needs verification during a separately approved container startup.
A Dockerfile-specific allowlist excludes credentials from both build contexts. The
explicit empty compose.defaults file also prevents automatic root .env discovery.
The worker copies code into its image; it does not bind-mount the workspace.

PostgreSQL data is a container tmpfs at /var/lib/postgresql. Generated bootstrap
credentials and the run UUID occupy a project-owned, memory-backed tmpfs volume.
The worker mounts that secret volume read-only; neither service logs passwords.
The worker runs as nfc-worker (UID/GID 18085), with only supplementary secrets
GID 18084. Both images create the nfc-secrets group at that GID; allocation
collisions fail the build instead of reusing an unrelated account. Compose pins
the worker identity and supplementary group; its capability drop, read-only
filesystem and no-new-privileges restrictions remain in force.

Bootstrap makes the identity directory postgres:18084 mode 0750 and both identity
files postgres:18084 mode 0440. PostgreSQL can read them after dropping root;
the worker can traverse/read through its dedicated group but cannot write,
chmod, or replace them. Other UIDs/groups have no access. Mode 0400 would deny
the distinct worker identity and would require another credential copy or shared
OS identity, so 0440 is the minimal shared-read mode here. The harness rejects
root/wrong worker identities, symlinks, unexpected ownership/group, and broader
file permissions before reading credential contents. Existing identity volumes
are not repaired or reused; a fresh run requires separately approved resources.

Bootstrap clears inherited password environment variables and hands only the
generated POSTGRES_PASSWORD_FILE path to the official entrypoint. Upstream
loads the file and supplies PGPASSWORD for local SCRAM initialization. The only
installed init hook, 99-nfc-clear-init-passwords.sh, is explicitly non-executable
(0644), so upstream sources it in its own shell. It clears PGPASSWORD,
POSTGRES_PASSWORD and POSTGRES_PASSWORD_FILE before final server handoff. No
later initialization SQL may be added after this hook; the image build rejects
unexpected pre-existing initialization scripts. Initialization and its temporary
server still handle credentials; the long-running server must not inherit them.
This lifecycle is tested with a stubbed entrypoint, not a running PostgreSQL image.

The PostgreSQL 18 data ancestors remain postgres-owned mode 0700. Startup
diagnostics at /run/nfc-startup-status are root-private and contain only a fixed
stage name and, on bootstrap-shell exit, a numeric exit code. No command output,
environment, passwords, UUIDs or connection details enter this file. A final
postgres-entrypoint stage is only a handoff marker: it does not prove startup
success or record an exit after exec replaces the bootstrap shell.

Database logging is disabled in Docker, and worker output is counts only. Test
tracebacks and captures are deliberately withheld because driver errors can carry
SQL literals. Diagnostic text stays in worker memory and is discarded on exit.
Host root/Docker administrators can still inspect process memory or tmpfs files.

The required opt-in value is NFC_DISPOSABLE_OPT_IN=YES_ISOLATED_PG18. The bootstrap
script, worker entry point and pytest fixtures all enforce it. Fixtures also
require a Docker-container marker and reject ambient PG* settings. Direct pytest
collection without opt-in skips these tests before any fixture connects. These
checks are protection against accidents, not against a hostile Docker operator.

The instance contains its own database named inventory_nfc_demo. It is unrelated
to the existing database with that name. inventory_db and inventory_checkout_test
are never created or accessed. Roles are created only in this new cluster:

- nfc_controller: bootstrap/failure-injection superuser; runner administrator stage.
- inventory_migrator: LOGIN, CONNECT and public USAGE/CREATE, no elevated attributes.
- inventory_app: LOGIN, CONNECT and public USAGE; narrow test grants as needed.
- inventory_metadata_audit_owner/writer: NOLOGIN, no memberships/elevated attributes.

The controller checks the exact server version, address/port, database, identities,
cluster_name carrying the random UUID, pg_control_system system identifier and
expected data-directory prefix before destructive operations. Database reset also
requires its exact per-run ownership comment. Backend termination is limited to
captured test PIDs in inventory_nfc_demo with the runner's application_name.
Bootstrap rejects pre-existing user roles or non-template databases. Restarting
a stopped PostgreSQL service with an old identity volume fails closed: use a new
project rather than reusing credentials against fresh tmpfs data.

## Proposed commands for later approved execution (PowerShell)

Use a fresh project name and retain it until cleanup. The value below contains
no credential. Do not reuse another application's Compose project name.

```powershell
$env:NFC_DISPOSABLE_OPT_IN = 'YES_ISOLATED_PG18'
$nfcTestProject = 'nfc-it-' + [guid]::NewGuid().ToString('N')
$nfcTestContext = docker context show
if ($LASTEXITCODE -ne 0) { throw 'Docker context lookup failed' }
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml config --quiet
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml build
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml up --abort-on-container-exit --exit-code-from worker
```

The worker bootstraps fresh roles, resets only its marked disposable database
between tests, and runs infra/nfc_postgres/tests. No production runner flags are
added. All migration execution uses the dedicated demo environment and unchanged
seven revisions. Successful output is JSON with passed/failed/skipped counts and
an exit code. A collection/setup failure can yield a nonzero exit code even if
failed=0. Do not infer success without checking exit_code and skipped counts.

## Test coverage

### Credential-safe startup diagnosis

The PostgreSQL image writes `/run/nfc-startup-status` in its container writable
layer. It contains only a fixed stage label and, if the bootstrap exits before
handoff, its numeric exit code. Docker logging remains `none`. After verifying
the exact container's project and harness labels as described below, read this
single file from a stopped container with:

```powershell
docker cp "${containerId}:/run/nfc-startup-status" ./nfc-startup-status.txt
Get-Content ./nfc-startup-status.txt
```

This does not start the container or access the identity volume. Do not copy the
identity directory or dump container environments. Missing status means the
bootstrap did not reach its first write, or the diagnostic path was unwritable.
`postgres-entrypoint` means handoff was reached; it does **not** mean PostgreSQL
initialized successfully and cannot distinguish failures inside the upstream
entrypoint. Bootstrap labels identify password write/validation, UUID write,
identity permissions, data-directory preparation, and reuse/opt-in checks without
recording passwords, UUIDs, connection strings, command output, or SQL.

The PG18 tmpfs mount hides image ownership. Bootstrap prepares both
`/var/lib/postgresql` and its `18` ancestor as postgres-owned `0700` directories
before the upstream privilege drop; upstream only fixes ownership of PGDATA.
Local SCRAM initialization also requires `PGPASSWORD` for upstream's socket-based
psql calls. Upstream supplies it from the generated file during initialization;
the sourced final hook clears it and `POSTGRES_PASSWORD` before the long-running
server handoff. Temporary initialization processes still handle these secrets;
trusted Docker administrators can inspect their memory and the identity volume.
Rebuild the PostgreSQL image to incorporate bootstrap changes. Use a fresh
identity volume for any later authorized runtime test; partial credentials now
also fail reuse checks. These changes have not been runtime-validated with Docker.

- Full staged API deployment and verify-only rerun, plus an unchanged interactive
  CLI deployment through a pseudo-terminal. Passwords are fed only via hidden
  prompts or in-memory callbacks, never arguments or generated source files.
- Real transaction-lock contention, including migrator versus administrator.
  Paused real migration stages prove contenders fail before concurrent execution.
  Tests target the runner's current nonblocking behavior; if acquisition changes
  to blocking, update expectations to prove waiting rather than parallel entry.
- Stage-backend termination after actual CREATE TABLE, DDL/version rollback,
  lock release, and subsequent successful resumption. The controller needs
  superuser to terminate the administrator's superuser backend.
- Client exceptions after real DDL and transaction rollback.
- Protected table/function ownership and live runtime denial tests, plus a real
  metadata change that creates an audit event through the approved generator.
- Authentic catalog acceptance and rejection of WHEN(false), UPDATE OF, disabled
  and deferred metadata triggers. Mutations occur only on disposable fixtures.
- Recovery from both intermediate checkpoints; a second deployment completing
  during an administrator password prompt makes the stale deployment abort.
- Ambiguous commit outcome: a driver wrapper performs a real COMMIT, then hides
  its acknowledgement. Rerun recognizes committed state instead of replaying.
- Unsafe target/configuration and foreign-instance identity rejection.

The ambiguous-commit case is deterministic client-boundary fault injection. It
is not a test of packets lost in transit, SSL, network partition timing, server
crash recovery or host failure. Those require a separate authorized fault-injection
VM/network setup; none is claimed here. The worker may find previously unknown
reflection/ACL/ownership conflicts: these integration tests have NOT been run.

## Cleanup limited to this project

Cleanup MUST first verify the saved unique project name, original Docker context,
and both ownership labels on every discovered container and volume. A filtered
resource list alone is not sufficient verification. Abort cleanup on any missing
or mismatched label, unexpected service/volume, unexpected project network, failed
inspection, or missing saved project/context. Do not guess replacement values.
There should be no project-owned Docker networks in this architecture.

The following proposed commands verify labels before allowing project cleanup;
they do not constitute permission to execute cleanup now:

```powershell
if ($nfcTestProject -notmatch '^nfc-it-[a-f0-9]{32}$' -or -not $nfcTestContext) {
    throw 'Abort cleanup: saved project and context required'
}
$currentContext = docker context show
if ($LASTEXITCODE -ne 0 -or $currentContext -ne $nfcTestContext) {
    throw 'Abort cleanup: Docker context mismatch'
}
$nfcContainers = @(docker ps -aq --filter "label=com.docker.compose.project=$nfcTestProject")
if ($LASTEXITCODE -ne 0) { throw 'Abort cleanup: container lookup failed' }
foreach ($containerId in $nfcContainers) {
    $labelJson = docker inspect --format '{{json .Config.Labels}}' $containerId
    if ($LASTEXITCODE -ne 0) { throw 'Abort cleanup: container inspection failed' }
    $labels = $labelJson | ConvertFrom-Json -ErrorAction Stop
    if ($labels.'com.docker.compose.project' -ne $nfcTestProject -or
        $labels.'org.inventory.harness' -ne 'nfc-disposable' -or
        $labels.'com.docker.compose.service' -notin @('postgres', 'worker')) {
        throw 'Abort cleanup: unexpected container ownership'
    }
}
$nfcVolumes = @(docker volume ls -q --filter "label=com.docker.compose.project=$nfcTestProject")
if ($LASTEXITCODE -ne 0) { throw 'Abort cleanup: volume lookup failed' }
foreach ($volumeName in $nfcVolumes) {
    $labelJson = docker volume inspect --format '{{json .Labels}}' $volumeName
    if ($LASTEXITCODE -ne 0) { throw 'Abort cleanup: volume inspection failed' }
    $labels = $labelJson | ConvertFrom-Json -ErrorAction Stop
    if ($labels.'com.docker.compose.project' -ne $nfcTestProject -or
        $labels.'org.inventory.harness' -ne 'nfc-disposable' -or
        $labels.'com.docker.compose.volume' -ne 'identity') {
        throw 'Abort cleanup: unexpected volume ownership'
    }
}
$nfcNetworks = @(docker network ls -q --filter "label=com.docker.compose.project=$nfcTestProject")
if ($LASTEXITCODE -ne 0 -or $nfcNetworks.Count -ne 0) {
    throw 'Abort cleanup: unexpected project network or lookup failure'
}
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml down --volumes
if ($LASTEXITCODE -ne 0) { throw 'Project cleanup failed; review remaining resources' }
Remove-Item Env:NFC_DISPOSABLE_OPT_IN
```

Do not use docker system prune, wildcard volume deletion, external-volume names,
or DROP DATABASE/ROLE commands against a host PostgreSQL server. Compose down
with the verified exact unique project removes its containers and identity
volume. No project network is expected. PostgreSQL tmpfs disappears with the container.
Build images/cache remain unless separately identified and approved for removal.
A failed run needs the same scoped cleanup. Never "repair" it by changing roles
or permissions on the existing PostgreSQL server.
