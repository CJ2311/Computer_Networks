"""Isolated PostgreSQL integration tests for Inventory Reporting & Analytics.

Exercises Phase B and Phase C real PostgreSQL 18 scenarios in the disposable,
opt-in container instance.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, func, event
from sqlalchemy.orm import Session

from backend.alembic.application_safety import apply_application, HEAD
from backend.app.models.user import User, UserRole
from backend.app.models.equipment import Equipment, EquipmentAsset, EquipmentStock
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.missing_equipment import MissingEquipmentIncident
from backend.app.models.operations import QuantityLoan
from backend.app.schemas.reports import (
    InventoryOverviewReport,
    CategoryReportResponse,
    StockReportPage,
    MissingReportPage,
    MaintenanceReportPage,
)
from backend.app.services.reports import (
    get_inventory_overview,
    get_category_report,
    get_stock_report,
    get_missing_report,
    get_maintenance_report,
)
from backend.app.services.stock import adjust_stock
from backend.app.schemas.stock import StockAdjustment
from backend.app.services import quantity_loans as loans
from backend.app.schemas.operations import QuantityLoanCreate
from backend.app.services.missing_equipment import report_asset_missing, reconcile_missing_asset
from backend.app.schemas.missing_equipment import AssetMissingReportCreate, AssetMissingReconcileCreate
from infra.nfc_postgres.harness import ADMIN


def _verify_staff_access(user: User | None):
    """Role-based access check matching the reporting API dependency."""
    if user is None or not getattr(user, "is_active", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.role not in (UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT):
        raise HTTPException(status_code=403, detail="Forbidden: Staff role required")
    return user


@pytest.fixture
def report_env(database):
    """Set up database at HEAD revision with schema privileges and seed users."""
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
            "maintenance_requests, maintenance_events TO inventory_app"
        )
        c.exec_driver_sql(
            "GRANT INSERT, UPDATE ON equipment, equipment_assets, equipment_checkouts, "
            "maintenance_requests TO inventory_app"
        )
        c.exec_driver_sql(
            "GRANT INSERT ON maintenance_events TO inventory_app"
        )

    staff = User(
        id=uuid4(), college_id="FACULTY_REP", full_name="Report Faculty",
        email="faculty.rep@college.invalid", role="FACULTY", is_active=True
    )
    student = User(
        id=uuid4(), college_id="STUDENT_REP", full_name="Report Student",
        email="student.rep@college.invalid", role="STUDENT", is_active=True
    )
    student2 = User(
        id=uuid4(), college_id="STUDENT_REP2", full_name="Second Student",
        email="student2.rep@college.invalid", role="STUDENT", is_active=True
    )

    with Session(admin, expire_on_commit=False) as db:
        db.add_all([staff, student, student2])
        db.commit()

    app_engine = database.engine("inventory_app")
    return SimpleNamespace(
        h=database,
        engine=app_engine,
        admin=admin,
        staff=staff,
        student=student,
        student2=student2,
    )


# ==============================================================================
# INTEGRATION TESTS: 20 REQUIRED SCENARIOS + READ-ONLY ENFORCEMENT
# ==============================================================================


def test_scenario_1_empty_inventory_returns_zeroed_counts(report_env):
    """Scenario 1: Empty inventory returns zeroed counts without error."""
    c = report_env
    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.total_catalog_items == 0
        assert overview.assets.total_physical_assets == 0
        assert overview.assets.available == 0
        assert overview.assets.borrowed == 0
        assert overview.assets.reserved == 0
        assert overview.assets.maintenance == 0
        assert overview.assets.retired == 0
        assert overview.conditions.working == 0
        assert overview.conditions.damaged == 0
        assert overview.conditions.under_repair == 0
        assert overview.conditions.missing == 0
        assert overview.quantity_stock.on_hand_units == 0
        assert overview.quantity_stock.active_loaned_units == 0
        assert overview.quantity_stock.accounted_units == 0
        assert overview.alerts.active_individual_checkouts == 0
        assert overview.alerts.open_missing_incidents == 0
        assert overview.alerts.active_maintenance_requests == 0

        cats = get_category_report(db)
        assert cats.categories == []

        stock = get_stock_report(db)
        assert stock.total == 0
        assert stock.items == []

        missing = get_missing_report(db)
        assert missing.total == 0
        assert missing.open_incident_count == 0
        assert missing.items == []

        maint = get_maintenance_report(db)
        assert maint.total == 0
        assert maint.total_active_count == 0
        assert maint.items == []


def test_scenario_2_individual_only_inventory_rollups(report_env):
    """Scenario 2: Individual-only inventory availability and condition rollup matches rows."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Spectrum Analyzer", category="RF",
        tracking_type="INDIVIDUAL", default_location="Lab 101"
    )
    a1 = EquipmentAsset(
        id=uuid4(), equipment_id=eq.id, asset_code="SA-001",
        location="Lab 101", availability_status="AVAILABLE", condition_status="WORKING",
        maintenance_hold=False
    )
    a2 = EquipmentAsset(
        id=uuid4(), equipment_id=eq.id, asset_code="SA-002",
        location="Lab 101", availability_status="BORROWED", condition_status="WORKING",
        maintenance_hold=False
    )
    a3 = EquipmentAsset(
        id=uuid4(), equipment_id=eq.id, asset_code="SA-003",
        location="Lab 101", availability_status="MAINTENANCE", condition_status="DAMAGED",
        maintenance_hold=True
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, a1, a2, a3])
        db.commit()

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.total_catalog_items == 1
        assert overview.assets.total_physical_assets == 3
        assert overview.assets.available == 1
        assert overview.assets.borrowed == 1
        assert overview.assets.maintenance == 1
        assert overview.conditions.working == 2
        assert overview.conditions.damaged == 1
        assert overview.quantity_stock.on_hand_units == 0
        assert overview.quantity_stock.accounted_units == 0


