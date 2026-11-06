[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Test = 'backend/tests/integration'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The project Python virtual environment was not found.'
}

$runner = @"
import getpass
import os
from pathlib import Path
import sys
import warnings
from unittest.mock import patch

root = Path.cwd()
suite = (root / "backend/tests/integration").resolve()
selection, separator, node = sys.argv[1].partition("::")
path = (root / selection).resolve()
if path != suite and (
    not path.is_relative_to(suite) or not path.is_file() or path.suffix != ".py"
):
    raise SystemExit("Select the integration suite, a test file, or a test node within it.")
if separator and (path == suite or not node):
    raise SystemExit("A test node requires an integration test file and a test name.")
target = str(path.relative_to(root))
if separator:
    target += separator + node

sys.path.insert(0, str(root))
os.environ["PYTHONPATH"] = str(root)
os.environ["INVENTORY_APP_PASSWORD"] = "unused-integration-placeholder"
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
for key in (
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG",
    "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS",
):
    os.environ.pop(key, None)

from sqlalchemy.engine import URL
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("error", getpass.GetPassWarning)
    try:
        password = getpass.getpass("Dedicated PostgreSQL test password: ")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise SystemExit("Secure password entry canceled or unavailable.")
if not password:
    raise SystemExit("A dedicated test password is required.")

os.environ["INVENTORY_TEST_DATABASE_URL"] = URL.create(
    "postgresql+psycopg",
    username="inventory_checkout_test_login",
    password=password,
    host="127.0.0.1",
    port=5432,
    database="inventory_checkout_test",
).render_as_string(hide_password=False)
del password

INTERNAL_ERROR_MESSAGE = "INTERNALERROR: Pytest encountered an internal error. Details hidden."


def report_internal_error(config, excinfo, option=None):
    # Config.notify_exception otherwise formats the raw exception before
    # handing it to terminal reporters, regardless of --tb and -r.
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(INTERNAL_ERROR_MESSAGE, file=sys.stderr)
    else:
        reporter.write_line(INTERNAL_ERROR_MESSAGE)


try:
    # Keep progress and totals, but omit failure text and captured output.
    with patch.object(pytest.Config, "notify_exception", report_internal_error):
        result = pytest.main([
            target, "-v", "-r", "N", "--tb=no", "--show-capture=no", "--disable-warnings",
            "-o", "addopts=", "-p", "no:cacheprovider", "-p", "no:logging",
        ])
except Exception:
    # A failure outside pytest's internal reporter can still escape pytest.main.
    print(INTERNAL_ERROR_MESSAGE, file=sys.stderr)
    result = pytest.ExitCode.INTERNAL_ERROR
finally:
    os.environ.pop("INVENTORY_TEST_DATABASE_URL", None)

raise SystemExit(result)
"@

$tempFile = New-TemporaryFile
try {
    Set-Content -LiteralPath $tempFile.FullName -Value $runner -Encoding utf8
    Push-Location -LiteralPath $repoRoot
    try {
        # Preserve the native exit code even if the caller opted into PS 7 errors.
        $PSNativeCommandUseErrorActionPreference = $false
        & $python -E $tempFile.FullName $Test
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item -LiteralPath $tempFile.FullName -ErrorAction Stop
}
exit $exitCode
