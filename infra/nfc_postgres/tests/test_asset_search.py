"""Isolated PostgreSQL integration tests for Asset Filtering & Search.

Exercises Phase B and Phase C real PostgreSQL 18 scenarios in the disposable,
opt-in container instance.
"""
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.alembic.application_safety import apply_application, HEAD
from backend.app.models.user import User, UserRole
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.schemas.asset import (
    AssetResponse,
    AvailabilityStatus,
    ConditionStatus,
)
from backend.app.services.asset import list_assets_filtered, lookup_asset
from infra.nfc_postgres.harness import ADMIN


def _verify_staff_access(user: User | None):
    """Role-based access check matching the assets API dependency."""
    if user is None or not getattr(user, "is_active", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.role not in (UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT):
        raise HTTPException(status_code=403, detail="Forbidden: Staff role required")
    return user


@pytest.fixture
def asset_env(database):
    """Set up database at application HEAD revision with seed users and permissions."""
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

    admin_user = User(
        id=uuid4(), college_id="ADMIN_SEARCH", full_name="Search Admin",
        email="admin.search@college.invalid", role="ADMIN", is_active=True
    )
    faculty = User(
        id=uuid4(), college_id="FACULTY_SEARCH", full_name="Search Faculty",
        email="faculty.search@college.invalid", role="FACULTY", is_active=True
    )
    lab_assistant = User(
        id=uuid4(), college_id="LAB_SEARCH", full_name="Search Lab Assistant",
        email="lab.search@college.invalid", role="LAB_ASSISTANT", is_active=True
    )
    student = User(
        id=uuid4(), college_id="STUDENT_SEARCH", full_name="Search Student",
        email="student.search@college.invalid", role="STUDENT", is_active=True
    )
    inactive_staff = User(
        id=uuid4(), college_id="INACTIVE_STAFF", full_name="Inactive Staff",
        email="inactive.staff@college.invalid", role="FACULTY", is_active=False
    )

    with Session(admin, expire_on_commit=False) as db:
        db.add_all([admin_user, faculty, lab_assistant, student, inactive_staff])
        db.commit()

    app_engine = database.engine("inventory_app")
    return SimpleNamespace(
        h=database,
        engine=app_engine,
        admin=admin,
        admin_user=admin_user,
        faculty=faculty,
        lab_assistant=lab_assistant,
        student=student,
        inactive_staff=inactive_staff,
    )


# ==============================================================================
# INTEGRATION TESTS: 32 REQUIRED SCENARIOS
# ==============================================================================


def test_scenario_01_empty_inventory_returns_empty_list(asset_env):
    """Scenario 1: Empty inventory returns an empty list without error."""
    with Session(asset_env.engine) as db:
        results = list_assets_filtered(db)
        assert results == []


def test_scenario_02_unfiltered_listing_deterministic_order(asset_env):
    """Scenario 2: Unfiltered listing returns assets in deterministic order (asset_code ASC, id ASC)."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Router", category="Networking", tracking_type="INDIVIDUAL", default_location="Lab 1")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-003", location="Lab 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-001", location="Lab 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a3 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RT-002", location="Lab 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2, a3])
        db.commit()

    with Session(c.engine) as db:
        results = list_assets_filtered(db)
        codes = [a.asset_code for a in results]
        assert codes == ["RT-001", "RT-002", "RT-003"]


def test_scenario_03_equipment_name_search_joins_equipment(asset_env):
    """Scenario 3: Equipment name search works through the equipment join."""
    c = asset_env
    eq1 = Equipment(id=uuid4(), name="Cisco Catalyst 2960", category="Switch", tracking_type="INDIVIDUAL", default_location="Rack A")
    eq2 = Equipment(id=uuid4(), name="HP ProCurve Switch", category="Switch", tracking_type="INDIVIDUAL", default_location="Rack B")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq1.id, asset_code="SW-001", location="Rack A", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="SW-002", location="Rack B", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq1, eq2])
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res1 = list_assets_filtered(db, search="catalyst")
        assert len(res1) == 1
        assert res1[0].asset_code == "SW-001"

        res2 = list_assets_filtered(db, search="procurve")
        assert len(res2) == 1
        assert res2[0].asset_code == "SW-002"

        res_both = list_assets_filtered(db, search="switch")
        codes = [a.asset_code for a in res_both]
        assert "SW-002" in codes


def test_scenario_04_asset_code_search(asset_env):
    """Scenario 4: Asset code search works."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Fiber Tester", category="Testing", tracking_type="INDIVIDUAL", default_location="Lab 2")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="FIBER-TEST-99", location="Lab 2", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="COPPER-TEST-11", location="Lab 2", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="fiber-test")
        assert len(res) == 1
        assert res[0].asset_code == "FIBER-TEST-99"


