"""Database-free scanner authentication, staging contracts and migration checks."""
import base64
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.app.auth.scanners import require_scanner, token_digest, ScannerCredential
from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db, get_inventory_write_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.request_limits import RequestSizeLimitMiddleware
from backend.app.schemas.scanners import ScanSubmit, ScanQuery
from backend.app.services import scanners
from backend.app.routers import scanners as routes
from backend.tests.inventory_session_stub import configure_session
from backend.tests.test_request_limits import request

KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
CAPTURE_KEY = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode()
migration = import_module("backend.alembic.application_versions.c3a7e9b120df_scanner_ingestion")


def payload(**changes):
    return {"scan_id": str(uuid4()), "capture_session_id": str(uuid4()), "sequence": 1, "uid": "04a1b2c3", **changes}


def credential_headers(scanner_id=None):
    return [(b"authorization", f"Scanner {scanner_id or uuid4()}.{KEY}".encode())]


@pytest.mark.parametrize("uid", ["04a1b2c3", "04112233445566", "00112233445566778899", " 04a1b2c3 "])
def test_uid_normalization_preserves_leading_zeroes(uid):
    assert ScanSubmit.model_validate(payload(uid=uid)).uid == uid.strip().upper()


@pytest.mark.parametrize("changes", [
    {"uid": "04:A1:B2:C3"}, {"uid": "04-A1-B2-C3"}, {"uid": "0x04A1B2C3"}, {"uid": "GG112233"},
    {"uid": "123456"}, {"uid": "A" * 21}, {"uid": "04A1\nB2C3"}, {"sequence": True}, {"sequence": 0},
    {"sequence": 2**63}, {"sequence": "1"}, {"scan_id": "bad"}, {"capture_session_id": "bad"},
    {"student_id": str(uuid4())}, {"role": "FACULTY"}, {"staff_user_id": str(uuid4())}, {"timestamp": "2099-01-01"},
])
def test_invalid_or_privilege_bearing_payloads_rejected(changes):
    with pytest.raises(ValidationError):
        ScanSubmit.model_validate(payload(**changes))


@pytest.mark.parametrize("value", ["DEV-FACULTY", "DEV-LAB_ASSISTANT", "DEV-STUDENT", "DEV-ADMIN", "Bearer " + KEY,
                                   "Scanner bad." + KEY, "Scanner " + str(uuid4()) + ".short"])
def test_human_credentials_cannot_authenticate_scanner(value):
    with pytest.raises(HTTPException) as error:
        require_scanner(Request({"type": "http", "headers": [(b"authorization", value.encode())]}))
    assert error.value.status_code == 401 and KEY not in error.value.detail


def test_scanner_credential_is_distinct_and_repr_hides_digest():
    scanner_id = uuid4()
    cred = require_scanner(Request({"type": "http", "headers": credential_headers(scanner_id) + [
        (b"x-forwarded-for", b"DEV-FACULTY"), (b"forwarded", b"for=DEV-ADMIN")]}))
    assert cred.scanner_id == scanner_id and cred.digest == token_digest(KEY)
    assert KEY not in repr(cred) and cred.digest not in repr(cred)
    for headers in (credential_headers(scanner_id) * 2, credential_headers(scanner_id) + [(b"x-dev-user", b"DEV-FACULTY")]):
        with pytest.raises(HTTPException) as error:
            require_scanner(Request({"type": "http", "headers": headers}))
        assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        token_digest("A" * 42 + "B")  # Noncanonical Base64 padding bits.


@pytest.fixture
def case(monkeypatch):
    db = Mock(spec=Session)
    db.info = {}
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.in_nested_transaction.return_value = False
    db.no_autoflush = nullcontext()
    configure_session(db, monkeypatch)
    db.get_bind.return_value.dialect = postgresql.dialect()
    actor = User(id=uuid4(), role="FACULTY", is_active=True)
    db.get.return_value = actor
    received = datetime.now(timezone.utc)
    data = ScanSubmit.model_validate(payload())
    receipt = {"scan_id": str(data.scan_id), "sequence": data.sequence, "uid": data.uid, "asset_id": str(uuid4()),
               "received_at": received.isoformat(), "expires_at": (received + timedelta(seconds=120)).isoformat(), "duplicate": False}
    return SimpleNamespace(db=db, actor=actor, data=data, receipt=receipt,
                           credential=ScannerCredential(uuid4(), token_digest(KEY)))


def test_ingestion_uses_only_protected_routine_and_hashed_credentials(case):
    case.db.scalar.side_effect = ["public", {"status": 200, "data": case.receipt}]
    outcome = scanners.ingest_scan(case.db, case.data, case.credential)
    assert str(outcome["data"].asset_id) == case.receipt["asset_id"]
    sql, values = case.db.scalar.call_args.args
    assert "public.scanner_ingest" in str(sql)
    assert values["uid"] == "04A1B2C3" and values["session"] == case.data.capture_session_id
    assert values["digest"] == token_digest(KEY) and KEY not in repr(values)
    case.db.get.assert_not_called()  # Device route does not resolve a faculty/student identity.
    case.db.add.assert_not_called()
    case.db.flush.assert_not_called()
    case.db.commit.assert_called_once()


