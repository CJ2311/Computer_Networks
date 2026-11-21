"""F1 regression tests. Run only through the opt-in disposable PG18 harness."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.alembic.application_safety import apply_application, verify_application
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.missing_equipment import MissingEquipmentIncident, MissingEquipmentEvent
from backend.app.models.user import User
from backend.app.schemas.checkout import ReturnCreate
from backend.app.schemas.missing_equipment import AssetMissingReportCreate, AssetMissingReconcileCreate
from backend.app.services.checkout import create_checkout, record_return
from backend.app.services.missing_equipment import report_asset_missing, reconcile_missing_asset
from infra.nfc_postgres.tests.test_operations import application
from infra.nfc_postgres.harness import ADMIN
from scripts import deploy_nfc_demo as baseline


def issue(case, notes="Original custody notes"):
    with Session(case.engine) as db:
        return create_checkout(db, case.asset.id, case.student.id, case.staff,
                               datetime.now(timezone.utc) + timedelta(days=1), notes=notes)


def report(case):
    with Session(case.engine) as db:
        return report_asset_missing(db, case.asset.id, AssetMissingReportCreate(
            idempotency_key=uuid4(), reason="Original missing report", police_report_number="REPORT-1"
        ), case.staff)


def reconcile(case):
    with Session(case.engine) as db:
        return reconcile_missing_asset(db, case.asset.id, AssetMissingReconcileCreate(
            idempotency_key=uuid4(), reason="Recovered by staff", recovered_condition="WORKING"
        ), case.staff)


@pytest.mark.parametrize("notes", [None, "", "Original notes\n[RETURN] text already present", "Unicode notes — preserved"])
@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED", "UNDER_REPAIR"])
def test_legitimate_return_preserves_original_history(application, notes, condition):
    case = application
    loan = issue(case, notes)
    with Session(case.engine) as db:
        result = record_return(db, loan.id, case.staff, ReturnCreate(condition_status=condition, notes="Received"))
    assert result.status == "RETURNED" and result.returned_to == case.staff.id
    assert result.notes.startswith("[RETURN] " if notes is None else notes + "\n\n[RETURN] ")
    assert result.condition_status == condition
    with Session(case.engine) as db:
        row = db.get(EquipmentCheckout, loan.id)
        assert (row.asset_id, row.student_id, row.issued_by, row.checked_out_at, row.due_at) == (
            loan.asset_id, loan.student_id, loan.issued_by, loan.checked_out_at, loan.due_at
        )


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("field,expression", [
    ("id", "gen_random_uuid()"), ("asset_id", "gen_random_uuid()"),
    ("student_id", "gen_random_uuid()"), ("issued_by", "gen_random_uuid()"),
    ("checked_out_at", "checked_out_at - interval '1 day'"),
    ("due_at", "due_at + interval '1 hour'"),
    ("equipment_name_at_checkout", "'Rewritten name'"),
    ("asset_code_at_checkout", "'Rewritten code'"),
    ("identifier_snapshot_status", "'LEGACY_UNKNOWN'"),
])
def test_runtime_cannot_rewrite_checkout_identity(application, closed, field, expression):
    case = application
    loan = issue(case)
    if closed:
        with Session(case.engine) as db:
            record_return(db, loan.id, case.staff, ReturnCreate(condition_status="WORKING"))
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text(f"UPDATE equipment_checkouts SET {field}={expression} WHERE id=:id"), {"id": loan.id})
    assert error.value.orig.sqlstate == "42501"
    with Session(case.engine) as db:
        row = db.get(EquipmentCheckout, loan.id)
        assert row.student_id == loan.student_id and row.due_at == loan.due_at


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("field,expression", [
    ("id", "gen_random_uuid()"), ("asset_id", "gen_random_uuid()"),
    ("operation_key", "gen_random_uuid()"), ("report_fingerprint", "repeat('a',64)"),
    ("reported_by", "gen_random_uuid()"), ("reported_at", "reported_at - interval '1 day'"),
    ("report_reason", "'Rewritten reason'"), ("police_report_number", "NULL"),
    ("previous_availability_status", "'RETIRED'"), ("previous_condition_status", "'DAMAGED'"),
])
def test_runtime_cannot_rewrite_missing_report(application, closed, field, expression):
    case = application
    incident = report(case)
    if closed:
        reconcile(case)
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text(f"UPDATE missing_equipment_incidents SET {field}={expression} WHERE id=:id"), {"id": incident.id})
    assert error.value.orig.sqlstate == "42501"
    with Session(case.engine) as db:
        assert db.get(MissingEquipmentIncident, incident.id).report_reason == incident.report_reason


def test_reconciliation_preserves_report_and_freezes_terminal_fields(application):
    case = application
    original = report(case)
    result = reconcile(case)
    assert result.status == "RECONCILED" and result.version == original.version + 1
    assert result.report_reason == original.report_reason and result.reported_by == original.reported_by
    assert result.police_report_number == original.police_report_number
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text("UPDATE missing_equipment_incidents SET reconcile_reason='Rewritten' WHERE id=:id"), {"id": original.id})
    assert error.value.orig.sqlstate == "42501"
    with Session(case.engine) as db:
        events = db.scalars(select(MissingEquipmentEvent).where(MissingEquipmentEvent.incident_id == original.id)
                            .order_by(MissingEquipmentEvent.sequence)).all()
        assert [event.reason for event in events] == ["Original missing report", "Recovered by staff"]


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED"])
def test_reconciliation_roles_conditions_and_idempotent_retry(application, role, condition):
    case = application
    with case.admin.begin() as connection:
        connection.execute(text("UPDATE users SET role=:role WHERE id=:id"), {"role": role, "id": case.staff.id})
    original = report(case)
    data = AssetMissingReconcileCreate(idempotency_key=uuid4(), reason="Recovered", recovered_condition=condition)
    for _ in range(2):
        with Session(case.engine) as db:
            result = reconcile_missing_asset(db, case.asset.id, data, case.staff)
        assert result.version == original.version + 1
        assert result.recovered_condition == condition
    with Session(case.engine) as db:
        assert len(db.scalars(select(MissingEquipmentEvent).where(
            MissingEquipmentEvent.incident_id == original.id)).all()) == 2


@pytest.mark.parametrize("field,expression", [
    ("version", "version+1"), ("status", "'OPEN'"),
    ("reconciled_by", "gen_random_uuid()"), ("reconciled_at", "reconciled_at+interval '1 second'"),
    ("reconcile_reason", "'Changed'"), ("recovered_condition", "'DAMAGED'"),
    ("resolved_availability_status", "'RETIRED'"), ("resolved_condition_status", "'DAMAGED'"),
    ("reconcile_key", "gen_random_uuid()"), ("reconcile_fingerprint", "repeat('b',64)"),
])
def test_terminal_reconciliation_fields_are_immutable(application, field, expression):
    case = application
    original = report(case)
    reconcile(case)
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text(f"UPDATE missing_equipment_incidents SET {field}={expression} WHERE id=:id"),
                               {"id": original.id})
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("invalid_version,student_actor", [(True, False), (False, True)])
def test_direct_reconciliation_rejects_wrong_version_and_student_actor(application, invalid_version, student_actor):
    case = application
    original = report(case)
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text("""UPDATE missing_equipment_incidents SET status='RECONCILED',
                version=:version, reconciled_by=:actor, reconciled_at=clock_timestamp(),
                reconcile_reason='Recovered', recovered_condition='WORKING',
                resolved_availability_status='AVAILABLE', resolved_condition_status='WORKING',
                reconcile_key=:key, reconcile_fingerprint=:fingerprint WHERE id=:id"""),
                {"id": original.id, "version": 10 if invalid_version else original.version + 1,
                 "actor": case.student.id if student_actor else case.staff.id,
                 "key": uuid4(), "fingerprint": "c" * 64})
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("closed", [False, True])
def test_notes_cannot_be_edited_outside_the_return_transition(application, closed):
    case = application
    loan = issue(case)
    if closed:
        with Session(case.engine) as db:
            record_return(db, loan.id, case.staff, ReturnCreate(condition_status="WORKING"))
    with pytest.raises(DBAPIError) as error:
        with case.engine.begin() as connection:
            connection.execute(text("UPDATE equipment_checkouts SET notes='Rewritten' WHERE id=:id"), {"id": loan.id})
    assert error.value.orig.sqlstate == "42501"


def test_direct_return_cannot_replace_original_notes_or_use_a_student_receiver(application):
    case = application
    loan = issue(case)
    for receiver, notes in [(case.staff.id, "[RETURN] replaced original"), (case.student.id, loan.notes + "\n\n[RETURN] invalid receiver")]:
        with pytest.raises(DBAPIError) as error:
            with case.engine.begin() as connection:
                connection.execute(text("""UPDATE equipment_checkouts SET status='RETURNED',
                    returned_at=clock_timestamp(), returned_to=:receiver, notes=:notes WHERE id=:id"""),
                    {"receiver": receiver, "notes": notes, "id": loan.id})
        assert error.value.orig.sqlstate == "42501"


def test_terminal_checkout_cannot_be_reopened_or_have_return_details_changed(application):
    case = application
    loan = issue(case)
    with Session(case.engine) as db:
        record_return(db, loan.id, case.staff, ReturnCreate(condition_status="WORKING"))
    for change in ["status='BORROWED', returned_at=NULL, returned_to=NULL", "returned_at=returned_at + interval '1 second'"]:
        with pytest.raises(DBAPIError) as error:
            with case.engine.begin() as connection:
                connection.execute(text(f"UPDATE equipment_checkouts SET {change} WHERE id=:id"), {"id": loan.id})
        assert error.value.orig.sqlstate == "42501"


def test_existing_audit_events_and_runtime_privileges_remain_protected(application):
    case = application
    report(case)
    with case.engine.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "inventory_app"
        for table in ("equipment_checkouts", "missing_equipment_incidents"):
            assert connection.scalar(text("SELECT has_table_privilege(current_user,:table,'UPDATE')"), {"table": table})
            assert not connection.scalar(text("SELECT has_table_privilege(current_user,:table,'DELETE,TRUNCATE,TRIGGER')"), {"table": table})
        for function in ("checkout_history_guard()", "missing_incident_history_guard()"):
            assert not connection.scalar(text("SELECT has_function_privilege(current_user,:function,'EXECUTE')"), {"function": function})
    for statement in ("UPDATE missing_equipment_events SET reason='Rewritten'",
                      "DELETE FROM missing_equipment_events", "TRUNCATE missing_equipment_events"):
        with pytest.raises(DBAPIError):
            with case.engine.begin() as connection:
                connection.exec_driver_sql(statement)


@pytest.mark.parametrize("tamper", [
    "GRANT DELETE ON equipment_checkouts TO inventory_app",
    "GRANT TRUNCATE ON equipment_checkouts TO inventory_app",
    "GRANT TRIGGER ON equipment_checkouts TO inventory_app",
    "ALTER TABLE equipment_checkouts DISABLE TRIGGER checkout_history_guard",
    "ALTER TABLE missing_equipment_incidents DISABLE TRIGGER missing_incident_history_guard",
    """CREATE OR REPLACE FUNCTION checkout_history_guard() RETURNS trigger LANGUAGE plpgsql
       SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $$BEGIN RETURN NEW; END;$$""",
])
def test_deployment_verifier_rejects_disabled_or_replaced_protection(application, tamper):
    case = application
    with case.admin.begin() as connection:
        connection.exec_driver_sql(tamper)
    with case.admin.connect() as connection:
        with pytest.raises(RuntimeError):
            verify_application(connection, case.h.settings)


def test_verify_only_deployment_preserves_existing_history(application):
    case = application
    loan = issue(case)
    with case.admin.connect() as connection:
        apply_application(connection, case.h.settings)
    with Session(case.engine) as db:
        assert db.get(EquipmentCheckout, loan.id).notes == loan.notes


def test_upgrade_preserves_preexisting_loans_and_incidents(database):
    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as connection:
        database.verify_engine_identity(connection)
        connection.rollback()
        config = Config("alembic_nfc_application.ini")
        config.attributes.update(connection=connection,
                                 nfc_demo_identity=baseline.identity(database.settings, ADMIN))
        command.upgrade(config, "c7d3e91024ab")
        connection.commit()
    staff = User(id=uuid4(), college_id="OLD-F", full_name="Faculty",
                 email="old-faculty@college.invalid", role="FACULTY", is_active=True)
    student = User(id=uuid4(), college_id="OLD-S", full_name="Student",
                   email="old-student@college.invalid", role="STUDENT", is_active=True)
    equipment = Equipment(id=uuid4(), name="Existing equipment", category="Lab", tracking_type="INDIVIDUAL", default_location="Lab")
    asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id, asset_code="OLD-1", location="Lab")
    with Session(admin, expire_on_commit=False) as db:
        db.add_all([staff, student, equipment])
        db.commit()
        db.add(asset)
        db.commit()
    runtime = database.engine("inventory_app")
    with Session(runtime) as db:
        loan = create_checkout(db, asset.id, student.id, staff,
                               datetime.now(timezone.utc) + timedelta(days=1), notes="Before migration")
    with Session(runtime) as db:
        incident = report_asset_missing(db, asset.id, AssetMissingReportCreate(
            idempotency_key=uuid4(), reason="Before migration"), staff)
    with admin.connect() as connection:
        apply_application(connection, database.settings)
    with Session(runtime) as db:
        assert db.get(EquipmentCheckout, loan.id).notes == "Before migration"
        assert db.get(MissingEquipmentIncident, incident.id).report_reason == "Before migration"
    with Session(runtime) as db:
        assert reconcile_missing_asset(db, asset.id, AssetMissingReconcileCreate(
            idempotency_key=uuid4(), reason="Recovered", recovered_condition="WORKING"), staff).status == "RECONCILED"
    with Session(runtime) as db:
        assert record_return(db, loan.id, staff, ReturnCreate(condition_status="WORKING")).status == "RETURNED"