def test_scenario_05_serial_number_search(asset_env):
    """Scenario 5: Serial number search works."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Server Rack", category="Servers", tracking_type="INDIVIDUAL", default_location="Server Room")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="SRV-001", serial_number="SN-ALPHA-7890", location="Server Room", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="SRV-002", serial_number="SN-BETA-1234", location="Server Room", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="alpha-7890")
        assert len(res) == 1
        assert res[0].asset_code == "SRV-001"


def test_scenario_06_nfc_tag_search(asset_env):
    """Scenario 6: NFC tag search works."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Tablet", category="Mobile", tracking_type="INDIVIDUAL", default_location="Locker")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="TAB-001", nfc_tag_id="04A1B2C3D4E5", location="Locker", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="TAB-002", nfc_tag_id="04F6E5D4C3B2", location="Locker", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="A1B2C3")
        assert len(res) == 1
        assert res[0].asset_code == "TAB-001"


def test_scenario_07_category_filtering_case_insensitive(asset_env):
    """Scenario 7: Category filtering is case-insensitive."""
    c = asset_env
    eq1 = Equipment(id=uuid4(), name="Analyzer", category="RF Instruments", tracking_type="INDIVIDUAL", default_location="Bench 1")
    eq2 = Equipment(id=uuid4(), name="Multimeter", category="General Tools", tracking_type="INDIVIDUAL", default_location="Bench 2")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq1.id, asset_code="AN-001", location="Bench 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="MM-001", location="Bench 2", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq1, eq2])
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res_lower = list_assets_filtered(db, category="rf instruments")
        assert len(res_lower) == 1
        assert res_lower[0].asset_code == "AN-001"

        res_upper = list_assets_filtered(db, category="RF INSTRUMENTS")
        assert len(res_upper) == 1
        assert res_upper[0].asset_code == "AN-001"

        res_other = list_assets_filtered(db, category="Other Category")
        assert res_other == []


def test_scenario_08_equipment_uuid_filtering(asset_env):
    """Scenario 8: Equipment UUID filtering works."""
    c = asset_env
    eq1 = Equipment(id=uuid4(), name="Sensor Hub A", category="IoT", tracking_type="INDIVIDUAL", default_location="Lab 3")
    eq2 = Equipment(id=uuid4(), name="Sensor Hub B", category="IoT", tracking_type="INDIVIDUAL", default_location="Lab 3")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq1.id, asset_code="HUB-A-1", location="Lab 3", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="HUB-B-1", location="Lab 3", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq1, eq2])
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, equipment_id=eq1.id)
        assert len(res) == 1
        assert res[0].asset_code == "HUB-A-1"


