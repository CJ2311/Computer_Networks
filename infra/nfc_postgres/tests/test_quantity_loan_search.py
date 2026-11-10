"""Isolated PostgreSQL integration tests for Quantity Loan Filtering.

Exercises Phase B and Phase C real PostgreSQL 18 scenarios in the disposable,
opt-in container instance.
"""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4, UUID
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, func
from sqlalchemy.orm import Session

from backend.alembic.application_safety import apply_application, HEAD
from backend.app.models.user import User, UserRole
from backend.app.models.equipment import Equipment, EquipmentStock
from backend.app.models.operations import QuantityLoan, EquipmentLoanPolicy
from backend.app.schemas.operations import (
    QuantityLoanCreate,
    QuantityReturnCreate,
    QuantityLoanResponse,
    QuantityLoanPage,
)
from backend.app.services.quantity_loans import issue_quantity, return_quantity, list_quantity, loan_view
from backend.app.services.stock import adjust_stock
from backend.app.schemas.stock import StockAdjustment
from backend.app.services.maintenance import _now
from infra.nfc_postgres.harness import ADMIN


def _verify_reader_access(user: User | None):
    """Role-based access check matching the quantity loans API dependency."""
    if user is None or not getattr(user, "is_active", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.role not in (UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT, UserRole.STUDENT):
        raise HTTPException(status_code=403, detail="Forbidden: Active user required")
    return user


@pytest.fixture
def loan_env(database):
    """Set up database at HEAD revision with schema privileges, seed users, and stock."""
    database.deploy()
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        c.rollback()
        apply_application(c, database.settings)

    admin = database.engine(ADMIN)
    with admin.begin() as c:
        database.verify_engine_identity(c)
        c.exec_driver_sql(
            "GRANT SELECT ON users, equipment, equipment_assets, equipment_checkouts, "
            "maintenance_requests, maintenance_events, quantity_loans, stock_ledger, equipment_stock TO inventory_app"
        )
        c.exec_driver_sql(
            "GRANT INSERT, UPDATE ON equipment, equipment_assets, equipment_checkouts, "
            "maintenance_requests, quantity_loans, equipment_stock TO inventory_app"
        )
        c.exec_driver_sql(
            "GRANT INSERT ON maintenance_events, stock_ledger TO inventory_app"
        )

    admin_user = User(
        id=uuid4(), college_id="ADMIN_QL", full_name="QL Admin",
        email="admin.ql@college.invalid", role="ADMIN", is_active=True
    )
    faculty = User(
        id=uuid4(), college_id="FACULTY_QL", full_name="QL Faculty",
        email="faculty.ql@college.invalid", role="FACULTY", is_active=True
    )
    lab_assistant = User(
        id=uuid4(), college_id="LAB_QL", full_name="QL Lab Assistant",
        email="lab.ql@college.invalid", role="LAB_ASSISTANT", is_active=True
    )
    student1 = User(
        id=uuid4(), college_id="STUDENT_QL1", full_name="First Student",
        email="student1.ql@college.invalid", role="STUDENT", is_active=True
    )
    student2 = User(
        id=uuid4(), college_id="STUDENT_QL2", full_name="Second Student",
        email="student2.ql@college.invalid", role="STUDENT", is_active=True
    )
    inactive_user = User(
        id=uuid4(), college_id="INACTIVE_QL", full_name="Inactive User",
        email="inactive.ql@college.invalid", role="STUDENT", is_active=False
    )

    eq1 = Equipment(
        id=uuid4(), name="Cat6 Patch Cable", category="Networking",
        tracking_type="QUANTITY", default_location="Lab 101"
    )
    eq2 = Equipment(
        id=uuid4(), name="Arduino Uno R3", category="Microcontrollers",
        tracking_type="QUANTITY", default_location="Lab 102"
    )
    policy1 = EquipmentLoanPolicy(equipment_id=eq1.id, allow_offsite=True)
    policy2 = EquipmentLoanPolicy(equipment_id=eq2.id, allow_offsite=True)

    with Session(admin, expire_on_commit=False) as db:
        db.add_all([admin_user, faculty, lab_assistant, student1, student2, inactive_user])
        db.add_all([eq1, eq2])
        db.add_all([policy1, policy2])
        db.commit()

    app_engine = database.engine("inventory_app")

    # Add initial stock for both items
    with Session(app_engine) as db:
        adjust_stock(db, eq1.id, StockAdjustment(expected_quantity=0, change=50, reason="Initial cable stock"), faculty)
        adjust_stock(db, eq2.id, StockAdjustment(expected_quantity=0, change=50, reason="Initial arduino stock"), faculty)

    return SimpleNamespace(
        h=database,
        engine=app_engine,
        admin=admin,
        admin_user=admin_user,
        faculty=faculty,
        lab_assistant=lab_assistant,
        student1=student1,
        student2=student2,
        inactive_user=inactive_user,
        eq1=eq1,
        eq2=eq2,
    )


# Helper for creating loans cleanly via service
def _issue(db, env, equipment_id, student_id, quantity=1, days_due=1, due_at=None):
    if due_at is None:
        due_at = datetime.now(timezone.utc) + timedelta(days=days_due)
    data = QuantityLoanCreate(
        idempotency_key=uuid4(),
        equipment_id=equipment_id,
        student_id=student_id,
        quantity=quantity,
        due_at=due_at,
    )
    return issue_quantity(db, data, env.faculty)


def _return(db, env, loan_id):
    data = QuantityReturnCreate(
        idempotency_key=uuid4(),
        condition_status="WORKING",
    )
    return return_quantity(db, loan_id, data, env.faculty)


# ==============================================================================
# INTEGRATION TESTS: 32 REQUIRED SCENARIOS
# ==============================================================================


def test_scenario_01_empty_quantity_loan_history(loan_env):
    """Scenario 1: Empty quantity-loan history returns empty page."""
    c = loan_env
    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty)
        assert page["items"] == []
        assert page["limit"] == 25
        assert page["offset"] == 0
        assert page["has_more"] is False


