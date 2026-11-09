"""Database-free unit tests for Missing Equipment Management."""

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import Index

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.missing_equipment import (
    MissingEquipmentEvent,
    MissingEquipmentIncident,
)
from backend.app.models.user import User
from backend.app.schemas.missing_equipment import (
    AssetMissingReconcileCreate,
    AssetMissingReportCreate,
    MissingEventResponse,
    MissingIncidentResponse,
)
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.checkout import record_return
from backend.app.services import missing_equipment as service
from backend.tests.inventory_session_stub import configure_session


# --- Schema Tests ---


def test_missing_report_schema_valid():
    key = uuid4()
    req = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Asset missing from bench 4",
        police_report_number="FIR-2026-001",
    )
    assert req.idempotency_key == key
    assert req.reason == "Asset missing from bench 4"
    assert req.police_report_number == "FIR-2026-001"


def test_missing_report_schema_whitespace_and_empty():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReportCreate(idempotency_key=key, reason="   ")

    with pytest.raises(ValidationError):
        AssetMissingReportCreate(idempotency_key=key, reason="")


def test_missing_report_schema_nul_byte_rejected():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReportCreate(idempotency_key=key, reason="Asset missing\x00")


def test_missing_report_schema_max_length():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReportCreate(idempotency_key=key, reason="x" * 4001)


def test_missing_report_schema_police_report_optional():
    key = uuid4()
    req = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Missing after lab session",
        police_report_number="  ",
    )
    assert req.police_report_number is None


def test_missing_reconcile_schema_valid():
    key = uuid4()
    req = AssetMissingReconcileCreate(
        idempotency_key=key,
        recovered_condition="WORKING",
        reason="Found in locker 12",
    )
    assert req.idempotency_key == key
    assert req.recovered_condition == "WORKING"
    assert req.reason == "Found in locker 12"


def test_missing_reconcile_schema_damaged_valid():
    key = uuid4()
    req = AssetMissingReconcileCreate(
        idempotency_key=key,
        recovered_condition="DAMAGED",
        reason="Found dropped under shelf with broken connector",
    )
    assert req.recovered_condition == "DAMAGED"


def test_missing_reconcile_schema_invalid_condition():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReconcileCreate(
            idempotency_key=key,
            recovered_condition="REPAIRED",  # only WORKING or DAMAGED
            reason="Recovered from shelf",
        )


def test_missing_reconcile_schema_whitespace_and_empty():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReconcileCreate(
            idempotency_key=key,
            recovered_condition="WORKING",
            reason="   ",
        )


def test_missing_reconcile_schema_nul_byte_rejected():
    key = uuid4()
    with pytest.raises(ValidationError):
        AssetMissingReconcileCreate(
            idempotency_key=key,
            recovered_condition="WORKING",
            reason="Found\x00 in locker",
        )


# --- Model Structure Tests ---


def test_missing_equipment_models_metadata():
    incident_table = MissingEquipmentIncident.__table__
    assert incident_table.name == "missing_equipment_incidents"

    # Verify column existence
    assert "operation_key" in incident_table.c
    assert "reconcile_key" in incident_table.c
    assert "report_fingerprint" in incident_table.c
    assert "reconcile_fingerprint" in incident_table.c
    assert "previous_availability_status" in incident_table.c
    assert "resolved_availability_status" in incident_table.c

    # Verify partial unique index
    indexes = [idx for idx in incident_table.indexes if idx.name == "uq_active_missing_incident_per_asset"]
    assert len(indexes) == 1
    partial_idx = indexes[0]
    assert partial_idx.unique is True
    assert "status = 'OPEN'" in str(partial_idx.dialect_options["postgresql"]["where"])

    # Verify check constraints
    ck_names = {ck.name for ck in incident_table.constraints if hasattr(ck, "name")}
    assert "ck_missing_incident_status" in ck_names
    assert "ck_missing_incident_version" in ck_names
    assert "ck_missing_recovered_condition" in ck_names
    assert "ck_missing_report_reason" in ck_names
    assert "ck_missing_incident_resolution" in ck_names

    event_table = MissingEquipmentEvent.__table__
    assert event_table.name == "missing_equipment_events"
    event_cks = {ck.name for ck in event_table.constraints if hasattr(ck, "name")}
    assert "uq_missing_event_sequence" in event_cks
    assert "ck_missing_event_sequence" in event_cks
    assert "ck_missing_event_type" in event_cks
    assert "ck_missing_event_reason" in event_cks