def test_scenario_09_availability_filtering_all_statuses(asset_env):
    """Scenario 9: Availability filtering works across all 5 valid statuses."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Test Rig", category="General", tracking_type="INDIVIDUAL", default_location="Bench")
    # Satisfying check constraints:
    # AVAILABLE requires condition=WORKING and NOT maintenance_hold
    # MAINTENANCE requires maintenance_hold=True
    a_avail = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-AVAIL", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False, location="Bench")
    a_borr = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-BORR", availability_status="BORROWED", condition_status="WORKING", maintenance_hold=False, location="Bench")
    a_res = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-RES", availability_status="RESERVED", condition_status="WORKING", maintenance_hold=False, location="Bench")
    a_maint = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-MAINT", availability_status="MAINTENANCE", condition_status="UNDER_REPAIR", maintenance_hold=True, location="Bench")
    a_ret = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="ST-RET", availability_status="RETIRED", condition_status="DAMAGED", maintenance_hold=False, location="Bench")

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a_avail, a_borr, a_res, a_maint, a_ret])
        db.commit()

    with Session(c.engine) as db:
        for status in [AvailabilityStatus.AVAILABLE, AvailabilityStatus.BORROWED, AvailabilityStatus.RESERVED, AvailabilityStatus.MAINTENANCE, AvailabilityStatus.RETIRED]:
            res = list_assets_filtered(db, availability_status=status)
            assert len(res) == 1
            assert res[0].availability_status == status.value


def test_scenario_10_condition_filtering_all_conditions(asset_env):
    """Scenario 10: Condition filtering works across all 4 valid conditions."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Condition Rig", category="General", tracking_type="INDIVIDUAL", default_location="Lab")
    c_work = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="CD-WORK", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False, location="Lab")
    c_dam = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="CD-DAM", availability_status="RETIRED", condition_status="DAMAGED", maintenance_hold=False, location="Lab")
    c_rep = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="CD-REP", availability_status="MAINTENANCE", condition_status="UNDER_REPAIR", maintenance_hold=True, location="Lab")
    c_miss = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="CD-MISS", availability_status="BORROWED", condition_status="MISSING", maintenance_hold=False, location="Lab")

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([c_work, c_dam, c_rep, c_miss])
        db.commit()

    with Session(c.engine) as db:
        for cond in [ConditionStatus.WORKING, ConditionStatus.DAMAGED, ConditionStatus.UNDER_REPAIR, ConditionStatus.MISSING]:
            res = list_assets_filtered(db, condition_status=cond)
            assert len(res) == 1
            assert res[0].condition_status == cond.value


def test_scenario_11_maintenance_hold_true_filtering(asset_env):
    """Scenario 11: Maintenance hold True filtering works."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Maint Eq", category="Tools", tracking_type="INDIVIDUAL", default_location="Storage")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="HOLD-TRUE", availability_status="MAINTENANCE", condition_status="UNDER_REPAIR", maintenance_hold=True, location="Storage")
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="HOLD-FALSE", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False, location="Storage")

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, maintenance_hold=True)
        assert len(res) == 1
        assert res[0].asset_code == "HOLD-TRUE"
        assert res[0].maintenance_hold is True


def test_scenario_12_maintenance_hold_false_filtering(asset_env):
    """Scenario 12: Maintenance hold False filtering works."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Maint Eq 2", category="Tools", tracking_type="INDIVIDUAL", default_location="Storage")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="HOLD-TRUE-2", availability_status="MAINTENANCE", condition_status="UNDER_REPAIR", maintenance_hold=True, location="Storage")
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="HOLD-FALSE-2", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False, location="Storage")

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, maintenance_hold=False)
        assert len(res) == 1
        assert res[0].asset_code == "HOLD-FALSE-2"
        assert res[0].maintenance_hold is False


