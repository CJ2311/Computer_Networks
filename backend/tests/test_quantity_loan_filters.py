"""Database-free unit and API tests for quantity loan filtering."""

from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.dialects import postgresql

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.main import app
from backend.app.models.operations import QuantityLoan
from backend.app.models.user import User, UserRole
from backend.app.schemas.operations import QuantityLoanPage, QuantityLoanResponse
from backend.app.services.quantity_loans import list_quantity, loan_view


@pytest.fixture
def mock_db():
    db = Mock()
    db.no_autoflush = nullcontext()
    now_dt = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    def _mock_scalar(stmt, *args, **kwargs):
        if stmt is not None and "clock_timestamp" in str(stmt):
            return now_dt
        return None

    db.scalar.side_effect = _mock_scalar
    db.scalars.return_value.all.return_value = []
    user_store = {}

    def _get(entity, ident, **kwargs):
        if entity is User:
            return user_store.get(ident, User(id=ident, role="FACULTY", is_active=True))
        return None

    db.get = Mock(side_effect=_get)
    db._user_store = user_store
    return db


def make_user(mock_db, role=UserRole.FACULTY, user_id=None, is_active=True):
    uid = user_id or uuid4()
    r = role.value if isinstance(role, UserRole) else str(role)
    user = User(
        id=uid,
        college_id=f"COL_{uid.hex[:8]}",
        full_name="Test User",
        email=f"{uid.hex[:8]}@college.invalid",
        role=r,
        is_active=is_active,
    )
    mock_db._user_store[uid] = user
    return user


def compile_stmt(stmt):
    """Compile SQLAlchemy statement using PostgreSQL dialect for SQL text inspection."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def assert_read_only_db(db: Mock):
    for method in ("add", "delete", "flush", "commit", "rollback"):
        getattr(db, method).assert_not_called()


@pytest.fixture
def client_factory(mock_db):
    def _create(role: UserRole | None = UserRole.FACULTY, user_id: UUID | None = None):
        user_uuid = user_id or uuid4()
        user = make_user(mock_db, role=role if role else UserRole.STUDENT, user_id=user_uuid)

        def override_db():
            yield mock_db

        def override_user():
            if role is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        client = TestClient(app, raise_server_exceptions=False)
        client._test_user = user
        return client

    yield _create
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Service Layer Query Construction & Logic Tests
# ==============================================================================


def test_existing_unfiltered_behavior(mock_db):
    """Scenario 1: Unfiltered request by staff returns QuantityLoanPage without extra where clauses."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    res = list_quantity(mock_db, staff, limit=25, offset=0)

    assert_read_only_db(mock_db)
    assert res == {"items": [], "limit": 25, "offset": 0, "has_more": False}

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "FROM quantity_loans" in sql
    assert "WHERE" not in sql
    assert "JOIN" not in sql


def test_staff_filtering_by_student_id(mock_db):
    """Scenario 2: Staff filtering by student_id applies student_id filter."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    target_student_id = uuid4()
    list_quantity(mock_db, staff, student_id=target_student_id)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.student_id =" in sql


def test_student_filtering_by_own_student_id(mock_db):
    """Scenario 3: Student filtering by their own student_id succeeds."""
    student_id = uuid4()
    student = make_user(mock_db, role=UserRole.STUDENT, user_id=student_id)
    res = list_quantity(mock_db, student, student_id=student_id)

    assert res["items"] == []
    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.student_id =" in sql


def test_student_filtering_by_another_student_id_returns_403(mock_db):
    """Scenario 4: Student filtering by another student's ID raises 403 Forbidden."""
    my_id = uuid4()
    other_id = uuid4()
    student = make_user(mock_db, role=UserRole.STUDENT, user_id=my_id)

    with pytest.raises(HTTPException) as exc:
        list_quantity(mock_db, student, student_id=other_id)
    assert exc.value.status_code == 403
    assert "Cannot filter another borrower's history" in exc.value.detail


def test_student_with_omitted_student_id_sees_only_own_records(mock_db):
    """Scenario 5: Student omitting student_id automatically gets scoped to own student_id."""
    student_id = uuid4()
    student = make_user(mock_db, role=UserRole.STUDENT, user_id=student_id)
    list_quantity(mock_db, student, student_id=None)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.student_id =" in sql


def test_equipment_id_filtering(mock_db):
    """Scenario 6: Equipment ID filtering applies exact UUID match against quantity_loans.equipment_id."""
    staff = make_user(mock_db, role=UserRole.LAB_ASSISTANT)
    eq_id = uuid4()
    list_quantity(mock_db, staff, equipment_id=eq_id)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.equipment_id =" in sql
    assert "JOIN" not in sql


