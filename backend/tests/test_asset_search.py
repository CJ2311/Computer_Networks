"""Database-free unit tests for asset filtering, search, and pagination."""

from contextlib import nullcontext
from unittest.mock import Mock
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.main import app
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User, UserRole
from backend.app.schemas.asset import (
    AssetResponse,
    AvailabilityStatus,
    ConditionStatus,
)
from backend.app.services.asset import escape_like, list_assets_filtered


@pytest.fixture
def mock_db():
    db = Mock()
    db.no_autoflush = nullcontext()
    db.scalars.return_value.all.return_value = []
    return db


def assert_read_only_db(db: Mock):
    for method in ("add", "delete", "flush", "commit", "rollback"):
        getattr(db, method).assert_not_called()


def compile_stmt(stmt):
    """Compile SQLAlchemy statement using PostgreSQL dialect for SQL text inspection."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


# ==============================================================================
# 1. Search Escaping & Normalization Unit Tests
# ==============================================================================


def test_search_escaping_wildcards():
    """Wildcards %, _, and backslash are properly escaped with backslash."""
    assert escape_like("100%") == "100\\%"
    assert escape_like("DMM_01") == "DMM\\_01"
    assert escape_like("path\\to\\asset") == "path\\\\to\\\\asset"
    assert escape_like("%_\\") == "\\%\\_\\\\"


def test_search_quotes_and_unicode_input():
    """Quotes, dashes, and Unicode characters are preserved as literal characters."""
    assert escape_like("Fluke '87V' -- test") == "Fluke '87V' -- test"
    assert escape_like('Resistor "10k"') == 'Resistor "10k"'
    assert escape_like("Sensor Ω-µA 🚀") == "Sensor Ω-µA 🚀"


# ==============================================================================
# 2. Service Layer Query Construction Tests
# ==============================================================================


def test_unfiltered_listing_retains_existing_behavior(mock_db):
    """Unfiltered listing selects from EquipmentAsset without join, ordered by asset_code, id."""
    res = list_assets_filtered(mock_db, limit=50, offset=10)

    assert_read_only_db(mock_db)
    assert res == []
    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)

    assert "FROM equipment_assets" in sql
    assert "JOIN equipment" not in sql
    assert stmt._limit_clause.value == 50
    assert stmt._offset_clause.value == 10
    # Deterministic ordering
    order_strs = [str(col) for col in stmt._order_by_clauses]
    assert any("asset_code" in s for s in order_strs)
    assert any("id" in s for s in order_strs)


def test_equipment_name_search(mock_db):
    """Search matches equipment name and joins Equipment."""
    list_assets_filtered(mock_db, search="Oscilloscope")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "JOIN equipment ON" in sql
    assert "equipment.name ILIKE" in sql


def test_asset_code_search(mock_db):
    """Search matches asset code."""
    list_assets_filtered(mock_db, search="OSC-001")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.asset_code ILIKE" in sql


def test_serial_number_search(mock_db):
    """Search matches serial number."""
    list_assets_filtered(mock_db, search="SN-98765")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.serial_number ILIKE" in sql


def test_nfc_identifier_search(mock_db):
    """Search matches NFC tag ID."""
    list_assets_filtered(mock_db, search="04A1B2C3")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.nfc_tag_id ILIKE" in sql


def test_case_insensitive_matching(mock_db):
    """Search is case-insensitive (ILIKE) across all four target fields."""
    list_assets_filtered(mock_db, search="fluke")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment.name ILIKE" in sql
    assert "equipment_assets.asset_code ILIKE" in sql
    assert "equipment_assets.serial_number ILIKE" in sql
    assert "equipment_assets.nfc_tag_id ILIKE" in sql


def test_category_filtering(mock_db):
    """Category filter joins equipment and applies lower(category) exact match."""
    list_assets_filtered(mock_db, category="Instruments")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "JOIN equipment ON" in sql
    assert "lower(equipment.category) = lower(" in sql or "lower(equipment.category) = " in sql


def test_equipment_uuid_filtering(mock_db):
    """Equipment ID filter applies exact match without requiring Equipment join."""
    target_id = uuid4()
    list_assets_filtered(mock_db, equipment_id=target_id)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.equipment_id = " in sql
    assert "JOIN equipment" not in sql


@pytest.mark.parametrize("status", [
    AvailabilityStatus.AVAILABLE,
    AvailabilityStatus.BORROWED,
    AvailabilityStatus.RESERVED,
    AvailabilityStatus.MAINTENANCE,
    AvailabilityStatus.RETIRED,
])
def test_all_availability_status_values(mock_db, status):
    """All availability status enum values filter correctly."""
    list_assets_filtered(mock_db, availability_status=status)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.availability_status = " in sql


@pytest.mark.parametrize("cond", [
    ConditionStatus.WORKING,
    ConditionStatus.DAMAGED,
    ConditionStatus.UNDER_REPAIR,
    ConditionStatus.MISSING,
])
def test_all_condition_status_values(mock_db, cond):
    """All condition status enum values filter correctly."""
    list_assets_filtered(mock_db, condition_status=cond)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.condition_status = " in sql


def test_maintenance_hold_true(mock_db):
    """Maintenance hold True filters equipment_assets.maintenance_hold = true."""
    list_assets_filtered(mock_db, maintenance_hold=True)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.maintenance_hold = true" in sql or "equipment_assets.maintenance_hold = 1" in sql or "equipment_assets.maintenance_hold IS true" in sql or "maintenance_hold =" in sql


def test_maintenance_hold_false(mock_db):
    """Maintenance hold False filters equipment_assets.maintenance_hold = false (distinguished from None)."""
    list_assets_filtered(mock_db, maintenance_hold=False)

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "WHERE" in sql
    assert "equipment_assets.maintenance_hold" in sql.split("WHERE")[1]

    # Compare with maintenance_hold=None
    mock_db.reset_mock()
    list_assets_filtered(mock_db, maintenance_hold=None)
    stmt_none = mock_db.scalars.call_args.args[0]
    sql_none = compile_stmt(stmt_none)
    assert "WHERE" not in sql_none


def test_location_filtering(mock_db):
    """Location filter applies ILIKE partial match without joining Equipment."""
    list_assets_filtered(mock_db, location="Lab 101")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "equipment_assets.location ILIKE" in sql
    assert "JOIN equipment" not in sql


def test_combined_filters(mock_db):
    """Combined search, category, status, hold, and location use AND across filters and OR within search."""
    eq_id = uuid4()
    list_assets_filtered(
        mock_db,
        search="Fluke",
        category="Instruments",
        equipment_id=eq_id,
        availability_status=AvailabilityStatus.AVAILABLE,
        condition_status=ConditionStatus.WORKING,
        maintenance_hold=False,
        location="Bench 1",
    )

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "JOIN equipment ON" in sql
    assert "equipment.name ILIKE" in sql
    assert "lower(equipment.category)" in sql
    assert "equipment_assets.equipment_id =" in sql
    assert "equipment_assets.availability_status =" in sql
    assert "equipment_assets.condition_status =" in sql
    assert "equipment_assets.maintenance_hold =" in sql
    assert "equipment_assets.location ILIKE" in sql


def test_empty_search_values_treated_as_absent(mock_db):
    """Empty or whitespace-only search, category, and location strings behave as absent filters."""
    list_assets_filtered(mock_db, search="   ", category="", location="  \t  ")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "WHERE" not in sql
    assert "JOIN equipment" not in sql


def test_whitespace_normalization_in_filters(mock_db):
    """Leading and trailing whitespace is stripped before filtering."""
    list_assets_filtered(mock_db, search="  Multimeter  ", category="  Tools  ", location="  Cabinet A  ")

    stmt = mock_db.scalars.call_args.args[0]
    sql = compile_stmt(stmt)
    assert "JOIN equipment ON" in sql
    assert "equipment.name ILIKE" in sql


# ==============================================================================
# 3. HTTP Endpoint & Validation Tests (FastAPI TestClient)
# ==============================================================================


@pytest.fixture
def client_factory(mock_db):
    def _create(role: UserRole | None = UserRole.FACULTY):
        staff = User(id=uuid4(), role=role.value if role else "STUDENT", is_active=True)

        def override_db():
            yield mock_db

        def override_user():
            if role is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return staff

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        return TestClient(app, raise_server_exceptions=False)

    yield _create
    app.dependency_overrides.clear()


def test_http_unfiltered_listing(client_factory, mock_db):
    """GET /assets returns 200 with list[AssetResponse]."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/assets")
    assert resp.status_code == 200
    assert resp.json() == []


