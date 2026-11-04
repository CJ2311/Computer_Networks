from backend.tests.inventory_session_stub import configure_session
"""Database-free Phase 3 policy, lock and transaction regression tests."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services import checkout as service, maintenance
from backend.app.services.inventory_transactions import flush_stage, transaction


@pytest.fixture
def flow(monkeypatch):
    c = SimpleNamespace(now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc), request=None,
                        parent=None, events=[], writes=[], stages=[])
    c.staff = User(id=uuid4(), role="FACULTY", is_active=True)
    c.student = User(id=uuid4(), role="STUDENT", is_active=True)
    c.asset = EquipmentAsset(id=uuid4(), availability_status="BORROWED", condition_status="WORKING", maintenance_hold=False)
    c.loan = EquipmentCheckout(id=uuid4(), asset_id=c.asset.id, student_id=c.student.id,
                               issued_by=c.staff.id, checked_out_at=c.now - timedelta(days=1),
                               due_at=c.now + timedelta(days=1), notes="Original history", status="BORROWED")
    c.db = Mock(new=set(), dirty=set(), deleted=set(), info={})
    c.db.no_autoflush = nullcontext()
    c.db.in_nested_transaction.return_value = False
    configure_session(c.db, monkeypatch)
    c.db.get.side_effect = lambda model, identifier, **kw: next((u for u in (c.staff, c.student) if u.id == identifier), None)

    def scalar(statement):
        sql, params = str(statement), statement.compile().params
        if "clock_timestamp()" in sql:
            return c.now
        entity = statement.column_descriptions[0].get("entity")
        if entity is Equipment:
            return Equipment(id=c.asset.equipment_id, name="Sensor")
        if entity is EquipmentAsset:
            return c.asset
        if entity is EquipmentCheckout:
            if len(list(statement.selected_columns)) == 1:
                return c.asset.id
            return c.loan if c.asset.availability_status == "BORROWED" else None
        if entity is MaintenanceRequest:
            if "recovery_of_request_id =" in sql:
                return None
            if "status IN" in sql:
                return c.request
            if "status =" in sql:
                return c.parent.id if c.parent else None
            return c.parent if c.parent and c.parent.id in params.values() else None
        raise AssertionError(sql)
    c.db.scalar.side_effect = scalar

    def add(obj):
        c.writes.append(obj)
        if isinstance(obj, MaintenanceRequest):
            c.request = obj
        if isinstance(obj, MaintenanceEvent):
            c.events.append(obj)
        if isinstance(obj, EquipmentCheckout):
            obj.id = uuid4()
    c.db.add.side_effect = add
    c.db.flush.side_effect = lambda: c.stages.append((c.loan.status, c.asset.maintenance_hold, len(c.events)))
    return c


def returning(c, condition="WORKING", **kwargs):
    return service.record_return(c.db, c.loan.id, c.staff, ReturnCreate(condition_status=condition, **kwargs))


def reported(c, status="OPEN"):
    c.asset.maintenance_hold = True
    c.request = MaintenanceRequest(id=uuid4(), asset_id=c.asset.id, issue_type="DAMAGE",
                                   issue_description="Keep original report", status=status,
                                   reported_by=uuid4(), reported_at=c.now - timedelta(hours=1), version=3,
                                   assigned_to=uuid4())
    return c.request


@pytest.mark.parametrize("condition, issue", [("DAMAGED", "DAMAGE"), ("UNDER_REPAIR", "MALFUNCTION")])
def test_return_creates_open_request_and_context_without_starting_repair(flow, condition, issue):
    c = flow
    result = returning(c, condition, notes="Observed fault")
    assert result.maintenance_hold and result.availability_status == "MAINTENANCE"
    assert c.request.issue_type == issue and c.request.status == "OPEN" and c.request.version == 1
    assert c.request.reported_at == c.loan.returned_at == c.now
    assert c.request.started_at is None
    event, = c.events
    assert event.event_type == "REPORTED" and event.sequence == 1
    assert json.loads(event.note.split("\n", 1)[0])["checkout_id"] == str(c.loan.id)
    assert event.from_availability == "BORROWED" and event.to_availability == "MAINTENANCE"
    assert c.stages == [("RETURNED", False, 0), ("RETURNED", True, 0),
                        ("RETURNED", True, 0), ("RETURNED", True, 1)]
    c.db.commit.assert_called_once()


@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED", "UNDER_REPAIR"])
def test_return_reuses_open_report_without_changing_original_information(flow, condition):
    c = flow
    request = reported(c)
    original = (request.id, request.issue_description, request.reported_by, request.reported_at, request.assigned_to)
    result = returning(c, condition)
    assert result.maintenance_request_id == request.id and result.maintenance_hold
    assert result.availability_status == "MAINTENANCE"
    assert request.version == 4 and request.status == "OPEN"
    assert original == (request.id, request.issue_description, request.reported_by, request.reported_at, request.assigned_to)
    assert [(e.event_type, e.sequence) for e in c.events] == [("RETURN_OBSERVED", 4)]


@pytest.mark.parametrize("state", ["IN_PROGRESS", "AWAITING_VERIFICATION"])
def test_advanced_maintenance_with_loan_rejected_without_writes(flow, state):
    reported(flow, state)
    with pytest.raises(HTTPException) as error:
        returning(flow, "DAMAGED")
    assert error.value.status_code == 409
    assert flow.loan.status == "BORROWED" and flow.events == []
    flow.db.flush.assert_not_called()


@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED"])
def test_cancelled_hold_requires_explicit_linked_inspection(flow, condition):
    c = flow
    c.parent = reported(c)
    c.parent.status, c.request = "CANCELLED", None
    with pytest.raises(HTTPException) as error:
        returning(c, condition)
    assert error.value.status_code == 409 and c.loan.status == "BORROWED"
    c.db.flush.assert_not_called()
    result = returning(c, condition, recovery_of_request_id=c.parent.id)
    assert result.maintenance_hold and c.request.issue_type == "INSPECTION"
    assert c.request.recovery_of_request_id == c.parent.id and c.request.status == "OPEN"


@pytest.mark.parametrize("problem", ["wrong_asset", "not_cancelled", "no_hold", "missing", "already_replaced"])
def test_invalid_recovery_is_rejected_before_return(flow, problem):
    c = flow
    c.parent = reported(c)
    c.parent.status, c.request = "CANCELLED", None
    parent_id = c.parent.id
    if problem == "wrong_asset":
        c.parent.asset_id = uuid4()
    elif problem == "not_cancelled":
        c.parent.status = "COMPLETED"
    elif problem == "no_hold":
        c.asset.maintenance_hold = False
    elif problem == "missing":
        c.parent = None
    else:
        original = c.db.scalar.side_effect
        c.db.scalar.side_effect = lambda stmt: uuid4() if "recovery_of_request_id =" in str(stmt) else original(stmt)
    with pytest.raises(HTTPException) as error:
        returning(c, recovery_of_request_id=parent_id)
    assert error.value.status_code == (404 if problem == "missing" else 409)
    c.db.flush.assert_not_called()
    assert c.loan.status == "BORROWED"


@pytest.mark.parametrize("hold, condition", [(True, "WORKING"), (False, "DAMAGED"), (False, "UNDER_REPAIR")])
def test_working_return_does_not_release_unexplained_hold_or_prior_damage(flow, hold, condition):
    c = flow
    c.asset.condition_status, c.asset.maintenance_hold = condition, hold
    result = returning(c)
    assert result.availability_status == "MAINTENANCE" and result.maintenance_hold
    assert c.request.issue_type == "INSPECTION" and c.request.status == "OPEN"


@pytest.mark.parametrize("operation", ["checkout", "return"])
@pytest.mark.parametrize("role, active", [("STUDENT", True), ("FACULTY", False)])
def test_forged_staff_attributes_do_not_override_persisted_authorization(flow, operation, role, active):
    c = flow
    forged = User(id=c.staff.id, role="FACULTY", is_active=True)
    c.staff.role, c.staff.is_active = role, active
    with pytest.raises(HTTPException) as error:
        if operation == "return":
            service.record_return(c.db, c.loan.id, forged, ReturnCreate(condition_status="WORKING"))
        else:
            service.create_checkout(c.db, c.asset.id, c.student.id, forged, c.loan.due_at)
    assert error.value.status_code == 403
    c.db.flush.assert_not_called()


@pytest.mark.parametrize("held, active", [(True, False), (False, True)])
def test_checkout_explicitly_checks_hold_and_active_maintenance(flow, held, active):
    c = flow
    c.asset.availability_status = "AVAILABLE"
    if active:
        reported(c)
    c.asset.maintenance_hold = held
    with pytest.raises(HTTPException) as error:
        service.create_checkout(c.db, c.asset.id, c.student.id, c.staff, c.loan.due_at)
    assert error.value.status_code == 409
    c.db.flush.assert_not_called()


@pytest.mark.parametrize("during_flush", [False, True])
def test_checkout_deadline_uses_database_clock_after_wait_and_before_commit(flow, during_flush):
    c = flow
    c.asset.availability_status = "AVAILABLE"
    due = c.now + timedelta(seconds=1)
    if during_flush:
        c.db.flush.side_effect = lambda: setattr(c, "now", due)
    else:
        c.now = due
    with pytest.raises(HTTPException) as error:
        service.create_checkout(c.db, c.asset.id, c.student.id, c.staff, due)
    assert error.value.status_code == 400
    c.db.commit.assert_not_called()
    c.db.rollback.assert_called_once()


@pytest.mark.parametrize("stage", [1, 2, 3, 4, "commit", "response"])
def test_any_integrated_return_failure_rolls_back(flow, monkeypatch, stage):
    c = flow
    if type(stage) is int:
        count = 0
        def flush():
            nonlocal count
            count += 1
            if count == stage:
                raise RuntimeError("Injected stage failure")
        c.db.flush.side_effect = flush
    elif stage == "commit":
        c.db.commit.side_effect = RuntimeError("Injected commit failure")
    else:
        monkeypatch.setattr(service, "ReturnResponse", Mock(side_effect=ValueError("Invalid response")))
    with pytest.raises((RuntimeError, ValueError)):
        returning(c, "DAMAGED")
    c.db.rollback.assert_called_once()
    if stage != "commit":
        c.db.commit.assert_not_called()
    assert not c.db.info


@pytest.mark.parametrize("sqlstate", ["40001", "40P01", "55P03", "XX000"])
def test_database_failures_are_sanitized_and_rolled_back(flow, sqlstate):
    failure = Exception("secret SQL/credentials")
    failure.sqlstate = sqlstate
    flow.db.flush.side_effect = OperationalError("private statement", {}, failure)
    with pytest.raises(HTTPException) as error:
        returning(flow, "DAMAGED")
    assert error.value.status_code == (503 if sqlstate == "XX000" else 409)
    assert "secret" not in error.value.detail and "private" not in error.value.detail
    flow.db.rollback.assert_called_once()


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_shared_owner_rejects_caller_work_without_discarding_it(flow, pending):
    setattr(flow.db, pending, {object()})
    with pytest.raises(HTTPException):
        returning(flow)
    flow.db.flush.assert_not_called()
    flow.db.rollback.assert_not_called()


@pytest.mark.parametrize("after_stage", [1, 2, 3, 4])
def test_unrelated_work_injected_between_return_stages_is_never_flushed(flow, after_stage):
    c = flow
    count = 0
    def flush():
        nonlocal count
        count += 1
        if count == after_stage:
            c.db.new = {object()}
    c.db.flush.side_effect = flush
    with pytest.raises(HTTPException) as error:
        returning(c, "DAMAGED")
    assert error.value.status_code == 409 and c.db.flush.call_count == after_stage
    c.db.rollback.assert_called_once()
    c.db.commit.assert_not_called()


def test_owner_rejects_nested_standalone_maintenance_commit(flow):
    @transaction
    def outer(db):
        return maintenance.report_maintenance_issue(db, flow.asset.id, flow.staff, "DAMAGE", "Fault")
    with pytest.raises(HTTPException) as error:
        outer(flow.db)
    assert error.value.status_code == 409
    flow.db.commit.assert_not_called()
    flow.db.rollback.assert_called_once()


def test_guard_works_with_real_unbound_session_without_opening_database():
    with Session() as db:
        unrelated = User(role="STUDENT")
        db.add(unrelated)
        with pytest.raises(HTTPException):
            flush_stage(db, EquipmentAsset())
        assert unrelated in db.new


def test_return_locks_refreshed_asset_checkout_request_in_order(flow):
    reported(flow)
    returning(flow)
    statements = [call.args[0] for call in flow.db.scalar.call_args_list if call.args[0]._for_update_arg is not None]
    assert [stmt.column_descriptions[0]["entity"] for stmt in statements] == [EquipmentAsset, EquipmentCheckout, MaintenanceRequest]
    assert all(stmt.get_execution_options()["populate_existing"] for stmt in statements)


def test_recovery_id_input_validation():
    with pytest.raises(ValidationError):
        ReturnCreate(condition_status="WORKING", recovery_of_request_id="invalid")


def test_maximum_return_notes_fit_audit_limit_without_losing_content(flow):
    notes = "\x01" * 1000  # Would expand to 6000 characters inside JSON.
    returning(flow, "DAMAGED", notes=notes)
    event, = flow.events
    assert len(event.note) <= 5000 and event.note.endswith(notes)
    assert notes in flow.request.issue_description


def test_return_rechecks_planned_maintenance_version_before_persistence(flow):
    reported(flow)
    flow.db.flush.side_effect = lambda: setattr(flow.request, "version", flow.request.version + 1)
    with pytest.raises(HTTPException) as error:
        returning(flow, "DAMAGED")
    assert error.value.status_code == 409 and "version" in error.value.detail
    assert flow.db.flush.call_count == 1
    flow.db.rollback.assert_called_once()
    flow.db.commit.assert_not_called()


@pytest.mark.parametrize("operation", ["checkout", "return"])
def test_staff_role_is_rechecked_after_locks(flow, operation):
    c = flow
    c.asset.availability_status = "AVAILABLE" if operation == "checkout" else "BORROWED"
    original = c.db.scalar.side_effect
    def revoke_after_lock(stmt):
        result = original(stmt)
        if stmt._for_update_arg is not None and stmt.column_descriptions[0].get("entity") is EquipmentAsset:
            c.staff.is_active = False
        return result
    c.db.scalar.side_effect = revoke_after_lock
    with pytest.raises(HTTPException) as error:
        if operation == "checkout":
            service.create_checkout(c.db, c.asset.id, c.student.id, c.staff, c.loan.due_at)
        else:
            returning(c, "DAMAGED")
    assert error.value.status_code == 403
    c.db.flush.assert_not_called()


@pytest.mark.parametrize("stage", [1, 2, "response", "commit"])
def test_checkout_failure_rolls_back_before_response_is_returned(flow, monkeypatch, stage):
    c = flow
    c.asset.availability_status = "AVAILABLE"
    if type(stage) is int:
        count = 0
        def flush():
            nonlocal count
            count += 1
            if count == stage:
                raise RuntimeError("Checkout stage failed")
        c.db.flush.side_effect = flush
    elif stage == "response":
        monkeypatch.setattr(service, "CheckoutResponse", Mock(model_validate=Mock(side_effect=ValueError("Invalid checkout response"))))
    else:
        c.db.commit.side_effect = RuntimeError("Commit failed")
    with pytest.raises((RuntimeError, ValueError)):
        service.create_checkout(c.db, c.asset.id, c.student.id, c.staff, c.loan.due_at)
    c.db.rollback.assert_called_once()
    if stage != "commit":
        c.db.commit.assert_not_called()

def test_checkout_locks_parent_after_asset_before_custody_records(flow):
    from sqlalchemy.dialects import postgresql
    c = flow
    c.asset.availability_status = "AVAILABLE"
    service.create_checkout(c.db, c.asset.id, c.student.id, c.staff, c.loan.due_at)
    statements = [call.args[0] for call in c.db.scalar.call_args_list if call.args[0]._for_update_arg is not None]
    assert [stmt.column_descriptions[0]["entity"] for stmt in statements] == [
        EquipmentAsset, Equipment, EquipmentCheckout, MaintenanceRequest,
    ]
    assert "FOR SHARE" in str(statements[1].compile(dialect=postgresql.dialect()))
    assert all(stmt.get_execution_options()["populate_existing"] for stmt in statements)
