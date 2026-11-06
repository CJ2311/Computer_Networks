"""Exercise the PowerShell runner's Python payload without a database."""
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("failure_mode", ["terminal", "no-terminal", "escaped"])
def test_runner_reports_internal_error_without_leaking_credentials(failure_mode):
    harness = r"""
import getpass
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

source = Path("scripts/run_integration_tests.ps1").read_text(encoding="utf-8-sig")
payload = source.split('$runner = @"\n', 1)[1].split('\n"@', 1)[0]
real_main = pytest.main

def sensitive_error():
    return RuntimeError("SYNTHETIC_DRIVER_ERROR " + os.environ["INVENTORY_TEST_DATABASE_URL"])


class InternalFailure:
    def pytest_sessionstart(self, session):
        if sys.argv[2] == "no-terminal":
            session.config.pluginmanager.unregister(name="terminalreporter")
        raise sensitive_error()

def trigger_internal_error(args):
    if sys.argv[2] == "escaped":
        raise sensitive_error()
    return real_main(args, plugins=[InternalFailure()])

with patch.object(getpass, "getpass", return_value="SYNTHETIC_PASSWORD"), \
        patch.object(pytest, "main", side_effect=trigger_internal_error):
    exec(compile(payload, "<integration runner>", "exec"), {})
"""
    result = subprocess.run(
        [sys.executable, "-B", "-", "backend/tests/integration", failure_mode],
        input=harness, capture_output=True, text=True, cwd=ROOT, timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.INTERNAL_ERROR
    assert output.count("INTERNALERROR: Pytest encountered an internal error. Details hidden.") == 1
    for sensitive in ("SYNTHETIC_DRIVER_ERROR", "SYNTHETIC_PASSWORD", "postgresql+psycopg://"):
        assert sensitive not in output
