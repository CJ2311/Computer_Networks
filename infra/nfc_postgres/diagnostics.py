"""Structured diagnostics: allowlisted identifiers, never exception rendering."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
FILES = ["infra/nfc_postgres/harness.py", "infra/nfc_postgres/worker.py",
         "infra/nfc_postgres/diagnostics.py", "infra/nfc_postgres/tests/conftest.py",
         "infra/nfc_postgres/tests/test_deployment.py", "scripts/deploy_nfc_demo.py",
         "backend/alembic/safety.py", "backend/alembic_nfc_demo/env.py"]
FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "backend/alembic/versions").glob("*.py")]
FILES += [
    "backend/alembic/application_safety.py",
    "infra/nfc_postgres/tests/test_operations.py",
    "infra/nfc_postgres/tests/test_missing_equipment.py",
    "infra/nfc_postgres/tests/test_inventory_reports.py",
]
FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "backend/alembic/application_versions").glob("*.py")]
SAFE_FILES = set(FILES)
SAFE_REASONS = set()
SAFE_TESTS = set()
for name in FILES:
    path = ROOT / name
    if not path.is_file():
        continue
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            value = node.exc.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                SAFE_REASONS.add(value.value)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            SAFE_TESTS.add(node.name)


def failure(excinfo, nodeid, phase):
    """No str/repr(exc), SQL, locals, source lines, captures or parameter IDs."""
    name = nodeid.split("::")[-1].split("[")[0]
    record = {"test": name if name in SAFE_TESTS else "unknown_test",
              "phase": phase if phase in {"setup", "call", "teardown"} else "unknown",
              "reason": "unclassified", "frames": []}
    error = excinfo.value
    classes = {"AssertionError", "RuntimeError", "DeploymentError", "IsolationError", "ProgrammingError",
               "OperationalError", "IntegrityError", "InterfaceError", "ConnectionError", "TimeoutError",
               "LostCommitAcknowledgement", "PermissionError", "FileNotFoundError", "ValueError", "TypeError"}
    record["exception"] = type(error).__name__ if type(error).__name__ in classes else "OtherError"
    if error.args and isinstance(error.args[0], str) and error.args[0] in SAFE_REASONS:
        record["reason"] = next(reason for reason in SAFE_REASONS if reason == error.args[0])
    original = getattr(error, "orig", error)
    state = getattr(original, "sqlstate", None)
    if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
        record["sqlstate"] = state
    for frame in excinfo.traceback:
        try:
            path = Path(str(frame.path)).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if path in SAFE_FILES:
            record["frames"].append({"file": path, "line": int(frame.lineno) + 1})
    record["frames"] = record["frames"][-8:]
    return record