def test_scenario_3_quantity_only_inventory_accounting(report_env):
    """Scenario 3: Quantity-only inventory calculates on_hand and accounted with no active loans."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Cat6 Patch Cable", category="Cabling",
        tracking_type="QUANTITY", default_location="Drawer 1"
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq.id, StockAdjustment(expected_quantity=0, change=25), c.staff)

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.total_catalog_items == 1
        assert overview.assets.total_physical_assets == 0
        assert overview.quantity_stock.on_hand_units == 25
        assert overview.quantity_stock.active_loaned_units == 0
        assert overview.quantity_stock.accounted_units == 25


def test_scenario_4_mixed_inventory_no_cross_contamination(report_env):
    """Scenario 4: Mixed individual and quantity inventory calculates without cross-contamination."""
    c = report_env
    eq_ind = Equipment(
        id=uuid4(), name="Signal Generator", category="Instruments",
        tracking_type="INDIVIDUAL", default_location="Bench A"
    )
    a1 = EquipmentAsset(
        id=uuid4(), equipment_id=eq_ind.id, asset_code="SG-001",
        location="Bench A", availability_status="AVAILABLE", condition_status="WORKING",
        maintenance_hold=False
    )
    eq_qty = Equipment(
        id=uuid4(), name="Breadboard", category="Prototyping",
        tracking_type="QUANTITY", default_location="Bin B"
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq_ind, a1, eq_qty])
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq_qty.id, StockAdjustment(expected_quantity=0, change=40), c.staff)

    with Session(c.engine) as db:
        cats = get_category_report(db)
        cat_map = {cat.category: cat for cat in cats.categories}
        assert "Instruments" in cat_map
        assert "Prototyping" in cat_map

        inst = cat_map["Instruments"]
        assert inst.catalog_items == 1
        assert inst.individual_catalog_items == 1
        assert inst.quantity_catalog_items == 0
        assert inst.total_physical_assets == 1
        assert inst.available_assets == 1
        assert inst.quantity_on_hand == 0
        assert inst.quantity_on_loan == 0

        proto = cat_map["Prototyping"]
        assert proto.catalog_items == 1
        assert proto.individual_catalog_items == 0
        assert proto.quantity_catalog_items == 1
        assert proto.total_physical_assets == 0
        assert proto.quantity_on_hand == 40
        assert proto.quantity_on_loan == 0


def test_scenario_5_availability_and_condition_counts_reconcile(report_env):
    """Scenario 5: Availability sum and condition sum both equal total_physical_assets."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Router Testbed", category="Networking",
        tracking_type="INDIVIDUAL", default_location="Rack 1"
    )
    assets = [
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-1", location="Rack 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False),
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-2", location="Rack 1", availability_status="BORROWED", condition_status="WORKING", maintenance_hold=False),
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-3", location="Rack 1", availability_status="RESERVED", condition_status="WORKING", maintenance_hold=False),
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-4", location="Rack 1", availability_status="MAINTENANCE", condition_status="DAMAGED", maintenance_hold=True),
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-5", location="Rack 1", availability_status="RETIRED", condition_status="UNDER_REPAIR", maintenance_hold=False),
    ]
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all(assets)
        db.commit()

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        a = overview.assets
        cond = overview.conditions

        assert a.total_physical_assets == 5
        avail_sum = a.available + a.borrowed + a.reserved + a.maintenance + a.retired
        assert avail_sum == a.total_physical_assets

        cond_sum = cond.working + cond.damaged + cond.under_repair + cond.missing
        assert cond_sum == a.total_physical_assets