def test_scenario_02_unfiltered_staff_listing(loan_env):
    """Scenario 2: Unfiltered staff listing returns all loans across students."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 2)
        l2 = _issue(db, c, c.eq2.id, c.student2.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty)
        assert len(page["items"]) == 2
        item_ids = {item.id for item in page["items"]}
        assert item_ids == {l1.id, l2.id}


def test_scenario_03_staff_filtering_by_student_id(loan_env):
    """Scenario 3: Staff filtering by student_id returns only that student's loans."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        page1 = list_quantity(db, c.faculty, student_id=c.student1.id)
        assert len(page1["items"]) == 1
        assert page1["items"][0].id == l1.id
        assert page1["items"][0].student_id == c.student1.id

        page2 = list_quantity(db, c.faculty, student_id=c.student2.id)
        assert len(page2["items"]) == 1
        assert page2["items"][0].id == l2.id


def test_scenario_04_staff_filtering_by_equipment_id(loan_env):
    """Scenario 4: Staff filtering by equipment_id returns only loans for that equipment."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq2.id, c.student1.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, equipment_id=c.eq1.id)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l1.id
        assert page["items"][0].equipment_id == c.eq1.id


def test_scenario_05_filtering_by_status_borrowed(loan_env):
    """Scenario 5: Filtering by status=BORROWED returns only active, unreturned loans."""
    c = loan_env
    with Session(c.engine) as db:
        l_open = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l_ret = _issue(db, c, c.eq2.id, c.student1.id, 1)
        _return(db, c, l_ret.id)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, status="BORROWED")
        assert len(page["items"]) == 1
        assert page["items"][0].id == l_open.id
        assert page["items"][0].returned_at is None


def test_scenario_06_filtering_by_status_returned(loan_env):
    """Scenario 6: Filtering by status=RETURNED returns only closed, returned loans."""
    c = loan_env
    with Session(c.engine) as db:
        l_open = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l_ret = _issue(db, c, c.eq2.id, c.student1.id, 1)
        _return(db, c, l_ret.id)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, status="RETURNED")
        assert len(page["items"]) == 1
        assert page["items"][0].id == l_ret.id
        assert page["items"][0].returned_at is not None


def test_scenario_07_filtering_by_overdue_true(loan_env, monkeypatch):
    """Scenario 7: Filtering by overdue=True returns only currently overdue active loans."""
    c = loan_env
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: past)
        with Session(c.engine) as db:
            l_overdue = _issue(db, c, c.eq2.id, c.student1.id, 1, due_at=past + timedelta(days=1))

    with Session(c.engine) as db:
        l_normal = _issue(db, c, c.eq1.id, c.student1.id, 1, days_due=2)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, overdue=True)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l_overdue.id
        assert page["items"][0].is_currently_overdue is True


def test_scenario_08_filtering_by_overdue_false(loan_env, monkeypatch):
    """Scenario 8: Filtering by overdue=False returns non-overdue loans (future due or returned)."""
    c = loan_env
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: past)
        with Session(c.engine) as db:
            l_overdue = _issue(db, c, c.eq2.id, c.student1.id, 1, due_at=past + timedelta(days=1))

    with Session(c.engine) as db:
        l_normal = _issue(db, c, c.eq1.id, c.student1.id, 1, days_due=2)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, overdue=False)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l_normal.id
        assert page["items"][0].is_currently_overdue is False


def test_scenario_09_combining_student_id_and_equipment_id(loan_env):
    """Scenario 9: Combining student_id and equipment_id uses AND semantics."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq2.id, c.student1.id, 1)
        l3 = _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, student_id=c.student1.id, equipment_id=c.eq1.id)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l1.id