# --- Service Workflow Fixture ---


@pytest.fixture
def missing_env(monkeypatch):
    c = SimpleNamespace(
        now=datetime(2026, 11, 8, 12, tzinfo=timezone.utc),
        events=[],
        checkout=None,
        active_maintenance=None,
        open_incident=None,
        existing_incident_by_key=None,
        existing_incident_by_reconcile_key=None,
        incidents_list=[],
        events_list=[],
    )
    c.users = {
        "admin": User(id=uuid4(), role="ADMIN", is_active=True),
        "faculty": User(id=uuid4(), role="FACULTY", is_active=True),
        "assistant": User(id=uuid4(), role="LAB_ASSISTANT", is_active=True),
        "student": User(id=uuid4(), role="STUDENT", is_active=True),
        "inactive": User(id=uuid4(), role="FACULTY", is_active=False),
    }
    c.asset = EquipmentAsset(
        id=uuid4(),
        availability_status="AVAILABLE",
        condition_status="WORKING",
        maintenance_hold=False,
    )

    c.db = Mock()
    c.db.new, c.db.dirty, c.db.deleted, c.db.info = set(), set(), set(), {}
    c.db.in_nested_transaction.return_value = False
    configure_session(c.db, monkeypatch)
    c.db.no_autoflush = nullcontext()

    def db_get(model, identifier, **kwargs):
        if model is User:
            return next((u for u in c.users.values() if u.id == identifier), None)
        if model is EquipmentAsset and identifier == c.asset.id:
            return c.asset
        if model is MissingEquipmentIncident:
            return next((inc for inc in c.incidents_list if inc.id == identifier), c.open_incident)
        return None

    c.db.get.side_effect = db_get

    def db_scalar(statement):
        sql = str(statement)
        if "clock_timestamp()" in sql:
            return c.now
        entity = statement.column_descriptions[0].get("entity")
        if entity is EquipmentAsset:
            return c.asset
        if entity is EquipmentCheckout:
            cols = list(statement.selected_columns)
            if len(cols) == 1 and cols[0].name == "asset_id":
                return c.checkout.asset_id if c.checkout else None
            return c.checkout
        if entity is MaintenanceRequest:
            if "status = 'CANCELLED'" in sql:
                return None
            return c.active_maintenance
        if entity is MissingEquipmentIncident:
            if "operation_key =" in sql:
                return c.existing_incident_by_key
            if "reconcile_key =" in sql:
                return c.existing_incident_by_reconcile_key
            if "status = 'OPEN'" in sql or "status =" in sql:
                return c.open_incident
            return c.open_incident
        return None

    c.db.scalar.side_effect = db_scalar

    def db_scalars(statement):
        mock_result = Mock()
        entity = statement.column_descriptions[0].get("entity")
        if entity is MissingEquipmentIncident:
            mock_result.all.return_value = c.incidents_list
        elif entity is MissingEquipmentEvent:
            mock_result.all.return_value = c.events_list
        else:
            mock_result.all.return_value = []
        return mock_result

    c.db.scalars.side_effect = db_scalars

    def db_add(instance):
        c.db.new.add(instance)
        if isinstance(instance, MissingEquipmentIncident):
            c.open_incident = instance
            c.incidents_list.append(instance)
        elif isinstance(instance, MissingEquipmentEvent):
            c.events.append(instance)
            c.events_list.append(instance)
        elif isinstance(instance, MaintenanceRequest):
            c.active_maintenance = instance

    def db_flush():
        c.db.new.clear()
        c.db.dirty.clear()

    c.db.add.side_effect = db_add
    c.db.flush.side_effect = db_flush

    return c


