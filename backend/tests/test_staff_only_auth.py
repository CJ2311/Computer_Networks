"""Staff-only policy across real authentication dependencies; no network/database."""
import hashlib
import json
import re
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from backend.app.auth import college
from backend.app.auth.dependencies import get_current_user
from backend.app.auth.introspection_limits import IntrospectionGuard, IntrospectionSettings
from backend.app.database import get_db, get_inventory_write_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services.checkout import validate_borrowing_student
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history
from backend.app.schemas.borrowing_history import HistoryFilters
from backend.tests.test_college_auth import provider
from backend.tests.test_request_limits import request


MODES = ("college_introspection", "opaque_token", "development")
TOKEN = "synthetic_staff_policy_" + "x" * 43


@pytest.fixture
def identity(provider, monkeypatch):
    def configure(mode, role):
        monkeypatch.setenv("AUTH_MODE", "" if mode == "development" else mode)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
        names = {"FACULTY": "DEV-FACULTY", "LAB_ASSISTANT": "DEV-ASSISTANT",
                 "STUDENT": "DEV-STUDENT", "ADMIN": "DEV-ADMIN"}
        user = User(id=uuid4(), college_id=names[role], full_name="Synthetic identity",
                    email="synthetic@college.invalid", role=role, is_active=True)
        db = Mock()
        db.scalar.return_value = user
        db.scalars.return_value.all.return_value = []
        monkeypatch.setenv("AUTH_TOKEN_SHA256_MAP", json.dumps({hashlib.sha256(TOKEN.encode()).hexdigest(): user.college_id}))
        guard = IntrospectionGuard(IntrospectionSettings(global_per_minute=100000, peer_per_minute=100000))
        monkeypatch.setattr(college, "get_introspection_guard", lambda: guard)
        provider[1]["roles"] = ["FACULTY"]  # Provider roles never override local policy.
        headers = [(b"x-dev-user", names[role].encode())] if mode == "development" else [
            (b"authorization", ("Bearer " + TOKEN).encode())]
        monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides,
                            get_db: lambda: db, get_inventory_write_db: lambda: db})
        return user, db, headers
    return configure


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("role", ["FACULTY", "LAB_ASSISTANT", "STUDENT", "ADMIN"])
def test_authentication_eligibility_in_every_mode(identity, mode, role):
    user, db, headers = identity(mode, role)
    before = dict(user.__dict__)
    code, body, _, _ = request(app, [b""], method="GET", path="/auth/me", headers=headers)
    if role in {"FACULTY", "LAB_ASSISTANT"}:
        assert code == 200 and body["id"] == str(user.id) and body["role"] == role
        assert body["is_active"] is True
    else:
        assert code == (401 if mode == "development" else 403)
        if mode == "development":
            db.scalar.assert_not_called()
    assert user.__dict__ == before  # Authentication cannot alter borrower/history data.
    for name in ("add", "delete", "flush", "commit"):
        getattr(db, name).assert_not_called()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_nonstaff_cannot_access_any_protected_endpoint(identity, mode, role):
    _, db, headers = identity(mode, role)
    checked = set()
    for template, operations in app.openapi()["paths"].items():
        if template in {"/", "/health"}:
            continue
        path = re.sub(r"\{[^}]+\}", str(uuid4()), template)
        for operation in operations:
            if operation not in {"get", "post", "patch", "put", "delete", "head", "options", "trace"}:
                continue
            method = operation.upper()
            code, _, _, _ = request(app, [b"{}"], method=method, path=path, headers=headers,
                                    query=b"nfc_tag_id=TAG&college_id=BORROWER")
            scanner_only = template in {
                "/device-scans",
                "/student-nfc/registrations/{registration_id}/verify",
            }
            expected = 401 if mode == "development" or scanner_only else 403
            assert code == expected, (method, template, code)
            checked.add((method, template))
    assert ("GET", "/auth/me") in checked
    assert ("GET", "/equipment") in checked
    assert ("POST", "/checkouts") in checked
    assert ("GET", "/quantity-loans") in checked
    assert len(checked) >= 50
    for name in ("get", "scalars", "execute", "add", "delete", "flush", "commit"):
        getattr(db, name).assert_not_called()


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_staff_development_header_cannot_authenticate_nonstaff_record(identity, role):
    _, db, _ = identity("development", role)
    req = Request({"type": "http", "client": ("127.0.0.1", 123), "headers": []})
    with pytest.raises(HTTPException) as error:
        get_current_user(req, x_dev_user="DEV-FACULTY", db=db)
    assert error.value.status_code == 403


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
@pytest.mark.parametrize("detail", [False, True])
def test_history_services_deny_nonstaff_without_reading_records(role, detail):
    user = User(id=uuid4(), role=role, is_active=True)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        if detail:
            get_borrowing_history(db, user, uuid4())
        else:
            list_borrowing_history(db, user, HistoryFilters(student_id=user.id))
    assert error.value.status_code == 403
    assert db.mock_calls == []


def test_student_remains_an_active_valid_borrower_without_authentication():
    borrower = User(id=uuid4(), college_id="REG-123", full_name="Borrower", role="STUDENT", is_active=True)
    db = Mock()
    db.get.return_value = borrower
    before = dict(borrower.__dict__)
    assert validate_borrowing_student(db, borrower.id) is borrower
    assert borrower.__dict__ == before
    db.get.assert_called_once_with(User, borrower.id, populate_existing=True)
    for name in ("add", "delete", "flush", "commit"):
        getattr(db, name).assert_not_called()