def test_scenario_6_borrowed_equipment_reported_missing_flow(report_env):
    """Scenario 6: Borrowed asset reported missing preserves loan custody and alerts."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Field Multimeter", category="Instruments",
        tracking_type="INDIVIDUAL", default_location="Toolbox"
    )
    asset = EquipmentAsset(
        id=uuid4(), equipment_id=eq.id, asset_code="MM-001",
        location="Toolbox", availability_status="AVAILABLE", condition_status="WORKING",
        maintenance_hold=False
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, asset])
        db.commit()

    # Checkout asset to student
    with Session(c.engine) as db:
        a = db.get(EquipmentAsset, asset.id)
        a.availability_status = "BORROWED"
        checkout = EquipmentCheckout(
            id=uuid4(), asset_id=asset.id, student_id=c.student.id, issued_by=c.staff.id,
            checked_out_at=datetime.now(timezone.utc), due_at=datetime.now(timezone.utc) + timedelta(days=3),
            status="BORROWED"
        )
        db.add(checkout)
        db.commit()

    # Staff reports asset missing
    with Session(c.engine) as db:
        report_asset_missing(
            db, asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Student lost it on campus"),
            c.staff
        )

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.alerts.open_missing_incidents == 1
        assert overview.alerts.active_individual_checkouts == 1
        assert overview.alerts.assets_on_maintenance_hold == 1

        missing = get_missing_report(db)
        assert missing.total == 1
        assert missing.items[0].was_on_loan_when_reported is True
        assert missing.items[0].current_asset_availability == "BORROWED"
        assert missing.items[0].current_asset_condition == "MISSING"
        assert missing.items[0].current_maintenance_hold is True


def test_scenario_7_reserved_equipment_reported_missing(report_env):
    """Scenario 7: Reserved asset reported missing does not affect quantity stock."""
    c = report_env
    eq_ind = Equipment(
        id=uuid4(), name="Lab Logic Analyzer", category="Instruments",
        tracking_type="INDIVIDUAL", default_location="Shelf 1"
    )
    asset = EquipmentAsset(
        id=uuid4(), equipment_id=eq_ind.id, asset_code="LA-001",
        location="Shelf 1", availability_status="RESERVED", condition_status="WORKING",
        maintenance_hold=False
    )
    eq_qty = Equipment(
        id=uuid4(), name="Jumpers", category="Connectors",
        tracking_type="QUANTITY", default_location="Bin 1"
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq_ind, asset, eq_qty])
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq_qty.id, StockAdjustment(expected_quantity=0, change=50), c.staff)

    with Session(c.engine) as db:
        rep = report_asset_missing(
            db, asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing from reserve hold"),
            c.staff
        )
        assert rep.previous_availability_status == "RESERVED"

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.quantity_stock.on_hand_units == 50
        assert overview.quantity_stock.accounted_units == 50
        assert overview.alerts.open_missing_incidents == 1

        missing = get_missing_report(db)
        assert missing.items[0].was_on_loan_when_reported is False


def test_scenario_8_retired_equipment_isolation(report_env):
    """Scenario 8: Retired equipment is counted under RETIRED and not in available/borrowed/maintenance."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Legacy CRT Monitor", category="Displays",
        tracking_type="INDIVIDUAL", default_location="Storage"
    )
    asset = EquipmentAsset(
        id=uuid4(), equipment_id=eq.id, asset_code="CRT-001",
        location="Storage", availability_status="RETIRED", condition_status="WORKING",
        maintenance_hold=False
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, asset])
        db.commit()

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.assets.total_physical_assets == 1
        assert overview.assets.retired == 1
        assert overview.assets.available == 0
        assert overview.assets.borrowed == 0
        assert overview.assets.reserved == 0
        assert overview.assets.maintenance == 0


