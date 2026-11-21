<# Runs only the reviewed disposable Docker harness; never host PostgreSQL. #>
[CmdletBinding()]
param(
    [ValidateRange(1, 86400)][int]$BuildTimeoutSeconds = 900,
    [ValidateRange(1, 86400)][int]$TestTimeoutSeconds = 1800,
    [ValidateRange(1, 86400)][int]$CleanupTimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$runnerPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $runnerPython -PathType Leaf)) {
    throw 'Project Python environment required; no PostgreSQL fallback is permitted.'
}
& $runnerPython -B -m infra.nfc_postgres.docker_runner --build-timeout $BuildTimeoutSeconds --test-timeout $TestTimeoutSeconds --cleanup-timeout $CleanupTimeoutSeconds
exit $LASTEXITCODE
