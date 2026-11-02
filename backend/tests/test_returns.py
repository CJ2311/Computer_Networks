from datetime import datetime, timedelta, timezone
import asyncio
import json
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from backend.app.models.checkout import EquipmentCheckout
from backend.app.database import get_db
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User
from backend.app.routers.checkout import router, return_equipment
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.checkout import record_return


@pytest.fixture
def loan():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    asset = EquipmentAsset(id=uuid4(), availability_status="BORROWED", condition_status="WORKING")
    checkout = EquipmentCheckout(
        id=uuid4(), asset_id=asset.id, student_id=uuid4(), issued_by=uuid4(),
        checked_out_at=now - timedelta(days=1), due_at=now + timedelta(hours=1),
        status="BORROWED", notes="Original checkout notes\nKeep this unchanged.",
    )
    staff = User(id=uuid4(), role="FACULTY", is_active=True)
    db = Mock()
    db.scalar.side_effect = [asset.id, asset, checkout, now]
    return db, checkout, asset, staff, now


@pytest.mark.parametrize("condition, availability", [
    ("WORKING", "AVAILABLE"), ("DAMAGED", "RESERVED"), ("UNDER_REPAIR", "RESERVED"),
])
@pytest.mark.parametrize("role", ["FACULTY", "LAB_ASSISTANT"])
def test_success_preserves_history_and_database_clock(loan, condition, availability, role):
    db, checkout, asset, staff, now = loan
    staff.role = role
    original = {key: getattr(checkout, key) for key in (
        "id", "asset_id", "student_id", "issued_by", "checked_out_at", "due_at", "notes",
    )}
    notes = 'Screen flickers.\n[RETURN] "original user text"'
    response = return_equipment(
        checkout.id, ReturnCreate(condition_status=condition, notes=notes), db, staff
    )
    assert checkout.returned_at == response.returned_at == now
    assert checkout.returned_to == response.returned_to == staff.id
    assert checkout.status == response.status == "RETURNED"
    assert asset.condition_status == response.condition_status == condition
    assert asset.availability_status == response.availability_status == availability
    assert response.is_overdue is False
    for key, value in original.items():
        if key != "notes":
            assert getattr(checkout, key) == value
    assert checkout.notes.startswith(original["notes"] + "\n\n[RETURN] ")
    annotation = json.loads(checkout.notes.split("\n\n[RETURN] ", 1)[1])
    assert annotation["condition_status"] == condition
    assert annotation["notes"] == notes
    assert annotation["returned_to"] == str(staff.id)
    statements = [call.args[0] for call in db.scalar.call_args_list]
    for statement in statements[1:3]:
        assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
        assert statement.get_execution_options()["populate_existing"] is True
    assert "equipment_assets" in str(statements[1])
    assert "equipment_checkouts" in str(statements[2])
    assert "clock_timestamp()" in str(statements[3])
    db.flush.assert_called_once()
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


@pytest.mark.parametrize("offset, expected", [(-1, True), (0, False), (1, False)])
def test_overdue_boundary(loan, offset, expected):
    db, checkout, _, staff, now = loan
    checkout.due_at = now + timedelta(seconds=offset)
    assert record_return(db, checkout.id, staff, ReturnCreate(condition_status="WORKING")).is_overdue is expected


@pytest.mark.parametrize("role, active", [
    ("STUDENT", True), ("ADMIN", True), ("FACULTY", False), ("LAB_ASSISTANT", False),
])
def test_unauthorized_service_call(loan, role, active):
    db, checkout, _, staff, _ = loan
    staff.role, staff.is_active = role, active
    with pytest.raises(HTTPException) as error:
        record_return(db, checkout.id, staff, ReturnCreate(condition_status="WORKING"))
    assert error.value.status_code == 403
    db.scalar.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.parametrize("state", ["missing", "asset_missing", "checkout_disappeared", "asset_changed",
                                  "returned", "partial_return", "available", "reserved", "retired"])
def test_invalid_state_is_not_modified(loan, state):
    db, checkout, asset, staff, now = loan
    expected = 409
    if state == "missing":
        db.scalar.side_effect = [None]
        expected = 404
    elif state == "asset_missing":
        db.scalar.side_effect = [asset.id, None, checkout]
    elif state == "checkout_disappeared":
        db.scalar.side_effect = [asset.id, asset, None]
        expected = 404
    elif state == "asset_changed":
        checkout.asset_id = uuid4()
    elif state == "returned":
        checkout.status, checkout.returned_at, checkout.returned_to = "RETURNED", now, staff.id
    elif state == "partial_return":
        checkout.returned_at = now
    else:
        asset.availability_status = state.upper()
    before = (checkout.status, checkout.returned_at, checkout.returned_to, checkout.notes,
              asset.availability_status, asset.condition_status)
    with pytest.raises(HTTPException) as error:
        record_return(db, checkout.id, staff, ReturnCreate(condition_status="DAMAGED"))
    assert error.value.status_code == expected
    assert before == (checkout.status, checkout.returned_at, checkout.returned_to, checkout.notes,
                      asset.availability_status, asset.condition_status)
    db.flush.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_database_clock_before_checkout_is_rejected(loan):
    db, checkout, _, staff, now = loan
    checkout.checked_out_at = now + timedelta(seconds=1)
    with pytest.raises(HTTPException) as error:
        record_return(db, checkout.id, staff, ReturnCreate(condition_status="WORKING"))
    assert error.value.status_code == 409
    assert checkout.returned_at is None
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.parametrize("operation", ["flush", "commit"])
@pytest.mark.parametrize("integrity", [True, False])
def test_transaction_failures_roll_back(loan, operation, integrity):
    db, checkout, _, staff, _ = loan
    failure = IntegrityError("return", {}, Exception()) if integrity else RuntimeError("write failed")
    getattr(db, operation).side_effect = failure
    with pytest.raises(HTTPException if integrity else RuntimeError) as error:
        record_return(db, checkout.id, staff, ReturnCreate(condition_status="DAMAGED"))
    if integrity:
        assert error.value.status_code == 409
    db.rollback.assert_called_once()
    if operation == "flush":
        db.commit.assert_not_called()