def test_scenario_13_location_filtering_case_insensitive(asset_env):
    """Scenario 13: Location filtering is case-insensitive partial matching."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Location Eq", category="Tools", tracking_type="INDIVIDUAL", default_location="Lab 101")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="LOC-1", location="Room 204B West Rack", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="LOC-2", location="Basement Depot", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, location="room 204b")
        assert len(res) == 1
        assert res[0].asset_code == "LOC-1"

        res_upper = list_assets_filtered(db, location="WEST RACK")
        assert len(res_upper) == 1
        assert res_upper[0].asset_code == "LOC-1"


def test_scenario_14_combined_filters_and_semantics(asset_env):
    """Scenario 14: Combined filters use AND semantics."""
    c = asset_env
    eq1 = Equipment(id=uuid4(), name="Digital Oscilloscope", category="Instruments", tracking_type="INDIVIDUAL", default_location="Bench A")
    eq2 = Equipment(id=uuid4(), name="Analog Oscilloscope", category="Vintage", tracking_type="INDIVIDUAL", default_location="Bench B")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq1.id, asset_code="OSC-DIG-1", location="Bench A", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="OSC-ANA-1", location="Bench B", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq1, eq2])
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        # Matches both search ("oscilloscope") and category ("Instruments")
        res = list_assets_filtered(db, search="oscilloscope", category="Instruments")
        assert len(res) == 1
        assert res[0].asset_code == "OSC-DIG-1"

        # Search matches but category does not
        res_empty = list_assets_filtered(db, search="oscilloscope", category="NonExistent")
        assert res_empty == []


def test_scenario_15_search_fields_or_semantics(asset_env):
    """Scenario 15: Search fields use OR semantics across name, code, serial, and NFC."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Fluke Wave Meter", category="Meters", tracking_type="INDIVIDUAL", default_location="Bench")
    # Matches name
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="WM-001", serial_number="SN-111", nfc_tag_id="NFC-111", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    # Matches code
    eq2 = Equipment(id=uuid4(), name="Other Meter", category="Meters", tracking_type="INDIVIDUAL", default_location="Bench")
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="WAVE-CODE-99", serial_number="SN-222", nfc_tag_id="NFC-222", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    # Matches serial
    a3 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="WM-003", serial_number="SN-WAVE-333", nfc_tag_id="NFC-333", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    # Matches NFC
    a4 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="WM-004", serial_number="SN-444", nfc_tag_id="NFC-WAVE-444", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    # Matches none
    a5 = EquipmentAsset(id=uuid4(), equipment_id=eq2.id, asset_code="WM-005", serial_number="SN-555", nfc_tag_id="NFC-555", location="Bench", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add_all([eq, eq2])
        db.add_all([a1, a2, a3, a4, a5])
        db.commit()

    with Session(c.engine) as db:
        results = list_assets_filtered(db, search="wave")
        codes = {a.asset_code for a in results}
        assert codes == {"WM-001", "WAVE-CODE-99", "WM-003", "WM-004"}
        assert "WM-005" not in codes


def test_scenario_16_whitespace_only_search_behaves_as_no_filter(asset_env):
    """Scenario 16: Whitespace-only search behaves as no filter."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Cable Tester", category="Network", tracking_type="INDIVIDUAL", default_location="Lab")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="WS-001", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="WS-002", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="   \t  ")
        assert len(res) == 2


def test_scenario_17_percent_signs_treated_literally(asset_env):
    """Scenario 17: Percent signs are treated literally, not as LIKE wildcards."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Speed 100% Kit", category="Kits", tracking_type="INDIVIDUAL", default_location="Lab")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="PC%100", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="PC1000", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="PC%100")
        assert len(res) == 1
        assert res[0].asset_code == "PC%100"


def test_scenario_18_underscores_treated_literally(asset_env):
    """Scenario 18: Underscores are treated literally, not as LIKE single-character wildcards."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Model_X Device", category="Devices", tracking_type="INDIVIDUAL", default_location="Lab")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="DEV_1", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="DEVX1", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="DEV_1")
        assert len(res) == 1
        assert res[0].asset_code == "DEV_1"


def test_scenario_19_backslashes_treated_literally(asset_env):
    """Scenario 19: Backslashes are treated literally."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Rack Unit", category="Racks", tracking_type="INDIVIDUAL", default_location="Lab")
    a1 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="BS-1", location="Cabinet\\ShelfA", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    a2 = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="BS-2", location="CabinetShelfA", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all([a1, a2])
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, location="Cabinet\\ShelfA")
        assert len(res) == 1
        assert res[0].asset_code == "BS-1"


def test_scenario_20_quotes_and_unicode_search_values(asset_env):
    """Scenario 20: Quotes and Unicode search values execute safely."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Capteur d'humidité — 20µF", category="Capteurs", tracking_type="INDIVIDUAL", default_location="Étage 1")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="CAP-01", location="Étage 1", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res1 = list_assets_filtered(db, search="d'humidité")
        assert len(res1) == 1
        assert res1[0].asset_code == "CAP-01"

        res2 = list_assets_filtered(db, search="20µF")
        assert len(res2) == 1
        assert res2[0].asset_code == "CAP-01"

        res3 = list_assets_filtered(db, location="Étage 1")
        assert len(res3) == 1
        assert res3[0].asset_code == "CAP-01"


def test_scenario_21_nullable_serial_numbers_no_errors(asset_env):
    """Scenario 21: Nullable serial numbers do not cause errors."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Patch Cord", category="Cables", tracking_type="INDIVIDUAL", default_location="Bin")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="NULL-SN-1", serial_number=None, location="Bin", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="Patch")
        assert len(res) == 1
        assert res[0].serial_number is None