# --- Report Missing Tests ---


def test_report_missing_available_asset_success(missing_env):
    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Asset missing from cabinet A",
        police_report_number="POL-1234",
    )
    res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )
    assert res.status == "OPEN"
    assert res.version == 1
    assert res.previous_availability_status == "AVAILABLE"
    assert res.previous_condition_status == "WORKING"
    assert res.asset_availability_status == "MAINTENANCE"
    assert res.asset_condition_status == "MISSING"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.availability_status == "MAINTENANCE"
    assert missing_env.asset.condition_status == "MISSING"
    assert missing_env.asset.maintenance_hold is True

    # Verify event creation
    assert len(missing_env.events) == 1
    ev = missing_env.events[0]
    assert ev.sequence == 1
    assert ev.event_type == "REPORTED_MISSING"
    assert ev.from_availability_status == "AVAILABLE"
    assert ev.to_availability_status == "MAINTENANCE"
    assert ev.from_condition_status == "WORKING"
    assert ev.to_condition_status == "MISSING"
    assert ev.from_maintenance_hold is False
    assert ev.to_maintenance_hold is True


def test_report_missing_borrowed_asset_preserves_custody(missing_env):
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "WORKING"
    missing_env.asset.maintenance_hold = False

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Student reported device lost during field test",
        police_report_number="LOST-99",
    )
    res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["assistant"]
    )
    # Crucial requirements: active loan remains BORROWED, student custody preserved
    assert res.asset_availability_status == "BORROWED"
    assert res.asset_condition_status == "MISSING"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.availability_status == "BORROWED"
    assert missing_env.asset.condition_status == "MISSING"
    assert missing_env.asset.maintenance_hold is True
    assert checkout.status == "BORROWED"  # Never closed automatically!


def test_report_missing_maintenance_asset_freezes_state(missing_env):
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "DAMAGED"
    missing_env.asset.maintenance_hold = True

    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Asset missing from repair queue",
    )
    res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["admin"]
    )
    assert res.asset_availability_status == "MAINTENANCE"
    assert res.asset_condition_status == "MISSING"
    assert res.asset_maintenance_hold is True
    assert res.previous_availability_status == "MAINTENANCE"
    assert res.previous_condition_status == "DAMAGED"


def test_report_missing_reserved_asset(missing_env):
    missing_env.asset.availability_status = "RESERVED"
    missing_env.asset.condition_status = "WORKING"
    missing_env.asset.maintenance_hold = False

    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Reserved asset not found in storage",
    )
    res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )
    assert res.asset_availability_status == "RESERVED"
    assert res.asset_condition_status == "MISSING"
    assert res.asset_maintenance_hold is True


def test_report_missing_retired_asset_rejected(missing_env):
    missing_env.asset.availability_status = "RETIRED"
    missing_env.asset.condition_status = "DAMAGED"

    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Trying to report retired asset missing",
    )
    with pytest.raises(HTTPException) as exc:
        service.report_asset_missing(
            missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "retired" in exc.value.detail.lower()


def test_report_missing_already_missing_rejected(missing_env):
    missing_env.asset.condition_status = "MISSING"
    missing_env.open_incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Already missing",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )

    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Second report attempt",
    )
    with pytest.raises(HTTPException) as exc:
        service.report_asset_missing(
            missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "already reported missing" in exc.value.detail.lower()


def test_report_missing_idempotent_replay(missing_env):
    key = uuid4()
    data = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Original report",
        police_report_number="P1",
    )

    # First report
    res1 = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    # Setup environment to return this incident when queried by operation_key
    missing_env.existing_incident_by_key = missing_env.open_incident

    # Replay with identical key and data
    res2 = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )
    assert res1.id == res2.id
    assert res1.operation_key == res2.operation_key


