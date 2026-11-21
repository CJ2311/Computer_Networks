"""Database-free borrower contracts; PostgreSQL effects have separate disposable tests."""
from contextlib import nullcontext
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.alembic.borrower_safety import verify_borrower_creation
from backend.app.models.user import User
from backend.app.schemas.borrowers import BorrowerResolve
from backend.app.services.borrowers import resolve_borrower
from backend.tests.inventory_session_stub import configure_session
from backend.tests.test_request_limits import request
from scripts import deploy_nfc_demo as baseline

migration = import_module("backend.alembic.application_versions.b2d4e6f810ac_borrower_registration")


@pytest.mark.parametrize("registration", [" reg/123 ", "\treg/123\n", "\u00a0reg/123\u3000"])
def test_registration_normalization(registration):
    value = BorrowerResolve(student_name="  Jane Doe  ", registration_number=registration)
    assert value.student_name == "Jane Doe" and value.registration_number == "REG/123"


@pytest.mark.parametrize("field,value", [
    ("student_name", ""), ("student_name", " \t"), ("student_name", "123"),
    ("student_name", "Jane\x00Doe"), ("student_name", "Jane\nDoe"), ("student_name", "A" * 151),
    ("registration_number", ""), ("registration_number", " \t"), ("registration_number", "A" * 51),
    ("registration_number", "REG 123"), ("registration_number", "REG\x00"),
    ("registration_number", "ß123"), ("registration_number", "';DELETE FROM users;--"),
    ("student_name", None), ("registration_number", 123),
])
def test_invalid_borrower_input(field, value):
    with pytest.raises(ValidationError):
        BorrowerResolve.model_validate({"student_name": "Jane Doe", "registration_number": "REG-123", field: value})


def test_maximum_lengths_and_extra_privilege_fields():
    assert len(BorrowerResolve(student_name="A" * 150, registration_number="a" * 50).registration_number) == 50
    for extra in ({"role": "FACULTY"}, {"email": "fake@college.invalid"}, {"is_active": False}, {"student_id": str(uuid4())}):
        with pytest.raises(ValidationError):
            BorrowerResolve.model_validate({"student_name": "Jane Doe", "registration_number": "REG", **extra})


@pytest.fixture
def case(monkeypatch):
    db = Mock(spec=Session)
    db.info = {}
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.in_nested_transaction.return_value = False
    db.no_autoflush = nullcontext()
    configure_session(db, monkeypatch)
    db.get_bind.return_value.dialect = SimpleNamespace(identifier_preparer=SimpleNamespace(quote_schema=lambda schema: schema))
    actor = User(id=uuid4(), role="FACULTY", is_active=True)
    db.get.return_value = actor
    db.scalar.return_value = "public"
    row = dict(id=uuid4(), college_id="legacy-reg", full_name="Jane Doe", created=False)
    db.execute.return_value.mappings.return_value.one.return_value = row
    return SimpleNamespace(db=db, actor=actor, row=row,
                           data=BorrowerResolve(student_name="Jane Doe", registration_number="legacy-reg"))


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("role", ["FACULTY", "LAB_ASSISTANT"])
def test_service_uses_restricted_routine_and_preserves_uuid(case, role, created):
    case.actor.role = role
    case.row["created"] = created
    result = resolve_borrower(case.db, case.data, case.actor)
    assert result.student_id == case.row["id"] and result.created is created
    assert result.registration_number == "LEGACY-REG"
    sql, parameters = case.db.execute.call_args.args
    assert "public.borrower_resolve_or_create" in str(sql)
    assert parameters["actor"] == case.actor.id and parameters["name"] == "Jane Doe"
    assert parameters["registration"] == "LEGACY-REG"
    assert set(parameters) == {"actor", "new_id", "name", "registration"}
    case.db.add.assert_not_called()
    case.db.flush.assert_not_called()
    case.db.commit.assert_called_once()


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_service_rechecks_persisted_eligibility(case, role):
    case.db.get.return_value = User(id=case.actor.id, role=role, is_active=True)
    with pytest.raises(HTTPException) as error:
        resolve_borrower(case.db, case.data, case.actor)
    assert error.value.status_code == 403
    case.db.execute.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("code,status", [("P2001", 409), ("42501", 403), ("22023", 422), ("40001", 409)])
