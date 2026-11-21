"""Database-free HTTP and OpenAPI contracts for operations responses."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import pytest

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import User
from backend.app.routers import operations
from backend.tests.test_backend_api_foundation import call


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(operations.router)
    db = Mock()
    db.no_autoflush = nullcontext()
    actor = User(id=uuid4(), college_id="F", full_name="Faculty", role="FACULTY", is_active=True)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_inventory_write_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        yield app, db, actor
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY"])
@pytest.mark.parametrize("allow_offsite", [False, True])
def test_policy_write_response(api, monkeypatch, role, allow_offsite):
    app, db, actor = api
    actor.role = role
    equipment_id = uuid4()
    service = Mock(return_value={"equipment_id": equipment_id, "allow_offsite": allow_offsite})
    monkeypatch.setattr(operations, "set_policy", service)
    if role == "ADMIN":
        assert call(app, f"/equipment/{equipment_id}/loan-policy", method="POST",
                    payload={"allow_offsite": allow_offsite})[0] == 403
        service.assert_not_called()
        return
    assert call(app, f"/equipment/{equipment_id}/loan-policy", method="POST",
                payload={"allow_offsite": allow_offsite}) == (
        200, {"equipment_id": str(equipment_id), "allow_offsite": allow_offsite}
    )
    args = service.call_args.args
    assert args[0] is db and args[1] == equipment_id and args[3] is actor
    assert args[2].allow_offsite is allow_offsite


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT", "STUDENT"])
@pytest.mark.parametrize("policy", [None, False, True])
def test_policy_read_response_and_default(api, role, policy):
    app, db, actor = api
    actor.role = role
    equipment_id = uuid4()
    db.get.side_effect = [object(), None if policy is None else SimpleNamespace(allow_offsite=policy)]
    if role in {"STUDENT", "ADMIN"}:
        assert call(app, f"/equipment/{equipment_id}/loan-policy")[0] == 403
        db.get.assert_not_called()
        return
    assert call(app, f"/equipment/{equipment_id}/loan-policy") == (
        200, {"equipment_id": str(equipment_id), "allow_offsite": policy is True}
    )


def test_policy_read_missing_equipment(api):
    app, db, actor = api
    db.get.return_value = None
    assert call(app, f"/equipment/{uuid4()}/loan-policy") == (404, {"detail": "Equipment not found"})


@pytest.mark.parametrize("destination", [None, "Room 101"])
def test_destination_response(api, monkeypatch, destination):
    app, db, actor = api
    checkout_id = uuid4()
    history = Mock()
    monkeypatch.setattr(operations, "get_borrowing_history", history)
    db.get.return_value = None if destination is None else SimpleNamespace(destination=destination)
    assert call(app, f"/checkouts/{checkout_id}/destination") == (
        200, {"checkout_id": str(checkout_id), "destination": destination}
    )
    history.assert_called_once_with(db, actor, checkout_id)


def test_destination_preserves_non_disclosing_history_error(api, monkeypatch):
    app, db, actor = api
    actor.role = "FACULTY"
    history = Mock(side_effect=HTTPException(404, "Checkout not found"))
    monkeypatch.setattr(operations, "get_borrowing_history", history)
    assert call(app, f"/checkouts/{uuid4()}/destination") == (404, {"detail": "Checkout not found"})
    db.get.assert_not_called()


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
def test_student_lookup_public_fields(api, role):
    app, db, actor = api
    actor.role = role
    student = User(id=uuid4(), college_id="S", full_name="Student", role="STUDENT",
                   email="private@college.invalid", is_active=True)
    db.scalars.return_value.all.return_value = [student]
    if role == "ADMIN":
        assert call(app, "/students/lookup", b"college_id=S")[0] == 403
        db.scalars.assert_not_called()
        return
    assert call(app, "/students/lookup", b"college_id=S") == (
        200, {"id": str(student.id), "college_id": "S", "full_name": "Student"}
    )


def test_student_lookup_missing_student(api):
    app, db, actor = api
    db.scalars.return_value.all.return_value = []
    assert call(app, "/students/lookup", b"college_id=S") == (404, {"detail": "Active student not found"})


@pytest.mark.parametrize("role", ["STUDENT", "LAB_ASSISTANT"])
def test_policy_write_denied_roles(api, monkeypatch, role):
    app, db, actor = api
    actor.role = role
    service = Mock()
    monkeypatch.setattr(operations, "set_policy", service)
    status, body = call(app, f"/equipment/{uuid4()}/loan-policy", method="POST",
                        payload={"allow_offsite": True})
    expected = "Application access is restricted to authorized staff" if role == "STUDENT" else "You do not have permission to perform this action"
    assert (status, body) == (403, {"detail": expected})
    service.assert_not_called()


def test_student_lookup_denies_students(api):
    app, db, actor = api
    actor.role = "STUDENT"
    assert call(app, "/students/lookup", b"college_id=S") == (
        403, {"detail": "Application access is restricted to authorized staff"}
    )
    db.scalar.assert_not_called()


@pytest.mark.parametrize("path,method,payload", [
    ("/equipment/{id}/loan-policy", "GET", None),
    ("/equipment/{id}/loan-policy", "POST", {"allow_offsite": True}),
    ("/checkouts/{id}/destination", "GET", None),
    ("/students/lookup", "GET", None),
])
def test_operations_require_authentication(api, path, method, payload):
    app, db, actor = api
    def unauthenticated():
        raise HTTPException(401, "Authentication required")
    app.dependency_overrides[get_current_user] = unauthenticated
    assert call(app, path.format(id=uuid4()), b"college_id=S", method, payload) == (
        401, {"detail": "Authentication required"}
    )
    db.get.assert_not_called()
    db.scalar.assert_not_called()


def test_operations_openapi_response_contracts(api):
    app, db, actor = api
    schema = app.openapi()
    for path, method, name in [
        ("/equipment/{equipment_id}/loan-policy", "post", "LoanPolicyResponse"),
        ("/equipment/{equipment_id}/loan-policy", "get", "LoanPolicyResponse"),
        ("/checkouts/{checkout_id}/destination", "get", "CheckoutDestinationResponse"),
        ("/students/lookup", "get", "StudentLookupResponse"),
    ]:
        response = schema["paths"][path][method]["responses"]["200"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{name}"
        }
    models = schema["components"]["schemas"]
    expected = {
        "LoanPolicyResponse": {"equipment_id": {"type": "string", "format": "uuid"},
                               "allow_offsite": {"type": "boolean"}},
        "CheckoutDestinationResponse": {"checkout_id": {"type": "string", "format": "uuid"},
                                        "destination": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "StudentLookupResponse": {"id": {"type": "string", "format": "uuid"},
                                  "college_id": {"type": "string"}, "full_name": {"type": "string"}},
    }
    for name, fields in expected.items():
        model = models[name]
        assert set(model["properties"]) == set(model["required"]) == set(fields)
        for field, definition in fields.items():
            actual = {key: value for key, value in model["properties"][field].items() if key != "title"}
            assert actual == definition