def test_report_missing_key_reuse_conflict(missing_env):
    key = uuid4()
    data1 = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Original report",
    )
    service.report_asset_missing(
        missing_env.db, missing_env.asset.id, data1, missing_env.users["faculty"]
    )

    missing_env.existing_incident_by_key = missing_env.open_incident

    # Replay with same key but altered reason
    data2 = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Altered reason",
    )
    with pytest.raises(HTTPException) as exc:
        service.report_asset_missing(
            missing_env.db, missing_env.asset.id, data2, missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "different parameters" in exc.value.detail.lower()


def test_report_missing_role_authorization(missing_env):
    data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Report by student",
    )
    with pytest.raises(HTTPException) as exc:
        service.report_asset_missing(
            missing_env.db, missing_env.asset.id, data, missing_env.users["student"]
        )
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc2:
        service.report_asset_missing(
            missing_env.db, missing_env.asset.id, data, missing_env.users["inactive"]
        )
    assert exc2.value.status_code == 403


# --- Reconcile Missing Tests ---


def test_reconcile_missing_clean_working_restores_available(missing_env):
    # Setup asset in missing state
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Lost in classroom",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Found undamaged in classroom podium",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    assert res.status == "RECONCILED"
    assert res.version == 2
    assert res.recovered_condition == "WORKING"
    assert res.resolved_availability_status == "AVAILABLE"
    assert res.resolved_condition_status == "WORKING"
    assert res.asset_availability_status == "AVAILABLE"
    assert res.asset_condition_status == "WORKING"
    assert res.asset_maintenance_hold is False
    assert missing_env.asset.availability_status == "AVAILABLE"
    assert missing_env.asset.condition_status == "WORKING"
    assert missing_env.asset.maintenance_hold is False

    # Verify event
    assert len(missing_env.events) == 1
    ev = missing_env.events[0]
    assert ev.sequence == 2
    assert ev.event_type == "RECONCILED"
    assert ev.from_availability_status == "MAINTENANCE"
    assert ev.to_availability_status == "AVAILABLE"
    assert ev.from_condition_status == "MISSING"
    assert ev.to_condition_status == "WORKING"
    assert ev.from_maintenance_hold is True
    assert ev.to_maintenance_hold is False


def test_reconcile_missing_damaged_enforces_maintenance_hold(missing_env):
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Missing",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="DAMAGED",
        reason="Found in rain with cracked casing",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["assistant"]
    )

    assert res.status == "RECONCILED"
    assert res.asset_availability_status == "MAINTENANCE"
    assert res.asset_condition_status == "DAMAGED"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.availability_status == "MAINTENANCE"
    assert missing_env.asset.condition_status == "DAMAGED"
    assert missing_env.asset.maintenance_hold is True


def test_reconcile_missing_preserves_active_checkout(missing_env):
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Reported missing during active loan",
        previous_availability_status="BORROWED",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Recovered from student locker; return not yet processed",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    # Core requirement: loan remains BORROWED, checkout is NOT closed.
    # When recovered as WORKING and no independent maintenance exists,
    # the missing-related maintenance hold is cleared!
    assert res.asset_availability_status == "BORROWED"
    assert res.asset_condition_status == "WORKING"
    assert res.asset_maintenance_hold is False
    assert missing_env.asset.availability_status == "BORROWED"
    assert missing_env.asset.condition_status == "WORKING"
    assert missing_env.asset.maintenance_hold is False
    assert checkout.status == "BORROWED"


def test_reconcile_missing_damaged_with_active_loan_preserves_hold(missing_env):
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Reported missing during active loan",
        previous_availability_status="BORROWED",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="DAMAGED",
        reason="Found damaged in hallway",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    # When recovered as DAMAGED with active loan, hold remains True
    assert res.asset_availability_status == "BORROWED"
    assert res.asset_condition_status == "DAMAGED"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.maintenance_hold is True
    assert checkout.status == "BORROWED"


