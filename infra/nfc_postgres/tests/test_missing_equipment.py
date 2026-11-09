"""Isolated PostgreSQL integration tests for Missing Equipment Management.

Exercises Phase B migration verification and Phase C real PostgreSQL scenarios
in the disposable, opt-in container instance.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import select, text, func
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from alembic.config import Config
from alembic import command

from backend.alembic.application_safety import apply_application, verify_application, HEAD, PREVIOUS_REVISIONS
from backend.app.models.user import User
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.missing_equipment import MissingEquipmentIncident, MissingEquipmentEvent
from backend.app.schemas.missing_equipment import AssetMissingReportCreate, AssetMissingReconcileCreate
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.missing_equipment import (
    report_asset_missing,
    reconcile_missing_asset,
    list_missing_incidents,
    list_incident_events,
)
from backend.app.services.checkout import record_return
from infra.nfc_postgres.harness import ADMIN
from scripts import deploy_nfc_demo as baseline


@pytest.fixture
def app_env(database):
    """Set up database at HEAD revision c7d3e91024ab with seed entities."""
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
        id=uuid4(), college_id="FACULTY_1", full_name="Faculty Staff",
        email="faculty@college.invalid", role="FACULTY", is_active=True
    )
    staff2 = User(
        id=uuid4(), college_id="FACULTY_2", full_name="Second Faculty",
        email="faculty2@college.invalid", role="FACULTY", is_active=True
    )
    student = User(
        id=uuid4(), college_id="STUDENT_1", full_name="Student Borrower",
        email="student@college.invalid", role="STUDENT", is_active=True
    )
    equipment = Equipment(
        id=uuid4(), name="Oscilloscope Rig", category="Lab Tools",
        tracking_type="INDIVIDUAL", default_location="Lab 101"
    )
    asset = EquipmentAsset(
        id=uuid4(), equipment_id=equipment.id, asset_code="SCOPE-INT-1",
        location="Lab 101", availability_status="AVAILABLE", condition_status="WORKING",
        maintenance_hold=False
    )

    with Session(admin, expire_on_commit=False) as db:
        db.add_all([staff, staff2, student, equipment, asset])
        db.commit()

    app_engine = database.engine("inventory_app")
    return SimpleNamespace(
        h=database,
        engine=app_engine,
        admin=admin,
        staff=staff,
        staff2=staff2,
        student=student,
        equipment=equipment,
        asset=asset,
    )


# ==============================================================================
# PHASE B: MIGRATION VERIFICATION
# ==============================================================================

def test_phase_b_baseline_migrations_and_head(database):
    """Verify baseline migrations reach authoritative HEAD d6f2b8a931c0."""
    database.deploy()
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        rev = c.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == baseline.HEAD
        assert rev == "d6f2b8a931c0"


def test_phase_b_incremental_upgrade_from_previous_to_head(database):
    """Verify incremental upgrade from e8a4c2d190ab to c7d3e91024ab."""
    database.deploy()
    # Step 1: Upgrade from baseline to predecessor application revision e8a4c2d190ab
    prev_rev = "e8a4c2d190ab"
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        config = Config("alembic_nfc_application.ini")
        config.attributes.update(connection=c, nfc_demo_identity=baseline.identity(database.settings, ADMIN))
        command.upgrade(config, prev_rev)
        c.commit()

    # Step 2: Confirm database is at predecessor revision
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        rev = c.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == prev_rev

    # Step 3: Run apply_application and confirm it upgrades cleanly to HEAD c7d3e91024ab
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        c.rollback()
        apply_application(c, database.settings)

    # Step 4: Verify schema objects and version
    with database.engine(ADMIN).connect() as c:
        database.verify_engine_identity(c)
        rev = c.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == HEAD
        assert rev == "c7d3e91024ab"
        verify_application(c, database.settings)


def test_phase_b_schema_objects_ownership_and_privileges(app_env):
    """Confirm missing equipment tables, indexes, triggers, and runtime privileges."""
    c = app_env
    with c.admin.connect() as conn:
        # Tables and ownership
        rows = conn.execute(text("""
            SELECT c.relname, pg_get_userbyid(c.relowner)
            FROM pg_class c
            WHERE c.relname IN ('missing_equipment_incidents', 'missing_equipment_events')
        """)).all()
        table_owners = dict(rows)
        assert table_owners.get("missing_equipment_incidents") == "inventory_metadata_audit_owner"
        assert table_owners.get("missing_equipment_events") == "inventory_metadata_audit_owner"

        # Partial unique index
        idx = conn.execute(text("""
            SELECT indexname, indexdef FROM pg_indexes
            WHERE tablename = 'missing_equipment_incidents'
            AND indexname = 'uq_active_missing_incident_per_asset'
        """)).one()
        assert "status" in idx[1] and "OPEN" in idx[1]

        # Triggers
        triggers = set(conn.scalars(text("""
            SELECT tgname FROM pg_trigger
            WHERE tgrelid IN ('missing_equipment_incidents'::regclass, 'missing_equipment_events'::regclass)
            AND NOT tgisinternal
        """)).all())
        expected_triggers = {
            "missing_incident_no_delete",
            "missing_incident_no_truncate",
            "missing_event_no_delete",
            "missing_event_no_truncate",
            "missing_event_no_update",
        }
        assert expected_triggers.issubset(triggers)


# ==============================================================================
# PHASE C: FUNCTIONAL INTEGRATION SCENARIOS
# ==============================================================================

def test_phase_c_scenario_1_available_asset_reported_missing(app_env):
    """Scenario 1: AVAILABLE asset reported missing transitions to MAINTENANCE / MISSING."""
    c = app_env
    key = uuid4()
    with Session(c.engine) as db:
        rep = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key, reason="Left on bench, missing now"),
            c.staff
        )
        assert rep.status == "OPEN"
        assert rep.asset_availability_status == "MAINTENANCE"
        assert rep.asset_condition_status == "MISSING"
        assert rep.asset_maintenance_hold is True

    with Session(c.engine) as db:
        reloaded = db.get(EquipmentAsset, c.asset.id)
        assert reloaded.availability_status == "MAINTENANCE"
        assert reloaded.condition_status == "MISSING"
        assert reloaded.maintenance_hold is True


def test_phase_c_scenario_2_missing_recovered_working(app_env):
    """Scenario 2: Missing asset recovered as WORKING restores to AVAILABLE / WORKING / hold=False."""
    c = app_env
    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing"),
            c.staff
        )

    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Recovered safely"),
            c.staff
        )
        assert rec.status == "RECONCILED"
        assert rec.asset_availability_status == "AVAILABLE"
        assert rec.asset_condition_status == "WORKING"
        assert rec.asset_maintenance_hold is False

    with Session(c.engine) as db:
        reloaded = db.get(EquipmentAsset, c.asset.id)
        assert reloaded.availability_status == "AVAILABLE"
        assert reloaded.condition_status == "WORKING"
        assert reloaded.maintenance_hold is False


def test_phase_c_scenario_3_missing_recovered_damaged(app_env):
    """Scenario 3: Missing asset recovered as DAMAGED transitions to MAINTENANCE / DAMAGED / hold=True."""
    c = app_env
    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing"),
            c.staff
        )

    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="DAMAGED", reason="Cracked casing"),
            c.staff
        )
        assert rec.status == "RECONCILED"
        assert rec.asset_availability_status == "MAINTENANCE"
        assert rec.asset_condition_status == "DAMAGED"
        assert rec.asset_maintenance_hold is True

    with Session(c.engine) as db:
        reloaded = db.get(EquipmentAsset, c.asset.id)
        assert reloaded.availability_status == "MAINTENANCE"
        assert reloaded.condition_status == "DAMAGED"
        assert reloaded.maintenance_hold is True


def test_phase_c_scenario_4_borrowed_asset_reported_missing(app_env):
    """Scenario 4: Borrowed asset reported missing preserves loan custody."""
    c = app_env
    # Check out asset
    with Session(c.engine) as db:
        asset = db.get(EquipmentAsset, c.asset.id)
        asset.availability_status = "BORROWED"
        checkout = EquipmentCheckout(
            id=uuid4(), asset_id=c.asset.id, student_id=c.student.id, issued_by=c.staff.id,
            checked_out_at=datetime.now(timezone.utc), due_at=datetime.now(timezone.utc) + timedelta(days=2),
            status="BORROWED"
        )
        db.add(checkout)
        db.commit()
        checkout_id = checkout.id

    with Session(c.engine) as db:
        rep = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Student lost it on campus"),
            c.staff
        )
        assert rep.asset_availability_status == "BORROWED"
        assert rep.asset_condition_status == "MISSING"
        assert rep.asset_maintenance_hold is True

    with Session(c.engine) as db:
        reloaded_chk = db.get(EquipmentCheckout, checkout_id)
        assert reloaded_chk.status == "BORROWED"
        assert reloaded_chk.returned_at is None


def test_phase_c_scenario_5_borrowed_recovered_working_and_formal_return(app_env):
    """Scenario 5: Borrowed asset recovered as WORKING and formally returned clears hold without extra ticket."""
    c = app_env
    with Session(c.engine) as db:
        asset = db.get(EquipmentAsset, c.asset.id)
        asset.availability_status = "BORROWED"
        checkout = EquipmentCheckout(
            id=uuid4(), asset_id=c.asset.id, student_id=c.student.id, issued_by=c.staff.id,
            checked_out_at=datetime.now(timezone.utc) - timedelta(days=1),
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
            status="BORROWED"
        )
        db.add(checkout)
        db.commit()
        checkout_id = checkout.id

    # Report missing
    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing on loan"),
            c.staff
        )

    # Reconcile working -> hold must be cleared!
    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Recovered from lab shelf"),
            c.staff
        )
        assert rec.asset_availability_status == "BORROWED"
        assert rec.asset_condition_status == "WORKING"
        assert rec.asset_maintenance_hold is False

    # Formal return
    with Session(c.engine) as db:
        ret = record_return(
            db, checkout_id, c.staff,
            ReturnCreate(condition_status="WORKING", notes="Returned formally")
        )
        assert ret.status == "RETURNED"
        assert ret.availability_status == "AVAILABLE"
        assert ret.condition_status == "WORKING"
        assert ret.maintenance_hold is False
        assert ret.maintenance_request_id is None


def test_phase_c_scenario_6_borrowed_recovered_damaged_and_formal_return(app_env):
    """Scenario 6: Borrowed asset recovered as DAMAGED routes to MAINTENANCE upon formal return."""
    c = app_env
    with Session(c.engine) as db:
        asset = db.get(EquipmentAsset, c.asset.id)
        asset.availability_status = "BORROWED"
        checkout = EquipmentCheckout(
            id=uuid4(), asset_id=c.asset.id, student_id=c.student.id, issued_by=c.staff.id,
            checked_out_at=datetime.now(timezone.utc) - timedelta(days=1),
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
            status="BORROWED"
        )
        db.add(checkout)
        db.commit()
        checkout_id = checkout.id

    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing"),
            c.staff
        )

    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="DAMAGED", reason="Damaged cable"),
            c.staff
        )
        assert rec.asset_availability_status == "BORROWED"
        assert rec.asset_condition_status == "DAMAGED"
        assert rec.asset_maintenance_hold is True

    with Session(c.engine) as db:
        ret = record_return(
            db, checkout_id, c.staff,
            ReturnCreate(condition_status="DAMAGED", notes="Formal return of damaged unit")
        )
        assert ret.status == "RETURNED"
        assert ret.availability_status == "MAINTENANCE"
        assert ret.condition_status == "DAMAGED"
        assert ret.maintenance_hold is True
        assert ret.maintenance_request_id is not None


def test_phase_c_scenario_7_recovery_while_maintenance_active(app_env):
    """Scenario 7: Recovery while independent maintenance is active preserves maintenance_hold."""
    c = app_env
    with Session(c.engine) as db:
        asset = db.get(EquipmentAsset, c.asset.id)
        asset.availability_status = "BORROWED"
        asset.maintenance_hold = True
        checkout = EquipmentCheckout(
            id=uuid4(), asset_id=c.asset.id, student_id=c.student.id, issued_by=c.staff.id,
            checked_out_at=datetime.now(timezone.utc) - timedelta(days=1),
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
            status="BORROWED"
        )
        req_id = uuid4()
        m_req = MaintenanceRequest(
            id=req_id, asset_id=c.asset.id, issue_type="DAMAGE",
            issue_description="Independent active maintenance request",
            status="OPEN", reported_by=c.staff.id, version=1
        )
        from backend.app.models.maintenance import MaintenanceEvent
        m_ev = MaintenanceEvent(
            id=uuid4(), request_id=req_id, sequence=1, event_type="REPORTED",
            actor_id=c.staff.id, occurred_at=datetime.now(timezone.utc),
            note="Initial damage report", to_status="OPEN",
            from_condition=None, to_condition="DAMAGED",
            from_availability=None, to_availability="MAINTENANCE",
            from_maintenance_hold=None, to_maintenance_hold=True
        )
        db.add_all([checkout, m_req])
        db.flush()
        db.add(m_ev)
        db.commit()

    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Missing during repair hold"),
            c.staff
        )

    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Recovered unit"),
            c.staff
        )
        assert rec.asset_availability_status == "BORROWED"
        assert rec.asset_condition_status == "WORKING"
        assert rec.asset_maintenance_hold is True  # Hold preserved due to active m_req!


def test_phase_c_scenario_8_reserved_asset_missing_and_recovery(app_env):
    """Scenario 8: RESERVED asset missing and recovery transitions."""
    c = app_env
    with Session(c.engine) as db:
        asset = db.get(EquipmentAsset, c.asset.id)
        asset.availability_status = "RESERVED"
        asset.condition_status = "WORKING"
        asset.maintenance_hold = False
        db.commit()

    # Report missing
    with Session(c.engine) as db:
        rep = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Reserved item missing"),
            c.staff
        )
        assert rep.asset_availability_status == "RESERVED"
        assert rep.asset_condition_status == "MISSING"
        assert rep.asset_maintenance_hold is True

    # Reconcile working -> restores to RESERVED / WORKING / hold=False
    with Session(c.engine) as db:
        rec = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Found in staging"),
            c.staff
        )
        assert rec.asset_availability_status == "RESERVED"
        assert rec.asset_condition_status == "WORKING"
        assert rec.asset_maintenance_hold is False


def test_phase_c_scenario_9_concurrent_reports_by_different_staff(app_env):
    """Scenario 9: Concurrent reports by different staff; one succeeds, other fails 409."""
    c = app_env
    barrier = None
    results = []

    def attempt_report(staff_user):
        try:
            with Session(c.engine) as db:
                res = report_asset_missing(
                    db, c.asset.id,
                    AssetMissingReportCreate(idempotency_key=uuid4(), reason=f"Report by {staff_user.college_id}"),
                    staff_user
                )
                results.append(("SUCCESS", res.status))
        except Exception as exc:
            results.append(("ERROR", getattr(exc, "status_code", str(exc))))

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_report, c.staff)
        f2 = executor.submit(attempt_report, c.staff2)
        f1.result()
        f2.result()

    successes = [r for r in results if r[0] == "SUCCESS"]
    errors = [r for r in results if r[0] == "ERROR"]
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0][1] == 409


def test_phase_c_scenario_10_duplicate_idempotency_key_replay(app_env):
    """Scenario 10: Duplicate idempotency-key replays return existing records without duplicate events."""
    c = app_env
    key_rep = uuid4()
    with Session(c.engine) as db:
        r1 = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key_rep, reason="Missing test"),
            c.staff
        )
        r2 = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key_rep, reason="Missing test"),
            c.staff
        )
        assert r1.id == r2.id
        events = list_incident_events(db, c.asset.id, r1.id, c.staff)
        assert len(events) == 1

    key_rec = uuid4()
    with Session(c.engine) as db:
        rc1 = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=key_rec, recovered_condition="WORKING", reason="Recovered"),
            c.staff
        )
        rc2 = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=key_rec, recovered_condition="WORKING", reason="Recovered"),
            c.staff
        )
        assert rc1.id == rc2.id
        events = list_incident_events(db, c.asset.id, r1.id, c.staff)
        assert len(events) == 2

    # Replay report key after reconciliation
    with Session(c.engine) as db:
        replay_rep = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key_rep, reason="Missing test"),
            c.staff
        )
        assert replay_rep.id == r1.id
        assert replay_rep.status == "RECONCILED"
        assert replay_rep.asset_availability_status == "AVAILABLE"
        events = list_incident_events(db, c.asset.id, r1.id, c.staff)
        assert len(events) == 2


def test_phase_c_scenario_11_repeated_missing_recovery_cycles(app_env):
    """Scenario 11: Repeated missing/recovery cycles create distinct immutable incidents."""
    c = app_env
    key1, key2 = uuid4(), uuid4()
    key3, key4 = uuid4(), uuid4()

    # Cycle 1
    with Session(c.engine) as db:
        inc1 = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key1, reason="First disappearance"),
            c.staff
        )
        rec1 = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=key2, recovered_condition="WORKING", reason="First recovery"),
            c.staff
        )

    # Cycle 2
    with Session(c.engine) as db:
        inc2 = report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=key3, reason="Second disappearance"),
            c.staff
        )
        rec2 = reconcile_missing_asset(
            db, c.asset.id,
            AssetMissingReconcileCreate(idempotency_key=key4, recovered_condition="WORKING", reason="Second recovery"),
            c.staff
        )

    assert inc1.id != inc2.id
    with Session(c.engine) as db:
        all_incidents = list_missing_incidents(db, c.asset.id, c.staff)
        assert len(all_incidents) == 2
        assert all(i.status == "RECONCILED" for i in all_incidents)


def test_phase_c_scenario_12_transaction_rollback_after_injected_failure(app_env):
    """Scenario 12: Injected failure rolls back state, writing zero incidents or events."""
    c = app_env
    key = uuid4()
    from sqlalchemy import event

    def fail(connection, cursor, statement, parameters, context, many):
        if "missing_equipment_events" in statement:
            raise RuntimeError("Injected failure during audit event persistence")

    event.listen(c.engine, "before_cursor_execute", fail)
    try:
        with Session(c.engine) as db:
            with pytest.raises(RuntimeError):
                report_asset_missing(
                    db, c.asset.id,
                    AssetMissingReportCreate(idempotency_key=key, reason="Will roll back"),
                    c.staff
                )
    finally:
        event.remove(c.engine, "before_cursor_execute", fail)

    with Session(c.engine) as db:
        reloaded = db.get(EquipmentAsset, c.asset.id)
        assert reloaded.availability_status == "AVAILABLE"
        assert reloaded.condition_status == "WORKING"
        incidents = db.scalars(select(MissingEquipmentIncident).where(MissingEquipmentIncident.asset_id == c.asset.id)).all()
        assert len(incidents) == 0


def test_phase_c_scenario_13_unauthorized_student_access(app_env):
    """Scenario 13: Student role cannot report or reconcile missing equipment."""
    c = app_env
    with pytest.raises(Exception) as exc:
        with Session(c.engine) as db:
            report_asset_missing(
                db, c.asset.id,
                AssetMissingReportCreate(idempotency_key=uuid4(), reason="Student reporting"),
                c.student
            )
    assert getattr(exc.value, "status_code", None) == 403

    with pytest.raises(Exception) as exc:
        with Session(c.engine) as db:
            reconcile_missing_asset(
                db, c.asset.id,
                AssetMissingReconcileCreate(idempotency_key=uuid4(), recovered_condition="WORKING", reason="Student reconciling"),
                c.student
            )
    assert getattr(exc.value, "status_code", None) == 403


def test_phase_c_scenario_14_immutable_audit_events_triggers(app_env):
    """Scenario 14: Runtime role attempts to UPDATE, DELETE, or TRUNCATE are blocked by triggers."""
    c = app_env
    # Create an event first
    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Audit protection test"),
            c.staff
        )

    # Directly execute forbidden operations as inventory_app
    statements = [
        "UPDATE missing_equipment_events SET reason = 'Tampered'",
        "DELETE FROM missing_equipment_events",
        "TRUNCATE missing_equipment_events",
        "DELETE FROM missing_equipment_incidents",
        "TRUNCATE missing_equipment_incidents",
    ]
    for stmt in statements:
        with pytest.raises(DBAPIError):
            with c.engine.begin() as conn:
                conn.exec_driver_sql(stmt)


def test_phase_c_scenario_15_partial_unique_index_rejects_multiple_open(app_env):
    """Scenario 15: Partial unique index rejects multiple OPEN incidents on same asset."""
    c = app_env
    with Session(c.engine) as db:
        report_asset_missing(
            db, c.asset.id,
            AssetMissingReportCreate(idempotency_key=uuid4(), reason="Legitimate open incident"),
            c.staff
        )

    # Attempt direct SQL INSERT of a second OPEN incident for the same asset
    with pytest.raises(IntegrityError):
        with c.engine.begin() as conn:
            conn.exec_driver_sql(f"""
                INSERT INTO missing_equipment_incidents (
                    id, asset_id, operation_key, report_fingerprint, status, version,
                    reported_by, report_reason, previous_availability_status, previous_condition_status
                ) VALUES (
                    '{uuid4()}', '{c.asset.id}', '{uuid4()}', 'fake_fp', 'OPEN', 1,
                    '{c.staff.id}', 'Illegal second open', 'AVAILABLE', 'WORKING'
                )
            """)