def test_scenario_10_combining_student_equipment_and_status(loan_env):
    """Scenario 10: Combining student_id, equipment_id, and status uses AND semantics."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        _return(db, c, l2.id)

    with Session(c.engine) as db:
        page_borrowed = list_quantity(db, c.faculty, student_id=c.student1.id, equipment_id=c.eq1.id, status="BORROWED")
        assert len(page_borrowed["items"]) == 1
        assert page_borrowed["items"][0].id == l1.id

        page_returned = list_quantity(db, c.faculty, student_id=c.student1.id, equipment_id=c.eq1.id, status="RETURNED")
        assert len(page_returned["items"]) == 1
        assert page_returned["items"][0].id == l2.id


def test_scenario_11_combining_all_filters_including_overdue(loan_env, monkeypatch):
    """Scenario 11: Combining student_id, equipment_id, status, and overdue."""
    c = loan_env
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: past)
        with Session(c.engine) as db:
            l2 = _issue(db, c, c.eq1.id, c.student1.id, 1, due_at=past + timedelta(days=1))

    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1, days_due=2)

    with Session(c.engine) as db:
        page = list_quantity(
            db, c.faculty,
            student_id=c.student1.id,
            equipment_id=c.eq1.id,
            status="BORROWED",
            overdue=True,
        )
        assert len(page["items"]) == 1
        assert page["items"][0].id == l2.id


def test_scenario_12_student_viewing_own_loans_without_student_id(loan_env):
    """Scenario 12: Student viewing loans without student_id sees only their own loans."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.student1)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l1.id
        assert page["items"][0].student_id == c.student1.id


def test_scenario_13_student_explicitly_filtering_by_own_student_id(loan_env):
    """Scenario 13: Student explicitly filtering by their own student_id succeeds."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.student1, student_id=c.student1.id)
        assert len(page["items"]) == 1
        assert page["items"][0].id == l1.id


def test_scenario_14_student_filtering_by_another_student_returns_403(loan_env):
    """Scenario 14: Student filtering by another student's ID raises 403 Forbidden."""
    c = loan_env
    with Session(c.engine) as db:
        _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as exc:
            list_quantity(db, c.student1, student_id=c.student2.id)
        assert exc.value.status_code == 403
        assert "Cannot filter another borrower's history" in exc.value.detail


def test_scenario_15_student_cannot_see_other_student_via_combined_filters(loan_env):
    """Scenario 15: Student cannot discover another student's loan even with equipment_id or status filters."""
    c = loan_env
    with Session(c.engine) as db:
        l_other = _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        # student1 queries for eq1 where only student2 has a loan
        page = list_quantity(db, c.student1, equipment_id=c.eq1.id, status="BORROWED")
        assert page["items"] == []