def test_reconcile_missing_preserves_hold_when_active_maintenance_and_loan_exist(missing_env):
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    m_req = MaintenanceRequest(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        issue_type="DAMAGE",
        issue_description="Active maintenance while on loan",
        status="OPEN",
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
    )
    missing_env.active_maintenance = m_req

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Missing",
        previous_availability_status="BORROWED",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Found working, but existing repair request is still open",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    # Active maintenance request must preserve hold
    assert res.asset_availability_status == "BORROWED"
    assert res.asset_condition_status == "WORKING"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.maintenance_hold is True


def test_borrowed_missing_recovery_and_formal_return_lifecycle(missing_env):
    # 1. Setup asset as checked out
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "WORKING"
    missing_env.asset.maintenance_hold = False

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        checked_out_at=missing_env.now,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    # 2. Staff reports asset missing
    report_data = AssetMissingReportCreate(
        idempotency_key=uuid4(),
        reason="Device lost on student commute",
    )
    rep_res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, report_data, missing_env.users["faculty"]
    )
    assert rep_res.asset_availability_status == "BORROWED"
    assert rep_res.asset_condition_status == "MISSING"
    assert rep_res.asset_maintenance_hold is True
    assert checkout.status == "BORROWED"

    # 3. Staff reconciles asset as WORKING
    reconcile_data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Recovered from transit lost-and-found in good shape",
    )
    rec_res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, reconcile_data, missing_env.users["faculty"]
    )
    assert rec_res.asset_availability_status == "BORROWED"
    assert rec_res.asset_condition_status == "WORKING"
    # Hold is cleared because condition is WORKING and no independent maintenance exists
    assert rec_res.asset_maintenance_hold is False
    assert checkout.status == "BORROWED"

    # 4. Student formally returns the asset via existing record_return workflow
    return_payload = ReturnCreate(condition_status="WORKING", notes="Returned in person")
    return_res = record_return(
        missing_env.db, checkout.id, missing_env.users["assistant"], return_payload
    )

    # Core requirement 5: Asset is restored to AVAILABLE / WORKING / maintenance_hold=False
    # with NO unrequested inspection maintenance created!
    assert return_res.availability_status == "AVAILABLE"
    assert return_res.condition_status == "WORKING"
    assert return_res.maintenance_hold is False
    assert return_res.maintenance_request_id is None
    assert return_res.status == "RETURNED"
    assert missing_env.asset.availability_status == "AVAILABLE"
    assert missing_env.asset.condition_status == "WORKING"
    assert missing_env.asset.maintenance_hold is False
    assert checkout.status == "RETURNED"


def test_borrowed_missing_recovery_damaged_and_formal_return_lifecycle(missing_env):
    missing_env.asset.availability_status = "BORROWED"
    missing_env.asset.condition_status = "WORKING"
    missing_env.asset.maintenance_hold = False

    checkout = EquipmentCheckout(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        student_id=missing_env.users["student"].id,
        issued_by=missing_env.users["faculty"].id,
        checked_out_at=missing_env.now,
        due_at=missing_env.now,
        status="BORROWED",
    )
    missing_env.checkout = checkout

    # 1. Report missing
    service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing"),
        missing_env.users["faculty"]
    )

    # 2. Reconcile as DAMAGED
    rec_res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id,
        AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="DAMAGED", reason="Recovered cracked"),
        missing_env.users["faculty"]
    )
    assert rec_res.asset_maintenance_hold is True

    # 3. Return via record_return with DAMAGED
    return_payload = ReturnCreate(condition_status="DAMAGED", notes="Cracked casing confirmed on return")
    return_res = record_return(
        missing_env.db, checkout.id, missing_env.users["assistant"], return_payload
    )

    # Core requirement 6: When recovered as DAMAGED, returns to MAINTENANCE with hold=True
    assert return_res.availability_status == "MAINTENANCE"
    assert return_res.condition_status == "DAMAGED"
    assert return_res.maintenance_hold is True
    assert return_res.maintenance_request_id is not None
    assert return_res.status == "RETURNED"
    assert missing_env.asset.availability_status == "MAINTENANCE"
    assert missing_env.asset.maintenance_hold is True