def test_borrowed_status_filtering(mock_db):
    """Scenario 7: BORROWED status filters quantity_loans.returned_at IS NULL."""
    staff = make_user(mock_db, role=UserRole.ADMIN)
    list_quantity(mock_db, staff, status="BORROWED")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.returned_at IS NULL" in sql


def test_returned_status_filtering(mock_db):
    """Scenario 8: RETURNED status filters quantity_loans.returned_at IS NOT NULL."""
    staff = make_user(mock_db, role=UserRole.ADMIN)
    list_quantity(mock_db, staff, status="RETURNED")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.returned_at IS NOT NULL" in sql


def test_service_invalid_status_returns_422(mock_db):
    """Scenario 9 (service): Invalid status value raises 422 Unprocessable Entity."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    with pytest.raises(HTTPException) as exc:
        list_quantity(mock_db, staff, status="LOST")
    assert exc.value.status_code == 422


def test_existing_overdue_true_behavior(mock_db):
    """Scenario 12: overdue=True filters returned_at IS NULL AND due_at < now."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    list_quantity(mock_db, staff, overdue=True)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.returned_at IS NULL" in sql
    assert "quantity_loans.due_at <" in sql


def test_existing_overdue_false_behavior(mock_db):
    """Scenario 13: overdue=False filters NOT (returned_at IS NULL AND due_at < now)."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    list_quantity(mock_db, staff, overdue=False)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.due_at <" in sql or "quantity_loans.returned_at IS NULL" in sql


def test_combined_filters(mock_db):
    """Scenario 14: Combined independent filters are combined with AND semantics."""
    staff = make_user(mock_db, role=UserRole.ADMIN)
    st_id = uuid4()
    eq_id = uuid4()
    list_quantity(mock_db, staff, student_id=st_id, equipment_id=eq_id, status="BORROWED", overdue=True)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "quantity_loans.student_id =" in sql
    assert "quantity_loans.equipment_id =" in sql
    assert "quantity_loans.returned_at IS NULL" in sql
    assert "quantity_loans.due_at <" in sql
    assert " AND " in sql


def test_overdue_boundary_due_at_equal_to_now(mock_db):
    """Scenario 15: due_at equal to now is not overdue (due_at < now is false)."""
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    loan = QuantityLoan(
        id=uuid4(),
        equipment_id=uuid4(),
        student_id=uuid4(),
        issued_by=uuid4(),
        quantity=5,
        equipment_name="Cable",
        destination="Lab",
        due_at=now,
        checked_out_at=now - timedelta(days=1),
        returned_at=None,
        returned_to=None,
        return_key=None,
    )
    view = loan_view(mock_db, loan, now=now)
    assert view.is_currently_overdue is False
    assert view.was_returned_late is False


def test_returned_loans_never_currently_overdue(mock_db):
    """Scenario 16: Returned loans are never currently overdue, even if returned after due_at."""
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    due_at = now - timedelta(days=2)
    returned_at = now - timedelta(days=1)
    loan = QuantityLoan(
        id=uuid4(),
        equipment_id=uuid4(),
        student_id=uuid4(),
        issued_by=uuid4(),
        quantity=2,
        equipment_name="Switch",
        destination="Lab",
        due_at=due_at,
        checked_out_at=due_at - timedelta(days=1),
        returned_at=returned_at,
        returned_to=uuid4(),
        return_key=uuid4(),
    )
    view = loan_view(mock_db, loan, now=now)
    assert view.is_currently_overdue is False
    assert view.was_returned_late is True


def test_pagination_bounds_and_validation(mock_db):
    """Scenario 17: Pagination validates limit (1-100) and offset (0 <= offset < 2**63)."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    with pytest.raises(HTTPException) as exc1:
        list_quantity(mock_db, staff, limit=0)
    assert exc1.value.status_code == 422

    with pytest.raises(HTTPException) as exc2:
        list_quantity(mock_db, staff, limit=101)
    assert exc2.value.status_code == 422

    with pytest.raises(HTTPException) as exc3:
        list_quantity(mock_db, staff, offset=-1)
    assert exc3.value.status_code == 422