def test_http_invalid_enum_availability_status_returns_422(client_factory):
    """Invalid availability_status value returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/assets?availability_status=INVALID_STATUS")
    assert resp.status_code == 422


def test_http_invalid_enum_condition_status_returns_422(client_factory):
    """Invalid condition_status value returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/assets?condition_status=BROKEN")
    assert resp.status_code == 422


def test_http_invalid_uuid_returns_422(client_factory):
    """Invalid equipment_id UUID returns 422 Unprocessable Entity."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get("/assets?equipment_id=not-a-valid-uuid")
    assert resp.status_code == 422


@pytest.mark.parametrize("limit_val, offset_val", [
    (0, 0),
    (101, 0),
    (50, -1),
])
def test_http_invalid_pagination_returns_422(client_factory, limit_val, offset_val):
    """Invalid limit (0, >100) or negative offset returns 422."""
    client = client_factory(UserRole.FACULTY)
    resp = client.get(f"/assets?limit={limit_val}&offset={offset_val}")
    assert resp.status_code == 422


def test_http_text_boundary_length_exceeded_returns_422(client_factory):
    """search > 100, category > 100, or location > 150 returns 422."""
    client = client_factory(UserRole.FACULTY)
    assert client.get(f"/assets?search={'a' * 101}").status_code == 422
    assert client.get(f"/assets?category={'a' * 101}").status_code == 422
    assert client.get(f"/assets?location={'a' * 151}").status_code == 422


def test_student_access_returns_403(client_factory):
    """Students are rejected from GET /assets with 403 Forbidden."""
    client = client_factory(UserRole.STUDENT)
    resp = client.get("/assets")
    assert resp.status_code == 403


def test_unauthenticated_access_returns_401(client_factory):
    """Unauthenticated caller is rejected with 401."""
    client = client_factory(None)
    resp = client.get("/assets")
    assert resp.status_code == 401


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT])
def test_all_staff_roles_permitted(client_factory, role):
    """Admin, Faculty, and Lab Assistant roles access GET /assets successfully."""
    client = client_factory(role)
    resp = client.get("/assets")
    assert resp.status_code == 200


def test_existing_response_model_serialization(client_factory, mock_db):
    """Response elements conform exactly to AssetResponse structure."""
    asset_id = uuid4()
    eq_id = uuid4()
    mock_asset = EquipmentAsset(
        id=asset_id,
        equipment_id=eq_id,
        asset_code="TEST-001",
        nfc_tag_id="04A1B2C3",
        serial_number="SN-12345",
        location="Lab 101",
        availability_status="AVAILABLE",
        condition_status="WORKING",
        maintenance_hold=False,
    )
    mock_db.scalars.return_value.all.return_value = [mock_asset]

    client = client_factory(UserRole.FACULTY)
    resp = client.get("/assets")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0] == {
        "id": str(asset_id),
        "equipment_id": str(eq_id),
        "asset_code": "TEST-001",
        "nfc_tag_id": "04A1B2C3",
        "serial_number": "SN-12345",
        "location": "Lab 101",
        "availability_status": "AVAILABLE",
        "condition_status": "WORKING",
        "maintenance_hold": False,
    }


def test_existing_nfc_lookup_behavior_unchanged(client_factory, mock_db):
    """GET /assets/lookup?nfc_tag_id={tag} retains existing exact lookup behavior."""
    asset_id = uuid4()
    eq_id = uuid4()
    mock_asset = EquipmentAsset(
        id=asset_id,
        equipment_id=eq_id,
        asset_code="NFC-ASSET",
        nfc_tag_id="TAG-XYZ",
        serial_number=None,
        location="Lab 202",
        availability_status="AVAILABLE",
        condition_status="WORKING",
        maintenance_hold=False,
    )
    mock_db.scalar.return_value = mock_asset

    client = client_factory(UserRole.LAB_ASSISTANT)
    resp = client.get("/assets/lookup?nfc_tag_id=TAG-XYZ")
    assert resp.status_code == 200
    assert resp.json()["asset_code"] == "NFC-ASSET"
    assert resp.json()["nfc_tag_id"] == "TAG-XYZ"