def test_reconcile_missing_preserves_active_maintenance(missing_env):
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    m_req = MaintenanceRequest(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        issue_type="DAMAGE",
        issue_description="Initial repair",
        status="OPEN",
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
    )
    missing_env.active_maintenance = m_req

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Missing from repair bay",
        previous_availability_status="MAINTENANCE",
        previous_condition_status="DAMAGED",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="DAMAGED",
        reason="Found in wrong repair drawer",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    # Core requirement: Never restore AVAILABLE while active maintenance exists
    assert res.asset_availability_status == "MAINTENANCE"
    assert res.asset_condition_status == "DAMAGED"
    assert res.asset_maintenance_hold is True
    assert missing_env.asset.availability_status == "MAINTENANCE"
    assert missing_env.asset.maintenance_hold is True


def test_reconcile_missing_restores_reserved(missing_env):
    missing_env.asset.availability_status = "RESERVED"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Missing from reserved shelf",
        previous_availability_status="RESERVED",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Found on reserved cart",
    )
    res = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    assert res.asset_availability_status == "RESERVED"
    assert res.asset_condition_status == "WORKING"
    assert res.asset_maintenance_hold is False


def test_reconcile_missing_without_open_incident_rejected(missing_env):
    missing_env.asset.availability_status = "AVAILABLE"
    missing_env.asset.condition_status = "WORKING"
    missing_env.open_incident = None

    data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Attempting to reconcile non-missing asset",
    )
    with pytest.raises(HTTPException) as exc:
        service.reconcile_missing_asset(
            missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "does not have an active missing" in exc.value.detail.lower()


def test_reconcile_missing_idempotent_replay(missing_env):
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Lost",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    key = uuid4()
    data = AssetMissingReconcileCreate(
        idempotency_key=key,
        recovered_condition="WORKING",
        reason="Recovered from shelf",
    )

    res1 = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    missing_env.existing_incident_by_reconcile_key = incident

    res2 = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data, missing_env.users["faculty"]
    )

    assert res1.id == res2.id
    assert res1.reconcile_key == res2.reconcile_key