def test_deterministic_ordering(mock_db):
    """Scenario 18: Query orders by checked_out_at DESC, id ASC for total determinism."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    list_quantity(mock_db, staff)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "ORDER BY quantity_loans.checked_out_at DESC, quantity_loans.id" in sql


def test_has_more_windowing(mock_db):
    """Scenario 19: Fetches limit + 1 rows to compute has_more accurately."""
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    mock_loans = [
        QuantityLoan(
            id=uuid4(),
            equipment_id=uuid4(),
            student_id=uuid4(),
            issued_by=uuid4(),
            quantity=1,
            equipment_name="Item",
            destination="Lab",
            due_at=now + timedelta(days=1),
            checked_out_at=now,
            returned_at=None,
            returned_to=None,
            return_key=None,
        )
        for _ in range(3)
    ]
    # Return 3 items when limit is 2 -> has_more is True, items trimmed to 2
    mock_db.scalars.return_value.all.return_value = mock_loans

    staff = make_user(mock_db, role=UserRole.FACULTY)
    res = list_quantity(mock_db, staff, limit=2, offset=0)

    assert len(res["items"]) == 2
    assert res["has_more"] is True


def test_existing_loan_detail_authorization(mock_db):
    """Scenario 20: Existing loan detail lookup respects student privacy and raises 404 for missing/unauthorized."""
    my_id = uuid4()
    loan_id = uuid4()

    student = make_user(mock_db, role=UserRole.STUDENT, user_id=my_id)

    with pytest.raises(HTTPException) as exc:
        list_quantity(mock_db, student, loan_id=loan_id)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Quantity loan not found"


def test_read_only_execution(mock_db):
    """Scenario 21: list_quantity executes within db.no_autoflush and never calls mutating methods."""
    staff = make_user(mock_db, role=UserRole.FACULTY)
    list_quantity(mock_db, staff, limit=10, offset=0, status="BORROWED", overdue=True)
    assert_read_only_db(mock_db)


def test_existing_response_schema():
    """Scenario 22: QuantityLoanPage and QuantityLoanResponse schema validation."""
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    item = QuantityLoanResponse(
        id=uuid4(),
        equipment_id=uuid4(),
        student_id=uuid4(),
        issued_by=uuid4(),
        quantity=5,
        equipment_name="Network Adapter",
        destination="Room 101",
        due_at=now + timedelta(days=2),
        checked_out_at=now,
        returned_at=None,
        returned_to=None,
        return_key=None,
        is_currently_overdue=False,
        was_returned_late=False,
    )
    page = QuantityLoanPage(items=[item], limit=25, offset=0, has_more=False)
    dumped = page.model_dump()
    assert "items" in dumped
    assert "has_more" in dumped
    assert dumped["items"][0]["quantity"] == 5


# ==============================================================================
# 2. HTTP Endpoint Integration & FastAPI Validation Tests
# ==============================================================================


def test_http_unauthenticated_access_is_denied(client_factory):
    """Scenario 23: Unauthenticated caller receives 401 Unauthorized."""
    client = client_factory(None)
    resp = client.get("/quantity-loans")
    assert resp.status_code == 401


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT])
def test_all_authorized_staff_roles_retain_access(client_factory, role):
    """Scenario 24: All authorized staff roles retain 200 OK access."""
    client = client_factory(role)
    resp = client.get("/quantity-loans")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "limit": 25, "offset": 0, "has_more": False}


def test_http_invalid_status_returns_422(client_factory):
    """Scenario 9 (HTTP): Invalid status query returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/quantity-loans?status=INVALID_STATUS")
    assert resp.status_code == 422


def test_http_invalid_student_uuid_returns_422(client_factory):
    """Scenario 10: Invalid student_id UUID query returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/quantity-loans?student_id=not-a-uuid")
    assert resp.status_code == 422


def test_http_invalid_equipment_uuid_returns_422(client_factory):
    """Scenario 11: Invalid equipment_id UUID query returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/quantity-loans?equipment_id=not-a-uuid")
    assert resp.status_code == 422


def test_http_student_filtering_by_another_student_returns_403(client_factory):
    """Student caller receives 403 when passing another student's UUID in query."""
    my_id = uuid4()
    other_id = uuid4()
    client = client_factory(UserRole.STUDENT, user_id=my_id)
    resp = client.get(f"/quantity-loans?student_id={other_id}")
    assert resp.status_code == 403
    assert "Cannot filter another borrower's history" in resp.json()["detail"]


def test_http_student_filtering_by_own_id_returns_200(client_factory):
    """Student caller receives 200 when passing own student UUID in query."""
    my_id = uuid4()
    client = client_factory(UserRole.STUDENT, user_id=my_id)
    resp = client.get(f"/quantity-loans?student_id={my_id}")
    assert resp.status_code == 200