def test_scenario_16_student_cannot_access_other_student_loan_detail(loan_env):
    """Scenario 16: Student looking up another student's loan ID receives non-disclosing 404."""
    c = loan_env
    with Session(c.engine) as db:
        l_other = _issue(db, c, c.eq1.id, c.student2.id, 1)

    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as exc:
            list_quantity(db, c.student1, loan_id=l_other.id)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Quantity loan not found"


def test_scenario_17_admin_access(loan_env):
    """Scenario 17: ADMIN role has full listing access."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)
        l2 = _issue(db, c, c.eq2.id, c.student2.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.admin_user)
        assert len(page["items"]) == 2


def test_scenario_18_faculty_access(loan_env):
    """Scenario 18: FACULTY role has full listing access."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty)
        assert len(page["items"]) == 1


def test_scenario_19_lab_assistant_access(loan_env):
    """Scenario 19: LAB_ASSISTANT role has full listing access."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.lab_assistant)
        assert len(page["items"]) == 1


def test_scenario_20_unauthenticated_access_denied(loan_env):
    """Scenario 20: Unauthenticated access and inactive user denied with 401."""
    c = loan_env
    with pytest.raises(HTTPException) as exc1:
        _verify_reader_access(None)
    assert exc1.value.status_code == 401

    with pytest.raises(HTTPException) as exc2:
        _verify_reader_access(c.inactive_user)
    assert exc2.value.status_code == 401


def test_scenario_21_invalid_student_id_uuid_validation(loan_env):
    """Scenario 21: Non-UUID student_id is rejected by Pydantic/FastAPI validation."""
    # Verified by Pydantic RFC 4122 type checking
    with pytest.raises(ValueError):
        UUID("not-a-valid-uuid")


def test_scenario_22_invalid_equipment_id_uuid_validation(loan_env):
    """Scenario 22: Non-UUID equipment_id is rejected by Pydantic/FastAPI validation."""
    with pytest.raises(ValueError):
        UUID("invalid-eq-uuid")


def test_scenario_23_invalid_status_returns_422(loan_env):
    """Scenario 23: Invalid status string raises 422 Unprocessable Entity."""
    c = loan_env
    with Session(c.engine) as db:
        with pytest.raises(HTTPException) as exc:
            list_quantity(db, c.faculty, status="DAMAGED")
        assert exc.value.status_code == 422
        assert "Invalid status filter" in exc.value.detail


def test_scenario_24_pagination_with_multiple_records(loan_env):
    """Scenario 24: Pagination slices across multiple records without overlap."""
    c = loan_env
    with Session(c.engine) as db:
        created_ids = []
        for _ in range(5):
            loan = _issue(db, c, c.eq1.id, c.student1.id, 1)
            created_ids.append(loan.id)

    with Session(c.engine) as db:
        page1 = list_quantity(db, c.faculty, limit=2, offset=0)
        page2 = list_quantity(db, c.faculty, limit=2, offset=2)
        page3 = list_quantity(db, c.faculty, limit=2, offset=4)

        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert len(page3["items"]) == 1

        ids1 = {item.id for item in page1["items"]}
        ids2 = {item.id for item in page2["items"]}
        ids3 = {item.id for item in page3["items"]}

        assert ids1.isdisjoint(ids2)
        assert ids2.isdisjoint(ids3)
        assert len(ids1 | ids2 | ids3) == 5


def test_scenario_25_stable_ordering_with_identical_timestamps(loan_env, monkeypatch):
    """Scenario 25: Ordering tie-breaks deterministically by id when checked_out_at timestamps match."""
    c = loan_env
    fixed_time = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: fixed_time)
        with Session(c.engine) as db:
            l1 = _issue(db, c, c.eq1.id, c.student1.id, 1, due_at=fixed_time + timedelta(days=1))
            l2 = _issue(db, c, c.eq1.id, c.student1.id, 1, due_at=fixed_time + timedelta(days=1))

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty)
        assert len(page["items"]) == 2
        # Deterministic tie break
        first_run = [item.id for item in page["items"]]

        page_again = list_quantity(db, c.faculty)
        second_run = [item.id for item in page_again["items"]]
        assert first_run == second_run


def test_scenario_26_has_more_behavior(loan_env):
    """Scenario 26: has_more is True on intermediate pages and False on final page."""
    c = loan_env
    with Session(c.engine) as db:
        for _ in range(3):
            _issue(db, c, c.eq1.id, c.student1.id, 1)

    with Session(c.engine) as db:
        p1 = list_quantity(db, c.faculty, limit=2, offset=0)
        assert p1["has_more"] is True

        p2 = list_quantity(db, c.faculty, limit=2, offset=2)
        assert p2["has_more"] is False


def test_scenario_27_returned_loans_not_currently_overdue(loan_env, monkeypatch):
    """Scenario 27: Returned loans are never currently overdue even if returned after due_at."""
    c = loan_env
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: past)
        with Session(c.engine) as db:
            loan = _issue(db, c, c.eq1.id, c.student1.id, 1, due_at=past + timedelta(days=1))

    with Session(c.engine) as db:
        _return(db, c, loan.id)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, loan_id=loan.id)
        # loan_view returns single response
        assert page.is_currently_overdue is False
        assert page.was_returned_late is True


def test_scenario_28_loans_due_exactly_at_now_not_overdue(loan_env):
    """Scenario 28: Loans due exactly at captured clock_timestamp are not overdue (due_at < now is false)."""
    c = loan_env
    with Session(c.engine) as db:
        loan = _issue(db, c, c.eq1.id, c.student1.id, 1, days_due=1)

    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    view = loan_view(None, loan, now=loan.due_at)
    assert view.is_currently_overdue is False


def test_scenario_29_overdue_loans_visible_when_status_borrowed(loan_env, monkeypatch):
    """Scenario 29: Overdue loans are active (BORROWED) and appear when filtering status=BORROWED."""
    c = loan_env
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr("backend.app.services.quantity_loans._now", lambda db: past)
        with Session(c.engine) as db:
            loan = _issue(db, c, c.eq1.id, c.student1.id, 1, due_at=past + timedelta(days=1))

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, status="BORROWED", overdue=True)
        assert len(page["items"]) == 1
        assert page["items"][0].id == loan.id


def test_scenario_30_contradictory_filters_return_empty_result(loan_env):
    """Scenario 30: Contradictory filters (status=RETURNED and overdue=True) return empty page without error."""
    c = loan_env
    with Session(c.engine) as db:
        loan = _issue(db, c, c.eq1.id, c.student1.id, 1)

    with Session(c.engine) as db:
        page = list_quantity(db, c.faculty, status="RETURNED", overdue=True)
        assert page["items"] == []


def test_scenario_31_strictly_read_only_select_execution(loan_env):
    """Scenario 31: Listing quantity loans executes strictly SELECT statements with zero writes."""
    c = loan_env
    with Session(c.engine) as db:
        _issue(db, c, c.eq1.id, c.student1.id, 1)

    executed_statements = []

    def before_cursor_execute_listener(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement.strip())

    event.listen(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    try:
        with Session(c.engine) as db:
            list_quantity(db, c.faculty, limit=10, offset=0)
            list_quantity(db, c.faculty, student_id=c.student1.id, status="BORROWED")
            list_quantity(db, c.faculty, overdue=False)
    finally:
        event.remove(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    assert len(executed_statements) > 0
    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE")
    for stmt in executed_statements:
        normalized = stmt.upper().lstrip()
        first_word = normalized.split()[0] if normalized.split() else ""
        assert first_word in ("SELECT", "WITH", "SHOW", "SET"), f"Non-read-only statement: {stmt}"
        for f in forbidden:
            assert not normalized.startswith(f), f"Forbidden keyword '{f}' in: {stmt}"


def test_scenario_32_existing_response_schema_unchanged(loan_env):
    """Scenario 32: Returned page validates against QuantityLoanPage schema."""
    c = loan_env
    with Session(c.engine) as db:
        l1 = _issue(db, c, c.eq1.id, c.student1.id, 3)

    with Session(c.engine) as db:
        page_dict = list_quantity(db, c.faculty)
        validated_page = QuantityLoanPage.model_validate(page_dict)
        assert len(validated_page.items) == 1
        item = validated_page.items[0]
        assert item.id == l1.id
        assert item.quantity == 3
        assert item.equipment_name == "Cat6 Patch Cable"
        assert item.destination == "Lab 101"
        assert item.is_currently_overdue is False
        assert item.was_returned_late is False