@pytest.mark.parametrize("original_notes", [None, ""])
def test_empty_original_notes(loan, original_notes):
    db, checkout, _, staff, _ = loan
    checkout.notes = original_notes
    response = record_return(db, checkout.id, staff, ReturnCreate(condition_status="WORKING"))
    assert "[RETURN] " in response.notes
    assert json.loads(response.notes.split("[RETURN] ", 1)[1])["notes"] is None


@pytest.mark.parametrize("data", [
    {}, {"condition_status": "MISSING"}, {"condition_status": "MALFUNCTIONING"},
    {"condition_status": "WORKING", "notes": "x" * 1001},
    {"condition_status": "WORKING", "returned_at": "2020-01-01T00:00:00Z"},
    {"condition_status": "WORKING", "returned_to": str(uuid4())},
])
def test_request_validation(data):
    with pytest.raises(ValidationError):
        ReturnCreate.model_validate(data)


@pytest.mark.parametrize("role, allowed", [("FACULTY", True), ("LAB_ASSISTANT", True),
                                          ("STUDENT", False), ("ADMIN", False)])
def test_route_uses_existing_role_dependency(role, allowed):
    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path.endswith("/return"))
    check = next(d.call for d in route.dependant.dependencies if d.name == "authorized_user")
    user = User(role=role, is_active=True)
    if allowed:
        assert check(current_user=user) is user
    else:
        with pytest.raises(HTTPException) as error:
            check(current_user=user)
        assert error.value.status_code == 403


@pytest.mark.parametrize("role, active, header, data, expected", [
    ("FACULTY", True, "DEV-FACULTY", {"condition_status": "WORKING"}, 200),
    ("LAB_ASSISTANT", True, "DEV-ASSISTANT", {"condition_status": "DAMAGED"}, 200),
    ("STUDENT", True, "DEV-STUDENT", {"condition_status": "WORKING"}, 403),
    ("ADMIN", True, "DEV-ADMIN", {"condition_status": "WORKING"}, 403),
    ("FACULTY", False, "DEV-FACULTY", {"condition_status": "WORKING"}, 403),
    ("LAB_ASSISTANT", False, "DEV-ASSISTANT", {"condition_status": "WORKING"}, 403),
    ("FACULTY", True, None, {"condition_status": "WORKING"}, 401),
    ("FACULTY", True, "DEV-UNKNOWN", {"condition_status": "WORKING"}, 401),
    ("FACULTY", True, "DEV-FACULTY", {"condition_status": "MISSING"}, 422),
    ("FACULTY", True, "DEV-FACULTY", {"condition_status": "WORKING", "returned_to": str(uuid4())}, 422),
])
def test_http_endpoint_with_real_auth_dependencies(loan, monkeypatch, role, active, header, data, expected):
    db, checkout, asset, staff, now = loan
    staff.role, staff.is_active = role, active
    db.scalar.side_effect = [staff, asset.id, asset, checkout, now]
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    app = FastAPI()
    app.include_router(router)

    def sessions():
        yield db

    app.dependency_overrides[get_db] = sessions

    async def request():
        path = f"/checkouts/{checkout.id}/return"
        headers = [(b"content-type", b"application/json")]
        if header is not None:
            headers.append((b"x-dev-user", header.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": headers,
                 "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80)}
        messages = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return status, body

    status, response = asyncio.run(request())
    assert status == expected
    if expected == 200:
        assert response["returned_to"] == str(staff.id)
        assert response["returned_at"] == now.isoformat().replace("+00:00", "Z")
        assert response["condition_status"] == data["condition_status"]
        assert response["is_overdue"] is False
        db.commit.assert_called_once()
    else:
        db.commit.assert_not_called()
        assert checkout.returned_at is None


def test_application_openapi_preserves_checkout_and_registers_returns():
    from backend.app.main import app

    spec = app.openapi()
    assert spec["paths"]["/checkouts"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/CheckoutResponse")
    operation = spec["paths"]["/checkouts/{checkout_id}/return"]["post"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/ReturnResponse")
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ReturnCreate")
    assert set(spec["components"]["schemas"]["CheckoutResponse"]["properties"]) == {
        "id", "asset_id", "student_id", "issued_by", "checked_out_at", "due_at",
        "returned_at", "returned_to", "status", "notes",
    }