@pytest.mark.parametrize("code", ["unknown_uid", "ambiguous_uid", "rate_limited", "scan_conflict", "capture_unavailable"])
def test_error_bookkeeping_commits_before_http_error(case, code):
    status, _ = scanners.ERRORS[code]
    case.db.scalar.side_effect = ["public", {"status": status, "error": code}]
    outcome = scanners.ingest_scan(case.db, case.data, case.credential)
    case.db.commit.assert_called_once()
    case.db.rollback.assert_not_called()
    with pytest.raises(HTTPException) as error:
        routes._response(outcome)
    assert error.value.status_code == status
    if status == 429:
        assert error.value.headers == {"Retry-After": "60"}


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_staff_read_service_rechecks_persisted_role(case, role):
    case.db.get.return_value = User(id=case.actor.id, role=role, is_active=True)
    with pytest.raises(HTTPException) as error:
        scanners.read_scans(case.db, uuid4(), ScanQuery(), case.actor, token_digest(CAPTURE_KEY))
    assert error.value.status_code == 403
    case.db.scalar.assert_not_called()
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("headers", [[], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")]])
@pytest.mark.parametrize("path", ["/device-scans", "/device-scans/"])
def test_scanner_size_limit_precedes_any_authentication_or_sql(monkeypatch, headers, path):
    service = Mock()
    monkeypatch.setattr(routes, "ingest_scan", service)
    assert request(app, [b"x" * 1024, b"y" * 1025], path=path, headers=headers)[0] == 413
    service.assert_not_called()


def test_scanner_limit_counts_exact_bytes_and_handles_proxy_root_path():
    async def echo(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)
    inner = AsyncMock(side_effect=echo)
    target = RequestSizeLimitMiddleware(inner, 4096, path_limits={"/device-scans": 2048})
    assert request(target, [b"x" * 2048], path="/device-scans")[0] == 200
    async def rooted(scope, receive, send):
        await target({**scope, "path": "/api/device-scans", "root_path": "/api"}, receive, send)
    assert request(rooted, [b"x" * 2049])[0] == 413


@pytest.mark.parametrize("role,expected", [("FACULTY", 200), ("LAB_ASSISTANT", 200), ("ADMIN", 403), ("STUDENT", 403)])
def test_scoped_staff_read_contract(monkeypatch, role, expected):
    actor = User(id=uuid4(), role=role, is_active=True)
    service = Mock(return_value={"status": 200, "data": {"items": [], "after_sequence": 0, "limit": 25, "has_more": False}})
    monkeypatch.setattr(routes, "read_scans", service)
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_current_user: lambda: actor,
                        get_inventory_write_db: lambda: Mock()})
    assert request(app, [b"{}"], path=f"/scan-sessions/{uuid4()}/events",
                   headers=[(b"x-scan-session-token", CAPTURE_KEY.encode())])[0] == expected
    if expected == 200:
        assert service.call_args.args[-1] == token_digest(CAPTURE_KEY)
    else:
        service.assert_not_called()


def test_invalid_json_and_uid_never_reach_ingestion(monkeypatch):
    service = Mock()
    monkeypatch.setattr(routes, "ingest_scan", service)
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_inventory_write_db: lambda: Mock()})
    for body in (b"{", json.dumps(payload(uid="bad")).encode()):
        assert request(app, [body], path="/device-scans", headers=credential_headers())[0] == 422
    service.assert_not_called()


def test_device_submission_does_not_use_oauth_and_logs_no_secrets(case, monkeypatch, caplog):
    from backend.app.auth import college
    monkeypatch.setattr(college, "introspect", Mock(side_effect=AssertionError("No identity provider for device scans")))
    monkeypatch.setattr(routes, "ingest_scan", Mock(return_value={"status": 200, "data": case.receipt}))
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_inventory_write_db: lambda: Mock()})
    caplog.set_level("INFO", logger="uvicorn.error")
    assert request(app, [case.data.model_dump_json().encode()], path="/device-scans", headers=credential_headers())[0] == 200
    assert "route=/device-scans" in caplog.text and "status=200" in caplog.text
    assert KEY not in caplog.text and case.credential.digest not in caplog.text and case.data.uid not in caplog.text
    caplog.clear()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_current_user: lambda: case.actor})
    monkeypatch.setattr(routes, "read_scans", Mock(return_value={"status": 200,
        "data": {"items": [], "after_sequence": 0, "limit": 25, "has_more": False}}))
    assert request(app, [b"{}"], path=f"/scan-sessions/{case.data.capture_session_id}/events", headers=[
        (b"authorization", b"Bearer private-faculty-credential"), (b"x-scan-session-token", CAPTURE_KEY.encode())])[0] == 200
    assert CAPTURE_KEY not in caplog.text and "private-faculty-credential" not in caplog.text
    assert str(case.data.capture_session_id) not in caplog.text


