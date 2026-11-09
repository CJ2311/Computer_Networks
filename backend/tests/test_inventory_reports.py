"""Database-free unit tests for inventory reporting and analytics."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid4

from fastapi import FastAPI
import pytest

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.models.user import User, UserRole
from backend.app.routers.reports import router as reports_router
from backend.app.schemas.reports import (
    AssetAvailabilitySummary,
    AssetConditionSummary,
    CategoryReportResponse,
    CategoryRollup,
    InventoryOverviewReport,
    MaintenanceReportItem,
    MaintenanceReportPage,
    MissingReportItem,
    MissingReportPage,
    OperationalAlertsSummary,
    QuantityStockSummary,
    StockReportItem,
    StockReportPage,
)
from backend.app.services.reports import (
    get_category_report,
    get_inventory_overview,
    get_maintenance_report,
    get_missing_report,
    get_stock_report,
)


@pytest.fixture
def test_now():
    return datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_db():
    db = Mock()
    return db


def assert_read_only_db(db: Mock):
    for method in ("add", "delete", "flush", "commit", "rollback", "refresh"):
        getattr(db, method).assert_not_called()


# ==============================================================================
# 1. Overview Report Tests
# ==============================================================================


def test_overview_empty_inventory(mock_db, test_now):
    empty_row = {
        "total_catalog": 0,
        "ind_catalog": 0,
        "qty_catalog": 0,
        "total_assets": 0,
        "avail_count": 0,
        "borrowed_count": 0,
        "reserved_count": 0,
        "maintenance_count": 0,
        "retired_count": 0,
        "working_count": 0,
        "damaged_count": 0,
        "repair_count": 0,
        "missing_count": 0,
        "hold_count": 0,
        "on_hand_units": 0,
        "loaned_units": 0,
        "active_quantity_loans": 0,
        "overdue_quantity_loans": 0,
        "active_checkouts": 0,
        "overdue_checkouts": 0,
        "open_incidents": 0,
        "active_maintenance": 0,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = empty_row

    res = get_inventory_overview(mock_db, as_of=test_now)

    assert_read_only_db(mock_db)
    assert res.as_of == test_now
    assert res.total_catalog_items == 0
    assert res.assets.total_physical_assets == 0
    assert res.assets.available == 0
    assert res.conditions.working == 0
    assert res.quantity_stock.on_hand_units == 0
    assert res.quantity_stock.accounted_units == 0
    assert res.alerts.active_individual_checkouts == 0


def test_overview_individual_only_reconciliation(mock_db, test_now):
    row = {
        "total_catalog": 5,
        "ind_catalog": 5,
        "qty_catalog": 0,
        "total_assets": 20,
        "avail_count": 10,
        "borrowed_count": 4,
        "reserved_count": 2,
        "maintenance_count": 3,
        "retired_count": 1,
        "working_count": 12,
        "damaged_count": 3,
        "repair_count": 2,
        "missing_count": 3,
        "hold_count": 6,
        "on_hand_units": 0,
        "loaned_units": 0,
        "active_quantity_loans": 0,
        "overdue_quantity_loans": 0,
        "active_checkouts": 4,
        "overdue_checkouts": 1,
        "open_incidents": 3,
        "active_maintenance": 4,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = row

    res = get_inventory_overview(mock_db, as_of=test_now)

    # Mathematical consistency checks:
    # 1. Sum of availability counts == total physical assets
    avail_sum = (
        res.assets.available
        + res.assets.borrowed
        + res.assets.reserved
        + res.assets.maintenance
        + res.assets.retired
    )
    assert avail_sum == res.assets.total_physical_assets == 20

    # 2. Sum of condition counts == total physical assets
    cond_sum = (
        res.conditions.working
        + res.conditions.damaged
        + res.conditions.under_repair
        + res.conditions.missing
    )
    assert cond_sum == res.conditions.total_physical_assets == 20

    # 3. Quantity stock is 0
    assert res.quantity_stock.catalog_items == 0
    assert res.quantity_stock.accounted_units == 0


def test_overview_quantity_only_accounting(mock_db, test_now):
    row = {
        "total_catalog": 3,
        "ind_catalog": 0,
        "qty_catalog": 3,
        "total_assets": 0,
        "avail_count": 0,
        "borrowed_count": 0,
        "reserved_count": 0,
        "maintenance_count": 0,
        "retired_count": 0,
        "working_count": 0,
        "damaged_count": 0,
        "repair_count": 0,
        "missing_count": 0,
        "hold_count": 0,
        "on_hand_units": 45,
        "loaned_units": 15,
        "active_quantity_loans": 5,
        "overdue_quantity_loans": 1,
        "active_checkouts": 0,
        "overdue_checkouts": 0,
        "open_incidents": 0,
        "active_maintenance": 0,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = row

    res = get_inventory_overview(mock_db, as_of=test_now)

    assert res.total_catalog_items == 3
    assert res.individual_catalog_items == 0
    assert res.quantity_catalog_items == 3
    assert res.assets.total_physical_assets == 0
    assert res.quantity_stock.on_hand_units == 45
    assert res.quantity_stock.active_loaned_units == 15
    assert res.quantity_stock.accounted_units == 60
    assert res.alerts.active_quantity_loans == 5
    assert res.alerts.overdue_quantity_loans == 1


def test_overview_borrowed_missing_overlap_no_double_count(mock_db, test_now):
    # Exactly 1 asset: borrowed by student, then reported missing during loan.
    row = {
        "total_catalog": 1,
        "ind_catalog": 1,
        "qty_catalog": 0,
        "total_assets": 1,
        "avail_count": 0,
        "borrowed_count": 1,  # Retains BORROWED custody
        "reserved_count": 0,
        "maintenance_count": 0,
        "retired_count": 0,
        "working_count": 0,
        "damaged_count": 0,
        "repair_count": 0,
        "missing_count": 1,  # Condition is MISSING
        "hold_count": 1,     # Maintenance hold is TRUE
        "on_hand_units": 0,
        "loaned_units": 0,
        "active_quantity_loans": 0,
        "overdue_quantity_loans": 0,
        "active_checkouts": 1,
        "overdue_checkouts": 0,
        "open_incidents": 1,
        "active_maintenance": 0,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = row

    res = get_inventory_overview(mock_db, as_of=test_now)

    assert res.assets.total_physical_assets == 1
    assert res.assets.borrowed == 1
    assert res.assets.available == 0
    assert res.conditions.missing == 1
    assert res.alerts.active_individual_checkouts == 1
    assert res.alerts.open_missing_incidents == 1
    assert res.alerts.assets_on_maintenance_hold == 1


def test_overview_mixed_inventory(mock_db, test_now):
    # Both individual physical assets and quantity stock
    row = {
        "total_catalog": 8,
        "ind_catalog": 5,
        "qty_catalog": 3,
        "total_assets": 15,
        "avail_count": 8,
        "borrowed_count": 4,
        "reserved_count": 1,
        "maintenance_count": 1,
        "retired_count": 1,
        "working_count": 11,
        "damaged_count": 2,
        "repair_count": 1,
        "missing_count": 1,
        "hold_count": 2,
        "on_hand_units": 120,
        "loaned_units": 30,
        "active_quantity_loans": 4,
        "overdue_quantity_loans": 0,
        "active_checkouts": 4,
        "overdue_checkouts": 0,
        "open_incidents": 1,
        "active_maintenance": 1,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = row

    res = get_inventory_overview(mock_db, as_of=test_now)

    assert res.total_catalog_items == 8
    assert res.individual_catalog_items == 5
    assert res.quantity_catalog_items == 3
    assert res.assets.total_physical_assets == 15
    assert res.quantity_stock.on_hand_units == 120
    assert res.quantity_stock.active_loaned_units == 30
    assert res.quantity_stock.accounted_units == 150
    # Strict separation: quantity units are never added to physical assets
    assert res.assets.total_physical_assets != res.quantity_stock.accounted_units


def test_overview_retired_assets_isolation(mock_db, test_now):
    # Inventory with retired assets
    row = {
        "total_catalog": 2,
        "ind_catalog": 2,
        "qty_catalog": 0,
        "total_assets": 10,
        "avail_count": 5,
        "borrowed_count": 2,
        "reserved_count": 0,
        "maintenance_count": 0,
        "retired_count": 3,  # 3 retired
        "working_count": 7,
        "damaged_count": 3,
        "repair_count": 0,
        "missing_count": 0,
        "hold_count": 0,
        "on_hand_units": 0,
        "loaned_units": 0,
        "active_quantity_loans": 0,
        "overdue_quantity_loans": 0,
        "active_checkouts": 2,
        "overdue_checkouts": 0,
        "open_incidents": 0,
        "active_maintenance": 0,
    }
    mock_db.execute.return_value.mappings.return_value.one.return_value = row

    res = get_inventory_overview(mock_db, as_of=test_now)

    assert res.assets.total_physical_assets == 10
    assert res.assets.retired == 3
    # Operational availability excludes retired assets:
    operational_assets = res.assets.available + res.assets.borrowed + res.assets.reserved + res.assets.maintenance
    assert operational_assets == 7
    assert operational_assets + res.assets.retired == res.assets.total_physical_assets


# ==============================================================================
# 2. Category Report Tests
# ==============================================================================


def test_category_report_empty(mock_db, test_now):
    mock_db.execute.return_value.mappings.return_value.all.return_value = []

    res = get_category_report(mock_db, as_of=test_now)
    assert_read_only_db(mock_db)
    assert res.as_of == test_now
    assert res.categories == []


def test_category_report_aggregation_and_reconciliation(mock_db, test_now):
    rows = [
        {
            "category": "Microcontrollers",
            "catalog_items": 3,
            "ind_catalog": 2,
            "qty_catalog": 1,
            "total_physical_assets": 10,
            "available_assets": 6,
            "borrowed_assets": 2,
            "reserved_assets": 1,
            "maintenance_assets": 1,
            "retired_assets": 0,
            "working_assets": 8,
            "damaged_assets": 1,
            "under_repair_assets": 0,
            "missing_assets": 1,
            "quantity_on_hand": 25,
            "quantity_on_loan": 10,
        },
        {
            "category": "Test Equipment",
            "catalog_items": 1,
            "ind_catalog": 1,
            "qty_catalog": 0,
            "total_physical_assets": 2,
            "available_assets": 1,
            "borrowed_assets": 0,
            "reserved_assets": 0,
            "maintenance_assets": 0,
            "retired_assets": 1,
            "working_assets": 1,
            "damaged_assets": 0,
            "under_repair_assets": 0,
            "missing_assets": 0,
            "quantity_on_hand": 0,
            "quantity_on_loan": 0,
        },
    ]
    mock_db.execute.return_value.mappings.return_value.all.return_value = rows

    res = get_category_report(mock_db, as_of=test_now)

    assert len(res.categories) == 2
    mc = res.categories[0]
    assert mc.category == "Microcontrollers"
    assert mc.total_physical_assets == 10
    # Reconciliation within category:
    assert mc.available_assets + mc.borrowed_assets + mc.reserved_assets + mc.maintenance_assets + mc.retired_assets == 10
    assert mc.working_assets + mc.damaged_assets + mc.under_repair_assets + mc.missing_assets == 10
    assert mc.quantity_on_hand == 25
    assert mc.quantity_on_loan == 10
    assert mc.accounted_quantity_units == 35


# ==============================================================================
# 3. Stock Report Tests
# ==============================================================================


def test_stock_report_empty(mock_db, test_now):
    mock_db.scalar.return_value = 0
    mock_db.execute.return_value.all.return_value = []

    res = get_stock_report(mock_db, as_of=test_now)
    assert_read_only_db(mock_db)
    assert res.total == 0
    assert res.items == []
    assert res.has_more is False


def test_stock_report_accounting_and_pagination(mock_db, test_now):
    from types import SimpleNamespace
    mock_db.scalar.return_value = 2
    eq_id1 = uuid4()
    eq_id2 = uuid4()

    row1 = SimpleNamespace(
        equipment_id=eq_id1,
        name="Resistor Kit",
        category="Passive Components",
        default_location="Drawer A1",
        on_hand_quantity=50,
        active_loaned_quantity=10,
        active_loan_count=2,
        overdue_loan_count=0,
    )
    row2 = SimpleNamespace(
        equipment_id=eq_id2,
        name="Jumper Wires",
        category="Connectors",
        default_location="Bin B2",
        on_hand_quantity=0,
        active_loaned_quantity=5,
        active_loan_count=1,
        overdue_loan_count=1,
    )
    mock_db.execute.return_value.all.return_value = [row1, row2]

    res = get_stock_report(mock_db, as_of=test_now, limit=25, offset=0)

    assert res.total == 2
    assert len(res.items) == 2
    assert res.items[0].on_hand_quantity == 50
    assert res.items[0].active_loaned_quantity == 10
    assert res.items[0].accounted_quantity == 60

    # Item 2 is out of stock on hand
    assert res.items[1].on_hand_quantity == 0
    assert res.items[1].active_loaned_quantity == 5
    assert res.items[1].accounted_quantity == 5
    assert res.has_more is False


def test_stock_report_out_of_stock_filtering(mock_db, test_now):
    from types import SimpleNamespace
    mock_db.scalar.return_value = 1
    eq_id = uuid4()
    row = SimpleNamespace(
        equipment_id=eq_id,
        name="Empty Bins",
        category="Storage",
        default_location="Bay 1",
        on_hand_quantity=0,
        active_loaned_quantity=10,
        active_loan_count=1,
        overdue_loan_count=0,
    )
    mock_db.execute.return_value.all.return_value = [row]

    res = get_stock_report(mock_db, as_of=test_now, out_of_stock_only=True)

    assert res.total == 1
    assert res.items[0].on_hand_quantity == 0
    assert res.items[0].active_loaned_quantity == 10
    assert res.items[0].accounted_quantity == 10


# ==============================================================================
# 4. Missing Report Tests
# ==============================================================================


def test_missing_report_durations_and_reconciliation(mock_db, test_now):
    # open_count = 1, reconciled_count = 1
    mock_db.scalar.side_effect = [1, 1, 2]  # open_count, reconciled_count, total

    inc1_id = uuid4()
    inc2_id = uuid4()
    reported1 = test_now - timedelta(days=5, hours=2)
    reported2 = test_now - timedelta(days=10)
    reconciled2 = test_now - timedelta(days=3)

    row1 = Mock(
        incident_id=inc1_id,
        asset_id=uuid4(),
        asset_code="DEV-001",
        equipment_id=uuid4(),
        equipment_name="SDR Transceiver",
        category="RF",
        incident_status="OPEN",
        reported_at=reported1,
        reported_by_id=uuid4(),
        reported_by_name="Dr. Smith",
        report_reason="Missing after lab test",
        police_report_number="PR-1001",
        previous_availability_status="AVAILABLE",
        current_asset_availability="MAINTENANCE",
        current_asset_condition="MISSING",
        current_maintenance_hold=True,
        reconciled_at=None,
        reconciled_by_id=None,
        reconciled_by_name=None,
        reconcile_reason=None,
        recovered_condition=None,
    )

    row2 = Mock(
        incident_id=inc2_id,
        asset_id=uuid4(),
        asset_code="DEV-002",
        equipment_id=uuid4(),
        equipment_name="Oscilloscope",
        category="Instruments",
        incident_status="RECONCILED",
        reported_at=reported2,
        reported_by_id=uuid4(),
        reported_by_name="Prof. Davis",
        report_reason="Lost during project checkout",
        police_report_number=None,
        previous_availability_status="BORROWED",
        current_asset_availability="AVAILABLE",
        current_asset_condition="WORKING",
        current_maintenance_hold=False,
        reconciled_at=reconciled2,
        reconciled_by_id=uuid4(),
        reconciled_by_name="Assistant Alex",
        reconcile_reason="Recovered from staging room",
        recovered_condition="WORKING",
    )

    mock_db.execute.return_value.all.return_value = [row1, row2]

    res = get_missing_report(mock_db, as_of=test_now)

    assert res.open_incident_count == 1
    assert res.reconciled_incident_count == 1
    assert res.total == 2
    assert len(res.items) == 2

    # Open incident duration: as_of - reported_at = 5 days 2 hours -> 5 days
    assert res.items[0].incident_status == "OPEN"
    assert res.items[0].days_missing == 5
    assert res.items[0].was_on_loan_when_reported is False

    # Reconciled incident duration: reconciled_at - reported_at = 7 days
    assert res.items[1].incident_status == "RECONCILED"
    assert res.items[1].days_missing == 7
    assert res.items[1].was_on_loan_when_reported is True


def test_missing_report_nonnegative_clock_skew(mock_db, test_now):
    mock_db.scalar.side_effect = [1, 0, 1]
    # Reported in future relative to as_of
    future_reported = test_now + timedelta(hours=1)
    row = Mock(
        incident_id=uuid4(),
        asset_id=uuid4(),
        asset_code="DEV-003",
        equipment_id=uuid4(),
        equipment_name="Router",
        category="Networking",
        incident_status="OPEN",
        reported_at=future_reported,
        reported_by_id=uuid4(),
        reported_by_name="Staff",
        report_reason="Reported",
        police_report_number=None,
        previous_availability_status="AVAILABLE",
        current_asset_availability="MAINTENANCE",
        current_asset_condition="MISSING",
        current_maintenance_hold=True,
        reconciled_at=None,
        reconciled_by_id=None,
        reconciled_by_name=None,
        reconcile_reason=None,
        recovered_condition=None,
    )
    mock_db.execute.return_value.all.return_value = [row]

    res = get_missing_report(mock_db, as_of=test_now)
    # Must be nonnegative
    assert res.items[0].days_missing == 0


# ==============================================================================
# 5. Maintenance Report Tests
# ==============================================================================


def test_maintenance_report_counters_and_statuses(mock_db, test_now):
    # open_count = 2, in_progress_count = 1, awaiting_count = 1, total_matching = 4
    mock_db.scalar.side_effect = [2, 1, 1, 4]

    reported = test_now - timedelta(days=3, hours=5)
    row = Mock(
        request_id=uuid4(),
        asset_id=uuid4(),
        asset_code="DEV-004",
        equipment_id=uuid4(),
        equipment_name="Spectrum Analyzer",
        category="RF",
        issue_type="DAMAGE",
        issue_description="Burned connector pin",
        status="IN_PROGRESS",
        reported_at=reported,
        reported_by_id=uuid4(),
        reported_by_name="Lab Tech",
        assigned_to_id=uuid4(),
        assigned_to_name="Repair Specialist",
        current_asset_availability="MAINTENANCE",
        current_asset_condition="UNDER_REPAIR",
        current_maintenance_hold=True,
    )
    mock_db.execute.return_value.all.return_value = [row]

    res = get_maintenance_report(mock_db, as_of=test_now, status_filter="ACTIVE")

    assert res.open_count == 2
    assert res.in_progress_count == 1
    assert res.awaiting_verification_count == 1
    assert res.total_active_count == 4
    assert res.total == 4
    assert len(res.items) == 1
    assert res.items[0].days_open == 3
    assert res.items[0].is_under_repair is True


@pytest.mark.parametrize(
    "status_filter",
    ["OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION", "COMPLETED", "CANCELLED", "ALL"],
)
def test_maintenance_request_all_statuses(mock_db, test_now, status_filter):
    mock_db.scalar.side_effect = [1, 0, 0, 1]
    mock_db.execute.return_value.all.return_value = []
    res = get_maintenance_report(mock_db, as_of=test_now, status_filter=status_filter)
    assert res.as_of == test_now
    assert res.total == 1
    assert res.items == []


def test_pagination_calculation_and_bounds():
    page = StockReportPage(
        as_of=datetime.now(timezone.utc),
        items=[],
        total=50,
        limit=25,
        offset=0,
        has_more=True,
    )
    assert page.has_more is True

    last_page = StockReportPage(
        as_of=datetime.now(timezone.utc),
        items=[],
        total=50,
        limit=25,
        offset=50,
        has_more=False,
    )
    assert last_page.has_more is False


def test_pydantic_schema_extra_fields_forbidden():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AssetAvailabilitySummary(
            available=1, borrowed=0, reserved=0, maintenance=0, retired=0,
            total_physical_assets=1, extra_forbidden_field=123
        )


# ==============================================================================
# 6. HTTP Authorization & RBAC Tests
# ==============================================================================


# ==============================================================================
# 6. Route & RBAC Dependency Verification
# ==============================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/reports/inventory/overview",
        "/reports/inventory/categories",
        "/reports/inventory/stock",
        "/reports/inventory/missing",
        "/reports/inventory/maintenance",
    ],
)
def test_reports_router_routes_enforce_staff_role_dependency(path):
    from fastapi.routing import APIRoute
    route = next(
        r for r in reports_router.routes
        if isinstance(r, APIRoute) and r.path == path
    )
    check = next(d.call for d in route.dependant.dependencies if d.name == "_actor")

    # Staff roles permitted
    for role in ("ADMIN", "FACULTY", "LAB_ASSISTANT"):
        user = User(id=uuid4(), role=role, is_active=True, college_id=f"DEV-{role}")
        assert check(current_user=user) is user

    # Student role rejected with 403 Forbidden
    student = User(id=uuid4(), role="STUDENT", is_active=True, college_id="DEV-STUDENT")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        check(current_user=student)
    assert exc.value.status_code == 403
    assert "permission" in exc.value.detail.lower()


def test_overview_route_handler(mock_db):
    mock_db.execute.return_value.mappings.return_value.one.return_value = {
        "total_catalog": 0, "ind_catalog": 0, "qty_catalog": 0,
        "total_assets": 0, "avail_count": 0, "borrowed_count": 0,
        "reserved_count": 0, "maintenance_count": 0, "retired_count": 0,
        "working_count": 0, "damaged_count": 0, "repair_count": 0,
        "missing_count": 0, "hold_count": 0, "on_hand_units": 0,
        "loaned_units": 0, "active_quantity_loans": 0,
        "overdue_quantity_loans": 0, "active_checkouts": 0,
        "overdue_checkouts": 0, "open_incidents": 0,
        "active_maintenance": 0,
    }
    from backend.app.routers.reports import overview_report
    staff = User(id=uuid4(), role="FACULTY", is_active=True)
    res = overview_report(db=mock_db, _actor=staff)
    assert res.as_of is not None
    assert res.as_of.tzinfo is not None
    assert res.total_catalog_items == 0


def test_categories_route_handler(mock_db):
    mock_db.execute.return_value.mappings.return_value.all.return_value = []
    from backend.app.routers.reports import categories_report
    staff = User(id=uuid4(), role="ADMIN", is_active=True)
    res = categories_report(category="Test", db=mock_db, _actor=staff)
    assert res.as_of is not None
    assert res.as_of.tzinfo is not None
    assert res.categories == []


def test_stock_route_handler(mock_db):
    mock_db.scalar.return_value = 0
    mock_db.execute.return_value.all.return_value = []
    from backend.app.routers.reports import stock_report
    staff = User(id=uuid4(), role="LAB_ASSISTANT", is_active=True)
    res = stock_report(
        out_of_stock_only=True,
        category="Test",
        search=None,
        limit=25,
        offset=0,
        db=mock_db,
        _actor=staff,
    )
    assert res.as_of is not None
    assert res.as_of.tzinfo is not None
    assert res.total == 0
    assert res.items == []


def test_missing_route_handler(mock_db):
    mock_db.scalar.side_effect = [0, 0, 0]
    mock_db.execute.return_value.all.return_value = []
    from backend.app.routers.reports import missing_report
    staff = User(id=uuid4(), role="FACULTY", is_active=True)
    res = missing_report(
        status="OPEN",
        category=None,
        limit=25,
        offset=0,
        db=mock_db,
        _actor=staff,
    )
    assert res.as_of is not None
    assert res.as_of.tzinfo is not None
    assert res.open_incident_count == 0
    assert res.items == []


def test_maintenance_route_handler(mock_db):
    mock_db.scalar.side_effect = [0, 0, 0, 0]
    mock_db.execute.return_value.all.return_value = []
    from backend.app.routers.reports import maintenance_report
    staff = User(id=uuid4(), role="ADMIN", is_active=True)
    res = maintenance_report(
        status="ACTIVE",
        issue_type=None,
        limit=25,
        offset=0,
        db=mock_db,
        _actor=staff,
    )
    assert res.as_of is not None
    assert res.as_of.tzinfo is not None
    assert res.total_active_count == 0
    assert res.items == []


def test_reports_openapi_contract_no_public_as_of():
    """Verify that no reporting endpoint exposes 'as_of' as a public query parameter."""
    app = FastAPI()
    app.include_router(reports_router)
    schema = app.openapi()

    report_paths = [
        "/reports/inventory/overview",
        "/reports/inventory/categories",
        "/reports/inventory/stock",
        "/reports/inventory/missing",
        "/reports/inventory/maintenance",
    ]

    for path in report_paths:
        assert path in schema["paths"], f"Path {path} missing in OpenAPI schema"
        endpoint = schema["paths"][path].get("get", {})
        parameters = endpoint.get("parameters", [])
        param_names = [p.get("name") for p in parameters]
        assert "as_of" not in param_names, (
            f"Endpoint {path} must NOT expose 'as_of' as a query parameter! Found: {param_names}"
        )


def test_pagination_offset_beyond_total(mock_db, test_now):
    """When offset >= total, items should be empty, has_more False, total preserved."""
    # Stock report
    mock_db.scalar.return_value = 10
    mock_db.execute.return_value.all.return_value = []
    stock_page = get_stock_report(mock_db, as_of=test_now, limit=25, offset=50)
    assert stock_page.total == 10
    assert stock_page.items == []
    assert stock_page.has_more is False
    assert stock_page.offset == 50

    # Missing report
    mock_db.scalar.side_effect = [3, 2, 5]  # open, reconciled, total
    mock_db.execute.return_value.all.return_value = []
    missing_page = get_missing_report(mock_db, as_of=test_now, limit=25, offset=10)
    assert missing_page.total == 5
    assert missing_page.items == []
    assert missing_page.has_more is False
    assert missing_page.offset == 10

    # Maintenance report
    mock_db.scalar.side_effect = [1, 1, 1, 3]  # open, in_prog, awaiting, total
    mock_db.execute.return_value.all.return_value = []
    maint_page = get_maintenance_report(mock_db, as_of=test_now, limit=25, offset=10)
    assert maint_page.total == 3
    assert maint_page.items == []
    assert maint_page.has_more is False
    assert maint_page.offset == 10

