<#
.SYNOPSIS
    Runs isolated integration tests in a dedicated, disposable Docker PostgreSQL container.
    Guarantees zero connection to host databases (inventory_db, inventory_checkout_test, inventory_nfc_demo).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

Write-Host "=== PHASE A: SAFETY PRECHECK ===" -ForegroundColor Cyan

# 1. Verify Docker CLI and Engine availability
try {
    $dockerContext = (docker context show).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $dockerContext) {
        throw "Docker Engine context lookup failed."
    }
} catch {
    Write-Error "Docker Engine is unavailable. Integration tests require an isolated Docker container."
    exit 1
}

# 2. Generate unique disposable project ID
$nfcTestProject = 'nfc-it-' + [guid]::NewGuid().ToString('N')
Write-Host "Assigned Unique Disposable Project ID: $nfcTestProject" -ForegroundColor Green
Write-Host "Verified Docker Context: $dockerContext" -ForegroundColor Green

# 3. Explicit Disposable Opt-in environment variable
$env:NFC_DISPOSABLE_OPT_IN = 'YES_ISOLATED_PG18'

# 4. Verify Compose configuration and ensure compose.defaults is used (no .env leakage)
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose configuration validation failed."
}

Write-Host "`n=== BUILDING DISPOSABLE TEST CONTAINERS ===" -ForegroundColor Cyan
docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml build
if ($LASTEXITCODE -ne 0) {
    throw "Building isolated test images failed."
}

Write-Host "`n=== EXECUTING ISOLATED INTEGRATION TESTS ===" -ForegroundColor Cyan
$testRunOutput = docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml up --abort-on-container-exit --exit-code-from worker
$runExitCode = $LASTEXITCODE

Write-Host "`nRaw Worker Output:" -ForegroundColor Yellow
$testRunOutput | ForEach-Object { Write-Host $_ }

Write-Host "`n=== PHASE D: SCOPED CLEANUP ===" -ForegroundColor Cyan
try {
    if ($nfcTestProject -notmatch '^nfc-it-[a-f0-9]{32}$' -or -not $dockerContext) {
        throw "Abort cleanup: saved project and context required"
    }

    $currentContext = (docker context show).Trim()
    if ($LASTEXITCODE -ne 0 -or $currentContext -ne $dockerContext) {
        throw "Abort cleanup: Docker context mismatch"
    }

    # Inspect containers for strict project ownership
    $containers = @(docker ps -aq --filter "label=com.docker.compose.project=$nfcTestProject")
    foreach ($cid in $containers) {
        if ($cid) {
            $labelJson = docker inspect --format '{{json .Config.Labels}}' $cid
            $labels = $labelJson | ConvertFrom-Json
            if ($labels.'com.docker.compose.project' -ne $nfcTestProject -or
                $labels.'org.inventory.harness' -ne 'nfc-disposable') {
                throw "Abort cleanup: unexpected container label in $cid"
            }
        }
    }

    # Clean up only this project and its tmpfs identity volume
    docker compose --env-file infra/nfc_postgres/compose.defaults -p $nfcTestProject -f infra/nfc_postgres/compose.yaml down --volumes
    Write-Host "Successfully stopped and removed disposable container and volume." -ForegroundColor Green
} finally {
    Remove-Item Env:NFC_DISPOSABLE_OPT_IN -ErrorAction SilentlyContinue
}

exit $runExitCode