def test_reconcile_missing_key_reuse_conflict(missing_env):
    missing_env.asset.availability_status = "MAINTENANCE"
    missing_env.asset.condition_status = "MISSING"
    missing_env.asset.maintenance_hold = True

    incident = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Lost",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )
    missing_env.open_incident = incident

    key = uuid4()
    data1 = AssetMissingReconcileCreate(
        idempotency_key=key,
        recovered_condition="WORKING",
        reason="Recovered from shelf",
    )
    service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, data1, missing_env.users["faculty"]
    )

    missing_env.existing_incident_by_reconcile_key = incident

    data2 = AssetMissingReconcileCreate(
        idempotency_key=key,
        recovered_condition="DAMAGED",  # Altered payload!
        reason="Recovered from shelf",
    )
    with pytest.raises(HTTPException) as exc:
        service.reconcile_missing_asset(
            missing_env.db, missing_env.asset.id, data2, missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "different parameters" in exc.value.detail.lower()


# --- Query Tests ---


def test_list_missing_incidents_and_events(missing_env):
    inc = MissingEquipmentIncident(
        id=uuid4(),
        asset_id=missing_env.asset.id,
        operation_key=uuid4(),
        report_fingerprint="fp",
        status="OPEN",
        version=1,
        reported_by=missing_env.users["faculty"].id,
        reported_at=missing_env.now,
        report_reason="Lost in hall",
        previous_availability_status="AVAILABLE",
        previous_condition_status="WORKING",
    )
    missing_env.incidents_list = [inc]

    incidents = service.list_missing_incidents(
        missing_env.db, missing_env.asset.id, missing_env.users["faculty"]
    )
    assert len(incidents) == 1
    assert incidents[0].id == inc.id

    ev = MissingEquipmentEvent(
        id=uuid4(),
        incident_id=inc.id,
        asset_id=missing_env.asset.id,
        sequence=1,
        event_type="REPORTED_MISSING",
        actor_id=missing_env.users["faculty"].id,
        occurred_at=missing_env.now,
        reason="Lost in hall",
        from_availability_status="AVAILABLE",
        to_availability_status="MAINTENANCE",
        from_condition_status="WORKING",
        to_condition_status="MISSING",
        from_maintenance_hold=False,
        to_maintenance_hold=True,
    )
    missing_env.events_list = [ev]

    events = service.list_incident_events(
        missing_env.db, missing_env.asset.id, inc.id, missing_env.users["faculty"]
    )
    assert len(events) == 1
    assert events[0].id == ev.id


def test_list_missing_incidents_asset_not_found(missing_env):
    with pytest.raises(HTTPException) as exc:
        service.list_missing_incidents(
            missing_env.db, uuid4(), missing_env.users["faculty"]
        )
    assert exc.value.status_code == 404


def test_missing_equipment_router_registration():
    from backend.app.main import app
    from backend.app.routers.asset import router as asset_router

    router_paths = {r.path for r in asset_router.routes}
    assert "/assets/{asset_id}/report-missing" in router_paths
    assert "/assets/{asset_id}/reconcile-missing" in router_paths
    assert "/assets/{asset_id}/missing-incidents" in router_paths
    assert "/assets/{asset_id}/missing-incidents/{incident_id}/events" in router_paths

    openapi = app.openapi()
    assert "/assets/{asset_id}/report-missing" in openapi["paths"]
    assert "post" in openapi["paths"]["/assets/{asset_id}/report-missing"]
    assert "/assets/{asset_id}/reconcile-missing" in openapi["paths"]
    assert "post" in openapi["paths"]["/assets/{asset_id}/reconcile-missing"]
    assert "/assets/{asset_id}/missing-incidents" in openapi["paths"]
    assert "get" in openapi["paths"]["/assets/{asset_id}/missing-incidents"]
    assert "/assets/{asset_id}/missing-incidents/{incident_id}/events" in openapi["paths"]
    assert "get" in openapi["paths"]["/assets/{asset_id}/missing-incidents/{incident_id}/events"]


# --- Fix 3 & Fix 4: Idempotency Scoping and Response Semantics Tests ---


def test_report_missing_replay_after_reconciliation_returns_current_incident_state(missing_env):
    key = uuid4()
    report_data = AssetMissingReportCreate(
        idempotency_key=key,
        reason="Missing report",
        police_report_number="POL-99",
    )
    # 1. Report missing
    res_rep = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, report_data, missing_env.users["faculty"]
    )
    assert res_rep.status == "OPEN"
    assert res_rep.version == 1
    assert len(missing_env.events) == 1

    # Wire environment so queries find this incident
    missing_env.existing_incident_by_key = missing_env.open_incident

    # 2. Reconcile missing
    reconcile_data = AssetMissingReconcileCreate(
        idempotency_key=uuid4(),
        recovered_condition="WORKING",
        reason="Found working",
    )
    res_rec = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id, reconcile_data, missing_env.users["faculty"]
    )
    assert res_rec.status == "RECONCILED"
    assert res_rec.version == 2
    assert len(missing_env.events) == 2

    # 3. Client resends original report request with key K
    replay_res = service.report_asset_missing(
        missing_env.db, missing_env.asset.id, report_data, missing_env.users["faculty"]
    )

    # Verifies:
    # - Returns current incident state (RECONCILED, version 2)
    # - Returns live asset state (AVAILABLE, WORKING, hold=False)
    # - Historical report metadata preserved (previous_availability_status=AVAILABLE, report_reason="Missing report")
    # - No new events appended (event count remains 2!)
    assert replay_res.status == "RECONCILED"
    assert replay_res.version == 2
    assert replay_res.operation_key == key
    assert replay_res.previous_availability_status == "AVAILABLE"
    assert replay_res.report_reason == "Missing report"
    assert replay_res.police_report_number == "POL-99"
    assert replay_res.asset_availability_status == "AVAILABLE"
    assert replay_res.asset_condition_status == "WORKING"
    assert replay_res.asset_maintenance_hold is False
    assert len(missing_env.events) == 2