@pytest.mark.parametrize("result", [None, {"status": 200, "data": {}}, {"status": 404, "error": []}])
def test_unexpected_database_results_fail_closed_without_raw_diagnostics(case, result):
    case.db.scalar.side_effect = ["public", result]
    with pytest.raises(HTTPException) as error:
        scanners.ingest_scan(case.db, case.data, case.credential)
    assert error.value.status_code == 503
    case.db.rollback.assert_called_once()


def test_scanner_credential_cannot_create_borrowers_or_mutate_inventory(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "opaque_token")
    monkeypatch.setenv("AUTH_TOKEN_SHA256_MAP", json.dumps({token_digest(KEY): "FACULTY"}))
    db = Mock()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides, get_db: lambda: db,
                        get_inventory_write_db: lambda: db})
    for path in ("/students/resolve", "/checkouts", f"/checkouts/{uuid4()}/return", "/equipment"):
        assert request(app, [b"{}"], path=path, headers=credential_headers())[0] == 401
    assert db.mock_calls == []


def test_forward_migration_does_not_grant_raw_table_access_or_inventory_writes(monkeypatch):
    c, operations = Mock(), Mock()
    c.dialect = postgresql.dialect()
    c.scalar.side_effect = [True, "public", False]
    operations.get_bind.return_value = c
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr("backend.alembic.scanner_safety.verify_scanner_staging", Mock())
    migration.upgrade()
    statements = [str(call.args[0]) for call in operations.execute.call_args_list]
    assert not any(sql.startswith("GRANT SELECT ON") or sql.startswith("GRANT INSERT") or sql.startswith("GRANT UPDATE") for sql in statements)
    assert sum(sql.startswith("GRANT EXECUTE") and "TO inventory_app" in sql for sql in statements) == 2
    for body in migration.routine_bodies("public").values():
        assert "INSERT INTO public.users" not in body and "UPDATE public.equipment_assets" not in body
        assert "equipment_checkouts" not in body and "quantity_loans" not in body
    assert migration.down_revision == "b2d4e6f810ac"
    with pytest.raises(RuntimeError, match="forward"):
        migration.downgrade()


@pytest.fixture
def catalog(monkeypatch):
    from backend.alembic import scanner_safety
    from backend.app.models.scanners import EquipmentScanner, ScannerScanEvent
    tables = {model.__tablename__: model.__table__ for model in (EquipmentScanner, ScannerScanEvent)}
    c = Mock()
    c.dialect = postgresql.dialect()
    c.scalar.side_effect = lambda sql, *args: "public" if str(sql) == "SELECT current_schema()" else False
    inspector = Mock()
    inspector.get_columns.side_effect = lambda name, **kwargs: [
        {"name": col.name, "type": col.type, "nullable": col.nullable,
         "default": str(col.server_default.arg) if col.server_default is not None else None} for col in tables[name].c]
    inspector.get_pk_constraint.side_effect = lambda name, **kwargs: {"constrained_columns": [col.name for col in tables[name].primary_key]}
    inspector.get_check_constraints.side_effect = lambda name, **kwargs: [
        {"name": item.name} for item in tables[name].constraints if item.__class__.__name__ == "CheckConstraint"]
    inspector.get_unique_constraints.side_effect = lambda name, **kwargs: [
        {"column_names": [col.name for col in item.columns]} for item in tables[name].constraints
        if item.__class__.__name__ == "UniqueConstraint"]
    inspector.get_foreign_keys.side_effect = lambda name, **kwargs: [
        {"constrained_columns": [col.name for col in item.columns], "referred_table": item.elements[0].column.table.name,
         "referred_columns": [element.column.name for element in item.elements], "options": {}, "referred_schema": "public"}
        for item in tables[name].foreign_key_constraints]
    monkeypatch.setattr(scanner_safety, "inspect", lambda connection: inspector)
    return c, inspector, scanner_safety


def test_verifier_binds_exact_routine_bodies_and_signatures(catalog):
    c, _, safety = catalog
    safety.verify_scanner_staging(c, "public")
    calls = [call for call in c.scalar.call_args_list if len(call.args) == 2 and "body" in call.args[1]]
    assert len(calls) == 2
    for call, (name, body) in zip(calls, migration.routine_bodies("public").items()):
        values = call.args[1]
        assert values["body"] == body and values["name"] == "public." + name + migration.SIGNATURES[name]
        assert values["argument_count"] == len(values["arguments"])
        assert "has_function_privilege" in str(call.args[0]) and "is_grantable" in str(call.args[0])


@pytest.mark.parametrize("marker", ["has_any_column_privilege", "FROM pg_index", "FROM pg_proc"])
def test_verifier_rejects_unsafe_privileges_index_or_routine(catalog, marker):
    c, _, safety = catalog
    def answer(sql, *args):
        return "public" if str(sql) == "SELECT current_schema()" else marker in str(sql)
    c.scalar.side_effect = answer
    with pytest.raises(RuntimeError):
        safety.verify_scanner_staging(c, "public")


def test_verifier_rejects_missing_columns(catalog):
    c, inspector, safety = catalog
    inspector.get_columns.side_effect = lambda *args, **kwargs: []
    with pytest.raises(RuntimeError, match="columns"):
        safety.verify_scanner_staging(c, "public")