def test_database_errors_are_generic_and_roll_back(case, code, status):
    class Failure(Exception):
        sqlstate = code
    case.db.execute.side_effect = DBAPIError("sensitive SQL", {"name": "sensitive identity"}, Failure("private"))
    with pytest.raises(HTTPException) as error:
        resolve_borrower(case.db, case.data, case.actor)
    assert error.value.status_code == status
    assert "private" not in error.value.detail and "sensitive" not in error.value.detail
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("role,expected", [("FACULTY", 200), ("LAB_ASSISTANT", 200), ("STUDENT", 403), ("ADMIN", 403)])
def test_http_role_and_response_contract(monkeypatch, role, expected):
    from backend.app.auth.dependencies import get_current_user
    from backend.app.database import get_inventory_write_db
    from backend.app.main import app
    from backend.app.routers import operations
    actor = User(id=uuid4(), role=role, is_active=True)
    borrower_id = uuid4()
    service = Mock(return_value={"student_id": borrower_id, "student_name": "Jane Doe", "registration_number": "REG", "created": True})
    monkeypatch.setattr(operations, "resolve_borrower", service)
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides,
                        get_current_user: lambda: actor, get_inventory_write_db: lambda: Mock()})
    code, body, _, _ = request(app, [b'{"student_name":"Jane Doe","registration_number":"reg"}'], path="/students/resolve")
    assert code == expected
    if expected == 200:
        assert body == {"student_id": str(borrower_id), "student_name": "Jane Doe", "registration_number": "REG", "created": True}
        assert service.call_args.args[1].registration_number == "REG"
    else:
        service.assert_not_called()


def test_openapi_and_checkout_contract_remain_explicit():
    from backend.app.main import app
    models = app.openapi()["components"]["schemas"]
    assert set(models["BorrowerResolve"]["required"]) == {"student_name", "registration_number"}
    assert models["BorrowerResolve"]["additionalProperties"] is False
    assert models["BorrowerResolveResponse"]["properties"]["student_id"]["format"] == "uuid"
    assert "student_id" in models["CheckoutCreate"]["required"]


@pytest.mark.parametrize("payload", [b'{"student_name":" ","registration_number":"REG"}',
                                     b'{"student_name":"Jane Doe","registration_number":"REG","role":"ADMIN"}'])
def test_http_invalid_input_cannot_reach_creation(monkeypatch, payload):
    from backend.app.auth.dependencies import get_current_user
    from backend.app.database import get_inventory_write_db
    from backend.app.main import app
    from backend.app.routers import operations
    service, db = Mock(), Mock()
    monkeypatch.setattr(operations, "resolve_borrower", service)
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides,
                        get_current_user: lambda: User(role="FACULTY", is_active=True),
                        get_inventory_write_db: lambda: db})
    assert request(app, [payload], path="/students/resolve")[0] == 422
    service.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("revision", [None, baseline.B7, baseline.C4, baseline.HEAD])
def test_legacy_checkpoint_remains_pinned(revision):
    old = baseline.checkpoint_model("users", revision)
    assert old.c.email.nullable is False
    assert "ck_users_staff_email" not in {item.name for item in old.constraints}
    assert User.__table__.c.email.nullable is True
    assert "ck_users_staff_email" in {item.name for item in User.__table__.constraints}


@pytest.mark.parametrize("failure", [True, None])
@pytest.mark.parametrize("stage", [0, 1, 2])
def test_verifier_rejects_missing_or_altered_protection(stage, failure):
    c = Mock()
    c.scalar.side_effect = [False] * stage + [failure]
    with pytest.raises(RuntimeError):
        verify_borrower_creation(c, "public")


def test_verifier_binds_exact_body_and_whitespace_policy():
    c = Mock()
    c.scalar.return_value = False
    verify_borrower_creation(c, "public")
    assert c.scalar.call_count == 3
    for call in c.scalar.call_args_list:
        assert call.args[1]["body"] == migration.routine_body("public")
        assert call.args[1]["trim_chars"] == migration.TRIM_CHARACTERS


def test_forward_migration_adds_only_scoped_privileges(monkeypatch):
    c = Mock()
    c.dialect.name = "postgresql"
    c.dialect.identifier_preparer.quote_schema.return_value = "public"
    c.scalar.side_effect = [True, "public", False, False]
    operations = Mock()
    operations.get_bind.return_value = c
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr("backend.alembic.borrower_safety.verify_borrower_creation", Mock())
    migration.upgrade()
    statements = [str(call.args[0]) for call in operations.execute.call_args_list]
    assert any("GRANT EXECUTE" in sql and "TO inventory_app" in sql for sql in statements)
    assert not any(sql.startswith("GRANT INSERT") and "TO inventory_app" in sql for sql in statements)
    assert not any(sql.startswith("UPDATE ") or sql.startswith("DELETE ") for sql in statements)
    assert migration.down_revision == "f1a6b8c920de"
    with pytest.raises(RuntimeError, match="forward"):
        migration.downgrade()


def test_legacy_collisions_abort_before_schema_changes(monkeypatch):
    c = Mock()
    c.dialect.name = "postgresql"
    c.dialect.identifier_preparer.quote_schema.return_value = "public"
    c.scalar.side_effect = [True, "public", False, True]
    operations = Mock()
    operations.get_bind.return_value = c
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="collide"):
        migration.upgrade()
    operations.alter_column.assert_not_called()
    operations.create_check_constraint.assert_not_called()
