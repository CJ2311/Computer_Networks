"""Explicit container-only test entry point; never emits driver exceptions."""
import contextlib
import io
import json
import logging
import sys
import pytest

from infra.nfc_postgres.harness import require_opt_in
from infra.nfc_postgres.diagnostics import failure


class Counts:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0
        self.failures = []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.failed and call.excinfo is not None:
            self.failures.append(failure(call.excinfo, item.nodeid, report.when))

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed += 1
        elif report.skipped:
            self.skipped += 1
        elif report.when == "call":
            self.passed += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.failures.append({"test": "collection", "phase": "collection", "reason": "collection_failed"})


def main():
    logging.disable(logging.CRITICAL)
    try:
        require_opt_in()
        import pytest
        counts = Counts()
        # Full traceback/captured output can contain generated SQL credentials.
        # Keep all pytest text in memory and output counts only, even on failure.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = pytest.main(["infra/nfc_postgres/tests", "-p", "no:terminal", "--assert=plain",
                                  "-p", "no:cacheprovider"], plugins=[counts])
        print(json.dumps(dict(passed=counts.passed, failed=counts.failed, skipped=counts.skipped,
                              exit_code=int(result), failures=counts.failures)))
        return int(result)
    except BaseException:
        print(json.dumps({"exit_code": 1, "failures": [failure(pytest.ExceptionInfo.from_current(), "startup", "setup")]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