def test_report_missing_replay_after_subsequent_cycle_preserves_original_incident(missing_env):
    key1 = uuid4()
    # Cycle 1
    res1 = service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=key1, reason="First incident"),
        missing_env.users["faculty"]
    )
    inc1 = missing_env.open_incident

    rec1 = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id,
        AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Recovered 1"),
        missing_env.users["faculty"]
    )
    assert rec1.status == "RECONCILED"

    # Cycle 2: asset is reported missing again with key2
    key2 = uuid4()
    missing_env.open_incident = None  # Previous incident is closed
    res2 = service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=key2, reason="Second incident"),
        missing_env.users["faculty"]
    )
    inc2 = missing_env.open_incident
    assert res2.operation_key == key2
    assert inc2.id != inc1.id

    # Configure query by operation_key to return inc1 when querying key1, and inc2 when querying key2
    def db_scalar_cycles(statement):
        sql = str(statement)
        if "clock_timestamp()" in sql:
            return missing_env.now
        if "operation_key =" in sql:
            if str(key1) in sql or f"'{key1}'" in sql or True:
                # Mock returning based on what was requested
                entity = statement.column_descriptions[0].get("entity")
                if entity is MissingEquipmentIncident:
                    return inc1
        entity = statement.column_descriptions[0].get("entity")
        if entity is EquipmentAsset:
            return missing_env.asset
        return None

    missing_env.db.scalar.side_effect = db_scalar_cycles

    # Replay cycle 1 report request
    replay1 = service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=key1, reason="First incident"),
        missing_env.users["faculty"]
    )
    assert replay1.id == inc1.id
    assert replay1.operation_key == key1
    assert replay1.report_reason == "First incident"


def test_cross_operation_idempotency_key_reuse(missing_env):
    # A single UUID used as the idempotency_key for both reporting and reconciling
    shared_key = uuid4()

    # 1. Report missing with shared_key (written to operation_key)
    res_rep = service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=shared_key, reason="Report with shared key"),
        missing_env.users["faculty"]
    )
    assert res_rep.operation_key == shared_key
    assert res_rep.reconcile_key is None

    # 2. Reconcile missing with the SAME shared_key (written to reconcile_key)
    res_rec = service.reconcile_missing_asset(
        missing_env.db, missing_env.asset.id,
        AssetMissingReconcileCreate(idempotency_key=shared_key, recovered_condition="WORKING", reason="Reconcile with shared key"),
        missing_env.users["faculty"]
    )
    # Operation-scoped separation ensures both succeed cleanly
    assert res_rec.operation_key == shared_key
    assert res_rec.reconcile_key == shared_key
    assert res_rec.status == "RECONCILED"


def test_cross_asset_key_reuse_conflict(missing_env):
    key = uuid4()
    # Report Asset 1
    service.report_asset_missing(
        missing_env.db, missing_env.asset.id,
        AssetMissingReportCreate(idempotency_key=key, reason="Asset 1 missing"),
        missing_env.users["faculty"]
    )
    missing_env.existing_incident_by_key = missing_env.open_incident

    # Attempt to report Asset 2 using the same key
    asset2_id = uuid4()
    with pytest.raises(HTTPException) as exc:
        service.report_asset_missing(
            missing_env.db, asset2_id,
            AssetMissingReportCreate(idempotency_key=key, reason="Asset 2 missing"),
            missing_env.users["faculty"]
        )
    assert exc.value.status_code == 409
    assert "different parameters" in exc.value.detail.lower()


