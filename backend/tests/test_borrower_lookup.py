"""Exercise production lookup SQL with in-memory SQLite normalization functions.

No PostgreSQL connection, migration or external database is used. PostgreSQL SQL
compilation separately checks the migration-compatible normalization expression.
"""
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.models.user import User
from backend.app.routers.operations import router
from backend.app.schemas.borrowers import BorrowerResolve
from backend.app.services.borrowers import lookup_borrower, _normalized_registration, REGISTRATION_TRIM_CHARACTERS
from backend.tests.test_backend_api_foundation import call


@pytest.fixture
def case():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def normalization(connection, _):
        connection.create_function("btrim", 2, lambda value, chars: value.strip(chars))
        connection.create_collation("C", lambda left, right: (left > right) - (left < right))

    User.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db:
        borrower = User(id=uuid4(), college_id=" \tReg/Ab-123\u3000", full_name="Jane Student",
                        email=None, role="STUDENT", is_active=True)
        db.add(borrower); db.commit()
        statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def record(_connection, _cursor, sql, _parameters, _context, _many):
            statements.append(sql)

        yield SimpleNamespace(db=db, borrower=borrower, statements=statements)
    engine.dispose()


@pytest.mark.parametrize("registration", [
    "reg/ab-123", "REG/AB-123", "rEg/aB-123", " REG/AB-123 ",
    "\treg/ab-123\n", "\u00a0reg/ab-123\u3000", "\x1creg/ab-123\x1f",
])
def test_case_and_unicode_whitespace_match_resolution_without_rewriting_identity(case, registration):
    canonical = BorrowerResolve(student_name="Jane Student", registration_number=registration).registration_number
    assert canonical == "REG/AB-123"
    original = (case.borrower.id, case.borrower.college_id, case.borrower.full_name)
    found = lookup_borrower(case.db, registration)
    assert (found.id, found.college_id, found.full_name) == original
    assert not case.db.new and not case.db.dirty and not case.db.deleted
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in case.statements)
    assert case.db.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("registration", ["NOT-FOUND", "   ", "\u00a0\u3000"])
def test_missing_borrower_preserves_404(case, registration):
    with pytest.raises(HTTPException) as error:
        lookup_borrower(case.db, registration)
    assert error.value.status_code == 404 and error.value.detail == "Active student not found"


@pytest.mark.parametrize("role,active", [("STUDENT", False), ("FACULTY", True), ("LAB_ASSISTANT", True), ("ADMIN", True)])
def test_only_active_students_are_returned(case, role, active):
    case.borrower.role, case.borrower.is_active = role, active
    case.borrower.email = "synthetic@college.invalid"
    case.db.commit()
    with pytest.raises(HTTPException) as error:
        lookup_borrower(case.db, "REG/AB-123")
    assert error.value.status_code == 404


@pytest.mark.parametrize("role,active", [("STUDENT", True), ("STUDENT", False), ("FACULTY", True)])
def test_ambiguous_normalized_legacy_identities_fail_closed(case, role, active):
    # SQLite intentionally has only the original raw unique constraint. This
    # models legacy/drifted data without weakening PostgreSQL's normalized index.
    duplicate = User(id=uuid4(), college_id="REG/AB-123", full_name="Other Identity",
                     email="other@college.invalid", role=role, is_active=active)
    case.db.add(duplicate); case.db.commit()
    case.statements.clear()
    with pytest.raises(HTTPException) as error:
        lookup_borrower(case.db, "reg/ab-123")
    assert error.value.status_code == 409
    assert "Other Identity" not in error.value.detail
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in case.statements)
    assert case.db.scalar(select(func.count()).select_from(User)) == 2


def test_read_does_not_autoflush_pending_mutations(case):
    pending = User(id=uuid4(), college_id="UNSAVED", full_name="Unsaved Borrower", role="STUDENT", is_active=True)
    case.db.add(pending)
    assert lookup_borrower(case.db, "REG/AB-123").id == case.borrower.id
    with pytest.raises(HTTPException):
        lookup_borrower(case.db, "UNSAVED")
    assert pending in case.db.new
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in case.statements)


@pytest.fixture
def api(case):
    app = FastAPI(); app.include_router(router)
    actor = User(id=uuid4(), role="FACULTY", is_active=True)
    app.dependency_overrides[get_db] = lambda: case.db
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        yield app, actor
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["FACULTY", "LAB_ASSISTANT"])
def test_http_preserves_response_schema_and_legacy_spelling(case, api, role):
    app, actor = api
    actor.role = role
    query = urlencode({"college_id": "\u00a0reg/ab-123\u3000"}).encode()
    assert call(app, "/students/lookup", query) == (200, {
        "id": str(case.borrower.id), "college_id": case.borrower.college_id, "full_name": "Jane Student",
    })


@pytest.mark.parametrize("role,active", [("ADMIN", True), ("STUDENT", True), ("FACULTY", False), ("LAB_ASSISTANT", False)])
def test_http_denies_ineligible_staff_before_lookup(case, api, role, active):
    app, actor = api
    actor.role, actor.is_active = role, active
    assert call(app, "/students/lookup", b"college_id=REG%2FAB-123")[0] == 403
    assert case.statements == []


def test_http_missing_credentials_cannot_reach_lookup(case, api):
    app, _ = api
    def deny():
        raise HTTPException(401, "Valid credentials required")
    app.dependency_overrides[get_current_user] = deny
    assert call(app, "/students/lookup", b"college_id=REG%2FAB-123")[0] == 401
    assert case.statements == []


def test_http_missing_borrower_preserves_error_contract(case, api):
    app, _ = api
    assert call(app, "/students/lookup", b"college_id=NOT-FOUND") == (
        404, {"detail": "Active student not found"},
    )


@pytest.mark.parametrize("value", ["", "X" * 51, "BAD\x00ID"])
def test_existing_query_validation_prevents_database_access(case, api, value):
    app, _ = api
    assert call(app, "/students/lookup", urlencode({"college_id": value}).encode())[0] == 422
    assert case.statements == []


def test_normalization_matches_frozen_postgresql_expression_and_is_parameterized():
    migration = import_module("backend.alembic.application_versions.b2d4e6f810ac_borrower_registration")
    assert REGISTRATION_TRIM_CHARACTERS == migration.TRIM_CHARACTERS
    expression = _normalized_registration(User.college_id)
    assert expression.name == "upper"
    compiled = select(User).where(expression == _normalized_registration("' OR 1=1 --")).limit(2).compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True},
    )
    sql = str(compiled)
    assert 'upper(btrim(users.college_id,' in sql and 'COLLATE "C"' in sql
    assert "' OR 1=1 --" not in sql and "' OR 1=1 --" in compiled.params.values()
    assert 2 in compiled.params.values()