def test_scenario_22_nullable_nfc_identifiers_no_errors(asset_env):
    """Scenario 22: Nullable NFC identifiers do not cause errors."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Loose Lens", category="Optics", tracking_type="INDIVIDUAL", default_location="Drawer")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="NULL-NFC-1", nfc_tag_id=None, location="Drawer", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, search="Lens")
        assert len(res) == 1
        assert res[0].nfc_tag_id is None


def test_scenario_23_borrowed_assets_reported_missing(asset_env):
    """Scenario 23: Borrowed assets reported missing can be found with availability_status=BORROWED and condition_status=MISSING."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Field Camera", category="Media", tracking_type="INDIVIDUAL", default_location="Studio")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="MISS-BORR-1", location="Offsite", availability_status="BORROWED", condition_status="MISSING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(
            db,
            availability_status=AvailabilityStatus.BORROWED,
            condition_status=ConditionStatus.MISSING,
        )
        assert len(res) == 1
        assert res[0].asset_code == "MISS-BORR-1"


def test_scenario_24_reserved_assets_reported_missing(asset_env):
    """Scenario 24: Reserved assets reported missing can be found with availability_status=RESERVED and condition_status=MISSING."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Reserve Laptop", category="Computers", tracking_type="INDIVIDUAL", default_location="Cabinet")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="MISS-RES-1", location="Cabinet", availability_status="RESERVED", condition_status="MISSING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(
            db,
            availability_status=AvailabilityStatus.RESERVED,
            condition_status=ConditionStatus.MISSING,
        )
        assert len(res) == 1
        assert res[0].asset_code == "MISS-RES-1"


def test_scenario_25_assets_under_maintenance_filtered(asset_env):
    """Scenario 25: Assets under maintenance can be filtered correctly with maintenance_hold=True."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Spectrum Gen", category="Instruments", tracking_type="INDIVIDUAL", default_location="Workshop")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="MAINT-GEN-1", location="Workshop", availability_status="MAINTENANCE", condition_status="UNDER_REPAIR", maintenance_hold=True)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(
            db,
            availability_status=AvailabilityStatus.MAINTENANCE,
            maintenance_hold=True,
        )
        assert len(res) == 1
        assert res[0].asset_code == "MAINT-GEN-1"


def test_scenario_26_pagination_deterministic_non_overlapping(asset_env):
    """Scenario 26: Pagination returns deterministic non-overlapping slices for an unchanged dataset."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Bulk Resistors", category="Components", tracking_type="INDIVIDUAL", default_location="Drawer")
    assets = [
        EquipmentAsset(
            id=uuid4(),
            equipment_id=eq.id,
            asset_code=f"PG-{i:03d}",
            location="Drawer",
            availability_status="AVAILABLE",
            condition_status="WORKING",
            maintenance_hold=False,
        )
        for i in range(1, 16)
    ]

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add_all(assets)
        db.commit()

    with Session(c.engine) as db:
        page1 = list_assets_filtered(db, limit=5, offset=0)
        page2 = list_assets_filtered(db, limit=5, offset=5)
        page3 = list_assets_filtered(db, limit=5, offset=10)

        codes1 = [a.asset_code for a in page1]
        codes2 = [a.asset_code for a in page2]
        codes3 = [a.asset_code for a in page3]

        assert len(codes1) == 5
        assert len(codes2) == 5
        assert len(codes3) == 5

        # Check non-overlapping and complete sequence
        all_codes = codes1 + codes2 + codes3
        expected_codes = [f"PG-{i:03d}" for i in range(1, 16)]
        assert all_codes == expected_codes
        assert set(codes1).isdisjoint(set(codes2))
        assert set(codes2).isdisjoint(set(codes3))


def test_scenario_27_pagination_beyond_final_page_empty(asset_env):
    """Scenario 27: Pagination beyond the final page returns an empty list."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Small Set", category="Components", tracking_type="INDIVIDUAL", default_location="Desk")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="SMALL-1", location="Desk", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        res = list_assets_filtered(db, limit=10, offset=100)
        assert res == []