def test_scenario_9_quantity_stock_with_active_loans(report_env):
    """Scenario 9: Quantity stock accurately reflects on_hand, active_loaned, and accounted units."""
    c = report_env
    eq = Equipment(
        id=uuid4(), name="Soldering Iron Tips", category="Tools",
        tracking_type="QUANTITY", default_location="Tool Cabinet"
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq.id, StockAdjustment(expected_quantity=0, change=30), c.staff)

    # Issue loan of 10 to student
    loan_data = QuantityLoanCreate(
        idempotency_key=uuid4(),
        equipment_id=eq.id,
        student_id=c.student.id,
        quantity=10,
        due_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    with Session(c.engine) as db:
        loans.issue_quantity(db, loan_data, c.staff)

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        assert overview.quantity_stock.on_hand_units == 20
        assert overview.quantity_stock.active_loaned_units == 10
        assert overview.quantity_stock.accounted_units == 30
        assert overview.alerts.active_quantity_loans == 1

        stock = get_stock_report(db)
        assert stock.total == 1
        item = stock.items[0]
        assert item.on_hand_quantity == 20
        assert item.active_loaned_quantity == 10
        assert item.accounted_quantity == 30
        assert item.active_loan_count == 1


def test_scenario_10_stock_out_of_stock_filtering(report_env):
    """Scenario 10: out_of_stock_only correctly filters items where on_hand_units = 0."""
    c = report_env
    eq1 = Equipment(id=uuid4(), name="HDMI Cable", category="Cables", tracking_type="QUANTITY", default_location="Bin A")
    eq2 = Equipment(id=uuid4(), name="VGA Cable", category="Cables", tracking_type="QUANTITY", default_location="Bin B")
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq1, eq2])
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq1.id, StockAdjustment(expected_quantity=0, change=5), c.staff)
        adjust_stock(db, eq2.id, StockAdjustment(expected_quantity=0, change=15), c.staff)

    # Loan out all 5 of eq1
    loan_data = QuantityLoanCreate(
        idempotency_key=uuid4(),
        equipment_id=eq1.id,
        student_id=c.student.id,
        quantity=5,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    with Session(c.engine) as db:
        loans.issue_quantity(db, loan_data, c.staff)

    with Session(c.engine) as db:
        stock_all = get_stock_report(db, out_of_stock_only=False)
        assert stock_all.total == 2

        stock_oos = get_stock_report(db, out_of_stock_only=True)
        assert stock_oos.total == 1
        assert stock_oos.items[0].equipment_id == eq1.id
        assert stock_oos.items[0].on_hand_quantity == 0


def test_scenario_11_category_aggregation_without_cartesian_multiplication(report_env):
    """Scenario 11: Joining assets, stock, and loans by category does NOT produce Cartesian product inflation."""
    c = report_env
    eq_ind = Equipment(id=uuid4(), name="RF Analyzer", category="Lab Tools", tracking_type="INDIVIDUAL", default_location="Lab")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq_ind.id, asset_code="RF-01", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq_ind.id, asset_code="RF-02", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a3 = EquipmentAsset(id=uuid4(), equipment_id=eq_ind.id, asset_code="RF-03", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    eq_qty = Equipment(id=uuid4(), name="Probes", category="Lab Tools", tracking_type="QUANTITY", default_location="Lab")
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq_ind, a1, a2, a3, eq_qty])
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq_qty.id, StockAdjustment(expected_quantity=0, change=50), c.staff)

    # Issue 2 loans on the quantity equipment (5 and 10 units = 15 total)
    l1 = QuantityLoanCreate(idempotency_key=uuid4(), equipment_id=eq_qty.id, student_id=c.student.id, quantity=5, due_at=datetime.now(timezone.utc) + timedelta(days=1))
    l2 = QuantityLoanCreate(idempotency_key=uuid4(), equipment_id=eq_qty.id, student_id=c.student2.id, quantity=10, due_at=datetime.now(timezone.utc) + timedelta(days=1))
    with Session(c.engine) as db:
        loans.issue_quantity(db, l1, c.staff)
        loans.issue_quantity(db, l2, c.staff)

    with Session(c.engine) as db:
        cats = get_category_report(db, category_filter="Lab Tools")
        assert len(cats.categories) == 1
        cat = cats.categories[0]
        # Must be exactly 3 physical assets, NOT 3 * 2 loans = 6!
        assert cat.total_physical_assets == 3
        # Must be exactly 35 on hand (50 - 15), NOT inflated
        assert cat.quantity_on_hand == 35
        # Must be exactly 15 on loan (5 + 10), NOT inflated
        assert cat.quantity_on_loan == 15