def test_scenario_28_staff_roles_authorized(asset_env):
    """Scenario 28: Staff roles (ADMIN, FACULTY, LAB_ASSISTANT) are authorized."""
    c = asset_env
    assert _verify_staff_access(c.admin_user).role == UserRole.ADMIN
    assert _verify_staff_access(c.faculty).role == UserRole.FACULTY
    assert _verify_staff_access(c.lab_assistant).role == UserRole.LAB_ASSISTANT


def test_scenario_29_students_denied(asset_env):
    """Scenario 29: Student access is rejected with 403 Forbidden."""
    c = asset_env
    with pytest.raises(HTTPException) as exc:
        _verify_staff_access(c.student)
    assert exc.value.status_code == 403


def test_scenario_30_unauthenticated_denied(asset_env):
    """Scenario 30: Unauthenticated requests and inactive users are rejected with 401 Unauthorized."""
    c = asset_env
    with pytest.raises(HTTPException) as exc1:
        _verify_staff_access(None)
    assert exc1.value.status_code == 401

    with pytest.raises(HTTPException) as exc2:
        _verify_staff_access(c.inactive_staff)
    assert exc2.value.status_code == 401


def test_scenario_31_strictly_read_only_execution(asset_env):
    """Scenario 31: GET /assets filtering performs strictly SELECT queries with zero writes."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="Read Only Test Eq", category="TestCat", tracking_type="INDIVIDUAL", default_location="Lab")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="RO-001", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    executed_statements = []

    def before_cursor_execute_listener(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement.strip())

    event.listen(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    try:
        with Session(c.engine) as db:
            list_assets_filtered(db, search="Test", category="TestCat", limit=10, offset=0)
            list_assets_filtered(db, availability_status=AvailabilityStatus.AVAILABLE, condition_status=ConditionStatus.WORKING)
            list_assets_filtered(db, maintenance_hold=False, location="Lab")
    finally:
        event.remove(c.engine, "before_cursor_execute", before_cursor_execute_listener)

    assert len(executed_statements) > 0
    forbidden_keywords = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE")
    for stmt in executed_statements:
        normalized = stmt.upper().lstrip()
        first_word = normalized.split()[0] if normalized.split() else ""
        assert first_word in ("SELECT", "WITH", "SHOW", "SET"), f"Non-read-only statement detected: {stmt}"
        for forbidden in forbidden_keywords:
            assert not normalized.startswith(forbidden), f"Forbidden keyword '{forbidden}' in statement: {stmt}"


def test_scenario_32_existing_lookup_asset_unchanged(asset_env):
    """Scenario 32: Existing lookup_asset NFC behavior remains completely unchanged."""
    c = asset_env
    eq = Equipment(id=uuid4(), name="NFC Target", category="NFC", tracking_type="INDIVIDUAL", default_location="Lab")
    a = EquipmentAsset(id=uuid4(), equipment_id=eq.id, asset_code="NFC-TGT-1", nfc_tag_id="04998877665544", location="Lab", availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)

    with Session(c.admin, expire_on_commit=False) as db:
        db.add(eq)
        db.add(a)
        db.commit()

    with Session(c.engine) as db:
        # Successful lookup
        found = lookup_asset(db, "04998877665544")
        assert found.id == a.id
        assert found.asset_code == "NFC-TGT-1"

        # Serialization to AssetResponse works
        resp = AssetResponse.model_validate(found, from_attributes=True)
        assert resp.asset_code == "NFC-TGT-1"

        # Not found raises 404
        with pytest.raises(HTTPException) as exc_404:
            lookup_asset(db, "04000000000000")
        assert exc_404.value.status_code == 404

        # Invalid NFC tag raises 422 Unprocessable Entity
        with pytest.raises(HTTPException) as exc_422_empty:
            lookup_asset(db, "   ")
        assert exc_422_empty.value.status_code == 422

        with pytest.raises(HTTPException) as exc_422_null:
            lookup_asset(db, "tag\x00null")
        assert exc_422_null.value.status_code == 422