def test_scenario_12_missing_incident_status_filtering(report_env):
    """Scenario 12: Missing incident filtering: OPEN returns only open, RECONCILED returns only reconciled."""
    c = report_env
    eq = Equipment(id=uuid4(), name="USB Analyzer", category="Tools", tracking_type="INDIVIDUAL", default_location="Bench")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="UA-1", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="UA-2", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, a1, a2])
        db.commit()

    with Session(c.engine) as db:
        report_asset_missing(db, a1.id, AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing 1"), c.staff)
        report_asset_missing(db, a2.id, AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing 2"), c.staff)
        reconcile_missing_asset(db, a2.id, AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Found in locker"), c.staff)

    with Session(c.engine) as db:
        open_page = get_missing_report(db, status_filter="OPEN")
        assert open_page.total == 1
        assert open_page.items[0].asset_id == a1.id
        assert open_page.items[0].incident_status == "OPEN"

        rec_page = get_missing_report(db, status_filter="RECONCILED")
        assert rec_page.total == 1
        assert rec_page.items[0].asset_id == a2.id
        assert rec_page.items[0].incident_status == "RECONCILED"

        all_page = get_missing_report(db, status_filter="ALL")
        assert all_page.total == 2


def test_scenario_13_missing_incident_duration_calculations(report_env):
    """Scenario 13: OPEN uses as_of, RECONCILED uses reconciled_at; durations are non-negative."""
    c = report_env
    eq = Equipment(id=uuid4(), name="Logic Probe", category="Tools", tracking_type="INDIVIDUAL", default_location="Bench")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="LP-1", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, a])
        db.commit()

    with Session(c.engine) as db:
        report_asset_missing(db, a.id, AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing probe"), c.staff)

    with Session(c.engine) as db:
        page = get_missing_report(db, status_filter="OPEN")
        assert page.total == 1
        assert page.items[0].days_missing >= 0

    with Session(c.engine) as db:
        reconcile_missing_asset(db, a.id, AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Recovered"), c.staff)

    with Session(c.engine) as db:
        page = get_missing_report(db, status_filter="RECONCILED")
        assert page.total == 1
        assert page.items[0].days_missing >= 0


def test_scenario_14_maintenance_request_status_filtering(report_env):
    """Scenario 14: ACTIVE returns OPEN, IN_PROGRESS, AWAITING_VERIFICATION; specific filters work."""
    c = report_env
    eq = Equipment(id=uuid4(), name="Bench DMM", category="Instruments", tracking_type="INDIVIDUAL", default_location="Bench")
    assets = [
        EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code=f"DMM-{i}", location="Bench", availability_status="MAINTENANCE", condition_status="DAMAGED", maintenance_hold=True)
        for i in range(4)
    ]
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all(assets)
        db.commit()

    now = datetime.now(timezone.utc)
    staff2 = User(
        id=uuid4(), college_id="FACULTY_REP2", full_name="Report Faculty 2",
        email="faculty2.rep@college.invalid", role="FACULTY", is_active=True
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(staff2)
        db.commit()

    m_open = MaintenanceRequest(
        id=uuid4(), asset_id=assets[0].id, reported_by=c.staff.id, issue_type="DAMAGE",
        issue_description="Blown fuse", status="OPEN", reported_at=now - timedelta(days=2), version=1
    )
    e_open = MaintenanceEvent(
        id=uuid4(), request_id=m_open.id, sequence=1, event_type="REPORTED",
        actor_id=c.staff.id, occurred_at=now - timedelta(days=2), to_status="OPEN", note="Blown fuse reported"
    )

    m_prog = MaintenanceRequest(
        id=uuid4(), asset_id=assets[1].id, reported_by=c.staff.id, issue_type="DAMAGE",
        issue_description="Calibration", status="IN_PROGRESS", reported_at=now - timedelta(days=2),
        started_at=now - timedelta(days=1), assigned_to=c.staff.id, version=1
    )
    e_prog = MaintenanceEvent(
        id=uuid4(), request_id=m_prog.id, sequence=1, event_type="STARTED",
        actor_id=c.staff.id, occurred_at=now - timedelta(days=1), to_status="IN_PROGRESS", note="Calibration started"
    )

    m_await = MaintenanceRequest(
        id=uuid4(), asset_id=assets[2].id, reported_by=c.staff.id, issue_type="DAMAGE",
        issue_description="Screen repair", status="AWAITING_VERIFICATION", reported_at=now - timedelta(days=3),
        started_at=now - timedelta(days=2), assigned_to=c.staff.id,
        repair_completed_at=now - timedelta(days=1), repair_completed_by=c.staff.id, version=1
    )
    e_await = MaintenanceEvent(
        id=uuid4(), request_id=m_await.id, sequence=1, event_type="REPAIR_COMPLETED",
        actor_id=c.staff.id, occurred_at=now - timedelta(days=1), to_status="AWAITING_VERIFICATION", note="Screen repaired"
    )

    m_done = MaintenanceRequest(
        id=uuid4(), asset_id=assets[3].id, reported_by=c.staff.id, issue_type="DAMAGE",
        issue_description="New leads", status="COMPLETED", reported_at=now - timedelta(days=4),
        started_at=now - timedelta(days=3), assigned_to=c.staff.id,
        repair_completed_at=now - timedelta(days=2), repair_completed_by=c.staff.id,
        verified_at=now - timedelta(days=1), verified_by=staff2.id,
        returned_to_service_at=now - timedelta(hours=12), version=1
    )
    e_done = MaintenanceEvent(
        id=uuid4(), request_id=m_done.id, sequence=1, event_type="VERIFIED",
        actor_id=staff2.id, occurred_at=now - timedelta(days=1), to_status="COMPLETED", note="Verification complete"
    )

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([m_open, m_prog, m_await, m_done])
        db.flush()
        db.add_all([e_open, e_prog, e_await, e_done])
        db.commit()

    with Session(c.engine) as db:
        active_page = get_maintenance_report(db, status_filter="ACTIVE")
        assert active_page.total == 3
        statuses = {item.status for item in active_page.items}
        assert statuses == {"OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION"}

        completed_page = get_maintenance_report(db, status_filter="COMPLETED")
        assert completed_page.total == 1
        assert completed_page.items[0].status == "COMPLETED"

        all_page = get_maintenance_report(db, status_filter="ALL")
        assert all_page.total == 4


def test_scenario_15_pagination_with_populated_pages(report_env):
    """Scenario 15: Pagination restricts returned rows, has_more set accurately."""
    c = report_env
    eqs = [
        Equipment(id=uuid4(), name=f"Bulk Resistor Pack {i:02d}", category="Passives", tracking_type="QUANTITY", default_location="Bins")
        for i in range(5)
    ]
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all(eqs)
        db.commit()

    for eq in eqs:
        with Session(c.engine) as db:
            adjust_stock(db, eq.id, StockAdjustment(expected_quantity=0, change=10), c.staff)

    with Session(c.engine) as db:
        p1 = get_stock_report(db, limit=2, offset=0)
        assert len(p1.items) == 2
        assert p1.total == 5
        assert p1.has_more is True

        p2 = get_stock_report(db, limit=2, offset=2)
        assert len(p2.items) == 2
        assert p2.total == 5
        assert p2.has_more is True

        p3 = get_stock_report(db, limit=2, offset=4)
        assert len(p3.items) == 1
        assert p3.total == 5
        assert p3.has_more is False


def test_scenario_16_pagination_beyond_the_last_page(report_env):
    """Scenario 16: offset > total returns empty items list, has_more=False, total preserved."""
    c = report_env
    eq = Equipment(id=uuid4(), name="Capacitor Pack", category="Passives", tracking_type="QUANTITY", default_location="Bins")
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.commit()

    with Session(c.engine) as db:
        adjust_stock(db, eq.id, StockAdjustment(expected_quantity=0, change=10), c.staff)

    with Session(c.engine) as db:
        page = get_stock_report(db, limit=10, offset=50)
        assert page.total == 1
        assert page.items == []
        assert page.has_more is False
        assert page.offset == 50


def test_scenario_17_nullable_fields_and_response_serialization(report_env):
    """Scenario 17: Nullable fields handle None correctly and serialize without error."""
    c = report_env
    eq = Equipment(id=uuid4(), name="Power Supply", category="Instruments", tracking_type="INDIVIDUAL", default_location="Bench")
    asset1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="PSU-001", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    asset2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="PSU-002", location="Bench", availability_status="MAINTENANCE", condition_status="DAMAGED", maintenance_hold=True)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([asset1, asset2])
        db.commit()

    # Missing incident with NULL police report number
    with Session(c.engine) as db:
        report_asset_missing(
            db, asset1.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing PSU", police_report_number=None),
            c.staff
        )

    # Maintenance request with NULL assigned_to
    m = MaintenanceRequest(
        id=uuid4(), asset_id=asset2.id, reported_by=c.staff.id, issue_type="MALFUNCTION",
        issue_description="Voltage fluctuation", status="OPEN", assigned_to=None, version=1
    )
    e = MaintenanceEvent(
        id=uuid4(), request_id=m.id, sequence=1, event_type="REPORTED",
        actor_id=c.staff.id, to_status="OPEN", note="Initial report"
    )
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(m)
        db.flush()
        db.add(e)
        db.commit()

    with Session(c.engine) as db:
        overview = get_inventory_overview(db)
        cats = get_category_report(db)
        stock = get_stock_report(db)
        missing = get_missing_report(db)
        maint = get_maintenance_report(db)

        # JSON serialization validation
        ov_json = overview.model_dump_json()
        assert '"total_catalog_items"' in ov_json

        cats_json = cats.model_dump_json()
        assert '"categories"' in cats_json

        stock_json = stock.model_dump_json()
        assert '"items"' in stock_json

        missing_json = missing.model_dump_json()
        assert '"police_report_number":null' in missing_json

        maint_json = maint.model_dump_json()
        assert '"assigned_to_id":null' in maint_json


def test_scenario_18_student_access_denied(report_env):
    """Scenario 18: Student credentials are explicitly rejected with 403 Forbidden."""
    c = report_env
    with pytest.raises(HTTPException) as exc:
        _verify_staff_access(c.student)
    assert exc.value.status_code == 403


def test_scenario_19_unauthenticated_access_denied(report_env):
    """Scenario 19: Unauthenticated caller is rejected with 401."""
    with pytest.raises(HTTPException) as exc:
        _verify_staff_access(None)
    assert exc.value.status_code == 401


def test_scenario_20_concurrent_inventory_changes_while_reporting(report_env):
    """Scenario 20: Concurrent inventory updates and report executions do not block or deadlock."""
    c = report_env
    eq = Equipment(id=uuid4(), name="RF Mixer", category="RF", tracking_type="INDIVIDUAL", default_location="Bench")
    asset = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RFM-001", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, asset])
        db.commit()

    def run_reporting():
        for _ in range(5):
            with Session(c.engine) as db:
                ov = get_inventory_overview(db)
                assert ov.total_catalog_items >= 1
                stk = get_stock_report(db)
                assert stk.total >= 0

    def run_updates():
        for _ in range(5):
            with Session(c.engine) as db:
                a = db.get(EquipmentAsset, asset.id)
                a.availability_status = "AVAILABLE" if a.availability_status != "AVAILABLE" else "RESERVED"
                db.commit()

    with ThreadPoolExecutor(max_workers=4) as executor:
        f1 = executor.submit(run_reporting)
        f2 = executor.submit(run_reporting)
        f3 = executor.submit(run_updates)
        f4 = executor.submit(run_updates)
        # All must complete without exception
        f1.result()
        f2.result()
        f3.result()
        f4.result()


def test_scenario_21_strictly_read_only_execution(report_env):
    """Every reporting request executes ONLY SELECT queries; zero INSERT/UPDATE/DELETE/ALTER/DROP/TRUNCATE."""
    c = report_env
    eq = Equipment(id=uuid4(), name="Signal Tracer", category="Instruments", tracking_type="INDIVIDUAL", default_location="Lab")
    asset = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-001", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, asset])
        db.commit()

    executed_statements = []

    def before_cursor_execute_listener(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement.strip())

    event.listen(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    try:
        with Session(c.engine) as db:
            get_inventory_overview(db)
            get_category_report(db)
            get_stock_report(db)
            get_missing_report(db)
            get_maintenance_report(db)
    finally:
        event.remove(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    assert len(executed_statements) > 0

    forbidden_keywords = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE")
    for stmt in executed_statements:
        normalized = stmt.upper().lstrip()
        first_word = normalized.split()[0] if normalized.split() else ""
        assert first_word in ("SELECT", "WITH", "SHOW", "SET"), (
            f"Non-read-only statement detected: {stmt}"
        )
        for forbidden in forbidden_keywords:
            assert not normalized.startswith(forbidden), (
                f"Forbidden keyword '{forbidden}' at start of statement: {stmt}"
            )
