from contextlib import nullcontext
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.dialects import postgresql

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User
from backend.app.services import maintenance as service


@pytest.fixture
def case():
    c = SimpleNamespace(now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc), events=[], checkout=None, flush_states=[])
    c.users = {name: User(id=uuid4(), role=role, is_active=active) for name, role, active in (
        ("faculty", "FACULTY", True), ("assistant", "LAB_ASSISTANT", True),
        ("admin", "ADMIN", True), ("student", "STUDENT", True),
        ("inactive", "FACULTY", False), ("verifier", "FACULTY", True),
    )}
    c.asset = EquipmentAsset(id=uuid4(), availability_status="MAINTENANCE", condition_status="DAMAGED", maintenance_hold=True)
    c.request = MaintenanceRequest(
        id=uuid4(), asset_id=c.asset.id, issue_type="DAMAGE", issue_description="Original issue",
        status="OPEN", reported_by=c.users["faculty"].id, reported_at=c.now - timedelta(days=1), version=1,
    )
    c.db = Mock()
    c.db.new, c.db.dirty, c.db.deleted = set(), set(), set()
    c.db.in_nested_transaction.return_value = False
    c.db.no_autoflush = nullcontext()
    c.db.get.side_effect = lambda model, identifier, **kwargs: next((u for u in c.users.values() if u.id == identifier), None)

    def scalar(statement):
        sql = str(statement)
        if "clock_timestamp()" in sql:
            return c.now
        entity = statement.column_descriptions[0].get("entity")
        if entity is EquipmentAsset:
            return c.asset
        if entity is EquipmentCheckout:
            return c.checkout
        if entity is MaintenanceRequest:
            if c.request is None:
                return None
            if len(list(statement.selected_columns)) == 1:
                return c.request.asset_id if list(statement.selected_columns)[0].name == "asset_id" else c.request.id
            if "status IN" in sql and c.request.status not in service.ACTIVE:
                return None
            return c.request
        raise AssertionError("Unexpected query: " + sql)

    def add(obj):
        if isinstance(obj, MaintenanceRequest):
            c.request = obj
        elif isinstance(obj, MaintenanceEvent):
            c.events.append(obj)

    c.db.scalar.side_effect, c.db.add.side_effect = scalar, add

    def flush():
        c.flush_states.append(dict(status=c.request.status if c.request else None,
                                   version=c.request.version if c.request else None,
                                   availability=c.asset.availability_status,
                                   condition=c.asset.condition_status,
                                   hold=c.asset.maintenance_hold, events=len(c.events)))

    c.db.flush.side_effect = flush
    return c


def run(c, operation, actor="faculty", **kwargs):
    kwargs.setdefault("request_id", c.request.id)
    kwargs.setdefault("expected_version", c.request.version)
    return operation(c.db, actor=c.users[actor], **kwargs)


def prepare(c, state="AWAITING_VERIFICATION", repairer="assistant"):
    c.request.assigned_to = c.users[repairer].id
    c.request.status = state
    if state != "OPEN":
        c.request.started_at = c.now - timedelta(hours=1)
        c.asset.condition_status = "UNDER_REPAIR"
    if state in {"AWAITING_VERIFICATION", "COMPLETED"}:
        c.request.repair_completed_at = c.now - timedelta(minutes=30)
        c.request.repair_completed_by = c.users[repairer].id
        c.asset.condition_status = "WORKING"


def assert_http(code, operation):
    with pytest.raises(HTTPException) as error:
        operation()
    assert error.value.status_code == code


@pytest.mark.parametrize("availability", ["AVAILABLE", "RESERVED", "BORROWED"])
@pytest.mark.parametrize("issue_type", ["DAMAGE", "MALFUNCTION", "INSPECTION"])
def test_report_preserves_custody_reservations_description_and_audit(case, availability, issue_type):
    c = case
    c.request = None
    c.asset.availability_status, c.asset.condition_status, c.asset.maintenance_hold = availability, "WORKING", False
    if availability == "BORROWED":
        c.checkout = EquipmentCheckout(id=uuid4(), asset_id=c.asset.id, status="BORROWED", notes="Loan history")
    description = "  Original description\nKeep exact formatting. "
    result = service.report_maintenance_issue(c.db, c.asset.id, c.users["faculty"], issue_type, description)
    assert result.status == "OPEN" and result.version == 1
    assert result.issue_description == description and result.reported_at == c.now
    assert result.maintenance_hold is True
    assert result.availability_status == ("MAINTENANCE" if availability == "AVAILABLE" else availability)
    assert result.condition_status == ("WORKING" if issue_type == "INSPECTION" else "DAMAGED")
    event, = c.events
    assert event.event_type == "REPORTED" and event.sequence == 1 and event.actor_id == result.reported_by
    assert event.from_status is None and event.to_status == "OPEN"
    assert event.from_maintenance_hold is False and event.to_maintenance_hold is True
    assert event.occurred_at == c.now
    assert c.flush_states[0]["status"] is None and c.flush_states[0]["hold"] is True
    assert c.flush_states[1]["status"] == "OPEN" and c.flush_states[1]["events"] == 0
    assert c.flush_states[2]["events"] == 1
    assert all(not call.args and not call.kwargs for call in c.db.flush.call_args_list)
    if c.checkout:
        assert c.checkout.status == "BORROWED" and c.checkout.notes == "Loan history"
    c.db.commit.assert_called_once()
    c.db.rollback.assert_not_called()


def test_full_lifecycle_is_versioned_and_returns_immutable_snapshots(case):
    c = case
    c.request = None
    c.asset.availability_status, c.asset.maintenance_hold = "AVAILABLE", False
    original = service.report_maintenance_issue(c.db, c.asset.id, c.users["faculty"], "DAMAGE", "Fault")
    run(c, service.assign_maintenance_request, assignee_id=c.users["assistant"].id)
    started = run(c, service.start_repair, actor="assistant")
    assert started.started_at == c.now and started.condition_status == "UNDER_REPAIR"
    submitted = run(c, service.submit_repair_for_verification, actor="assistant", repair_notes="Repair findings")
    assert submitted.status == "AWAITING_VERIFICATION" and submitted.maintenance_hold
    assert submitted.repair_completed_by == c.users["assistant"].id
    c.db.flush.reset_mock()
    c.flush_states.clear()
    completed = run(c, service.verify_repair, outcome="PASS", notes="Independent functional test")
    assert completed.status == "COMPLETED" and completed.version == 5
    assert completed.availability_status == "AVAILABLE" and completed.condition_status == "WORKING"
    assert not completed.maintenance_hold
    assert completed.verified_by == c.users["faculty"].id
    assert completed.verified_at == completed.returned_to_service_at == c.now
    assert c.flush_states == [
        dict(status="COMPLETED", version=5, availability="MAINTENANCE", condition="WORKING", hold=True, events=4),
        dict(status="COMPLETED", version=5, availability="MAINTENANCE", condition="WORKING", hold=True, events=5),
        dict(status="COMPLETED", version=5, availability="AVAILABLE", condition="WORKING", hold=False, events=5),
    ]
    assert all(not call.args and not call.kwargs for call in c.db.flush.call_args_list)
    assert [e.sequence for e in c.events] == [1, 2, 3, 4, 5]
    assert [e.event_type for e in c.events] == ["REPORTED", "ASSIGNED", "STARTED", "REPAIR_COMPLETED", "VERIFIED"]
    assert c.events[-1].from_maintenance_hold and not c.events[-1].to_maintenance_hold
    assert original.status == "OPEN" and original.version == 1
    with pytest.raises(FrozenInstanceError):
        original.version = 99


@pytest.mark.parametrize("actor", ["student", "inactive"])
@pytest.mark.parametrize("operation", ["report", "assign", "start", "submit", "verify", "cancel", "recover", "view", "history"])
def test_inactive_and_student_denied_for_all_operations(case, actor, operation):
    c = case
    functions = {
        "report": lambda: service.report_maintenance_issue(c.db, c.asset.id, c.users[actor], "DAMAGE", "Fault"),
        "assign": lambda: run(c, service.assign_maintenance_request, actor, assignee_id=c.users["assistant"].id),
        "start": lambda: run(c, service.start_repair, actor),
        "submit": lambda: run(c, service.submit_repair_for_verification, actor, repair_notes="Repaired"),
        "verify": lambda: run(c, service.verify_repair, actor, outcome="PASS", notes="Checked"),
        "cancel": lambda: run(c, service.cancel_maintenance_request, actor, reason="Cancel"),
        "recover": lambda: service.recover_cancelled_request(c.db, c.request.id, c.users[actor], "Inspect"),
        "view": lambda: service.get_maintenance_request(c.db, c.request.id, c.users[actor]),
        "history": lambda: service.list_maintenance_events(c.db, c.request.id, c.users[actor]),
    }
    assert_http(403, functions[operation])
    c.db.scalar.assert_not_called()
    c.db.commit.assert_not_called()


@pytest.mark.parametrize("role", ["faculty", "assistant", "admin"])
def test_authoritative_persisted_user_is_required_for_reporting(case, role):
    c = case
    c.request = None
    c.asset.availability_status, c.asset.maintenance_hold = "AVAILABLE", False
    assert service.report_maintenance_issue(c.db, c.asset.id, c.users[role], "INSPECTION", "Inspect").reported_by == c.users[role].id


def test_forged_or_stale_role_attributes_are_not_trusted(case):
    forged = User(id=case.users["student"].id, role="ADMIN", is_active=True)
    assert_http(403, lambda: service.report_maintenance_issue(case.db, case.asset.id, forged, "DAMAGE", "Fault"))


@pytest.mark.parametrize("operation", [service.assign_maintenance_request, service.cancel_maintenance_request, service.verify_repair])
def test_assistant_cannot_manage_or_verify(case, operation):
    kwargs = {"assignee_id": case.users["faculty"].id} if operation is service.assign_maintenance_request else (
        {"reason": "Cancel"} if operation is service.cancel_maintenance_request else {"outcome": "PASS", "notes": "Checked"})
    assert_http(403, lambda: run(case, operation, "assistant", **kwargs))


@pytest.mark.parametrize("operation", [service.start_repair, service.submit_repair_for_verification])
def test_admin_cannot_perform_repairs(case, operation):
    kwargs = {"repair_notes": "Repair"} if operation is service.submit_repair_for_verification else {}
    assert_http(403, lambda: run(case, operation, "admin", **kwargs))


@pytest.mark.parametrize("assignee, expected", [("student", 403), ("admin", 403), ("inactive", 403), ("missing", 404)])
def test_assignment_requires_active_eligible_assignee(case, assignee, expected):
    identifier = uuid4() if assignee == "missing" else case.users[assignee].id
    assert_http(expected, lambda: run(case, service.assign_maintenance_request, assignee_id=identifier))


@pytest.mark.parametrize("version", [0, -1, True, 1.5, "1", 2**31 - 1])
def test_invalid_expected_version_rejected(case, version):
    assert_http(422, lambda: run(case, service.cancel_maintenance_request, expected_version=version, reason="Cancel"))


def test_stale_version_and_duplicate_report_do_not_modify_state(case):
    c = case
    assert_http(409, lambda: run(c, service.cancel_maintenance_request, expected_version=99, reason="Cancel"))
    assert_http(409, lambda: service.report_maintenance_issue(c.db, c.asset.id, c.users["faculty"], "DAMAGE", "Duplicate"))
    assert c.request.version == 1 and c.request.status == "OPEN"
    assert c.events == []
    c.db.flush.assert_not_called()


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED"])
def test_terminal_requests_rejected(case, terminal):
    case.request.status = terminal
    assert_http(409, lambda: run(case, service.cancel_maintenance_request, reason="Again"))


@pytest.mark.parametrize("state, operation", [("IN_PROGRESS", "start"), ("OPEN", "submit"),
                                           ("OPEN", "verify"), ("AWAITING_VERIFICATION", "assign")])
def test_invalid_transitions_rejected(case, state, operation):
    prepare(case, state)
    calls = {"start": lambda: run(case, service.start_repair, "assistant"),
             "submit": lambda: run(case, service.submit_repair_for_verification, "assistant", repair_notes="Repair"),
             "verify": lambda: run(case, service.verify_repair, outcome="PASS", notes="Checked"),
             "assign": lambda: run(case, service.assign_maintenance_request, assignee_id=case.users["faculty"].id)}
    assert_http(409, calls[operation])


@pytest.mark.parametrize("operation", ["start", "submit"])
def test_only_assigned_repairer_can_work(case, operation):
    prepare(case, "OPEN" if operation == "start" else "IN_PROGRESS")
    op = service.start_repair if operation == "start" else service.submit_repair_for_verification
    kwargs = {} if operation == "start" else {"repair_notes": "Repair"}
    assert_http(403, lambda: run(case, op, "faculty", **kwargs))


@pytest.mark.parametrize("operation", ["start", "submit", "verify"])
def test_borrowed_equipment_cannot_be_repaired_or_verified(case, operation):
    prepare(case, {"start": "OPEN", "submit": "IN_PROGRESS", "verify": "AWAITING_VERIFICATION"}[operation])
    case.asset.availability_status = "BORROWED"
    case.checkout = EquipmentCheckout(id=uuid4(), asset_id=case.asset.id, status="BORROWED")
    calls = {"start": lambda: run(case, service.start_repair, "assistant"),
             "submit": lambda: run(case, service.submit_repair_for_verification, "assistant", repair_notes="Repair"),
             "verify": lambda: run(case, service.verify_repair, outcome="PASS", notes="Checked")}
    assert_http(409, calls[operation])
    assert case.checkout.status == "BORROWED"
    case.db.flush.assert_not_called()


@pytest.mark.parametrize("outcome", ["PASS", "FAIL"])
def test_no_self_verification_bypass(case, outcome):
    prepare(case, repairer="faculty")
    assert_http(403, lambda: run(case, service.verify_repair, outcome=outcome, notes="Checked"))
    assert case.asset.maintenance_hold


def test_failed_verification_and_repair_resubmission_preserve_audit(case):
    c = case
    prepare(c)
    old_completion = c.request.repair_completed_at
    failed = run(c, service.verify_repair, outcome="FAIL", notes="Still intermittently faulty")
    assert failed.status == "IN_PROGRESS" and failed.maintenance_hold and failed.verified_at is None
    assert failed.condition_status == "UNDER_REPAIR"
    assert failed.repair_completed_at == old_completion
    assert c.events[-1].event_type == "VERIFICATION_FAILED"
    assert c.events[-1].note == "Still intermittently faulty"
    run(c, service.submit_repair_for_verification, "assistant", repair_notes="Second repair attempt")
    assert c.request.repair_completed_at == c.now
    assert c.events[-1].note == "Second repair attempt"


@pytest.mark.parametrize("availability", ["MAINTENANCE", "RESERVED"])
def test_pass_releases_only_maintenance_and_preserves_reservations(case, availability):
    prepare(case)
    case.asset.availability_status = availability
    result = run(case, service.verify_repair, "admin", outcome="PASS", notes="Independent verification")
    assert result.availability_status == ("AVAILABLE" if availability == "MAINTENANCE" else "RESERVED")
    assert not result.maintenance_hold


def test_cancel_and_linked_recovery_do_not_release(case):
    c = case
    original_description = c.request.issue_description
    cancelled = run(c, service.cancel_maintenance_request, reason="Replacement inspection needed")
    parent = c.request
    assert cancelled.status == "CANCELLED" and cancelled.maintenance_hold
    assert cancelled.cancelled_at == c.now and cancelled.cancellation_reason == c.events[-1].note
    replacement = service.recover_cancelled_request(c.db, parent.id, c.users["assistant"], "Inspect cancelled fault")
    assert replacement.issue_type == "INSPECTION" and replacement.status == "OPEN"
    assert replacement.recovery_of_request_id == cancelled.id and replacement.maintenance_hold
    assert parent.status == "CANCELLED" and parent.issue_description == original_description
    assert replacement.id != parent.id


@pytest.mark.parametrize("kind", ["wrong_asset", "not_cancelled", "no_hold"])
def test_invalid_recovery_relationship(case, kind):
    c = case
    c.request.status = "CANCELLED"
    if kind == "wrong_asset":
        c.request.asset_id = uuid4()
    elif kind == "not_cancelled":
        c.request.status = "COMPLETED"
    else:
        c.asset.maintenance_hold = False
    assert_http(409, lambda: service.report_maintenance_issue(c.db, c.asset.id, c.users["faculty"], "INSPECTION", "Inspect", c.request.id))


@pytest.mark.parametrize("issue_type, description", [("INVALID", "Fault"), ([], "Fault"),
    ("DAMAGE", " \t\n"), ("DAMAGE", "x" * 4001), ("DAMAGE", None)])
def test_report_validation(case, issue_type, description):
    assert_http(422, lambda: service.report_maintenance_issue(case.db, case.asset.id, case.users["faculty"], issue_type, description))


@pytest.mark.parametrize("notes", [None, "", " \t", "x" * 5001])
def test_required_repair_notes(case, notes):
    prepare(case, "IN_PROGRESS")
    assert_http(422, lambda: run(case, service.submit_repair_for_verification, "assistant", repair_notes=notes))


@pytest.mark.parametrize("point", [1, 2, 3, "commit"])
@pytest.mark.parametrize("integrity", [True, False])
def test_all_write_failures_roll_back(case, point, integrity):
    prepare(case)
    failure = IntegrityError("test", {}, Exception()) if integrity else RuntimeError("Injected failure")
    if point == "commit":
        case.db.commit.side_effect = failure
    else:
        case.db.flush.side_effect = [None] * (point - 1) + [failure]
    with pytest.raises(HTTPException if integrity else RuntimeError) as error:
        run(case, service.verify_repair, outcome="PASS", notes="Verified")
    if integrity:
        assert error.value.status_code == 409
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("sqlstate", ["40001", "40P01", "55P03"])
def test_concurrency_database_errors_are_sanitized_and_rolled_back(case, sqlstate):
    failure = SimpleNamespace(sqlstate=sqlstate)
    case.db.flush.side_effect = OperationalError("sensitive SQL", {}, failure)
    assert_http(409, lambda: run(case, service.cancel_maintenance_request, reason="Cancel"))
    case.db.rollback.assert_called_once()


def test_role_is_rechecked_after_lock_wait(case):
    actor = case.users["faculty"]
    case.db.get.side_effect = [actor, case.users["inactive"]]
    assert_http(403, lambda: run(case, service.cancel_maintenance_request, reason="Cancel"))
    case.db.flush.assert_not_called()


def test_asset_checkout_request_lock_order_and_refresh(case):
    run(case, service.cancel_maintenance_request, reason="Cancel")
    statements = [call.args[0] for call in case.db.scalar.call_args_list if "FOR UPDATE" in str(call.args[0])]
    assert [s.column_descriptions[0]["entity"] for s in statements] == [EquipmentAsset, EquipmentCheckout, MaintenanceRequest]
    assert all(s.get_execution_options()["populate_existing"] for s in statements)
    assert sum("clock_timestamp()" in str(call.args[0]) for call in case.db.scalar.call_args_list) == 1


@pytest.mark.parametrize("problem", ["dirty", "new", "deleted", "nested"])
def test_unrelated_pending_work_is_never_committed_or_discarded(case, problem):
    if problem == "nested":
        case.db.in_nested_transaction.return_value = True
    else:
        setattr(case.db, problem, {object()})
    assert_http(409, lambda: run(case, service.cancel_maintenance_request, reason="Cancel"))
    case.db.commit.assert_not_called()
    case.db.rollback.assert_not_called()


def test_view_operations_are_authorized_bounded_and_read_only(case):
    c = case
    record = service._snapshot(c.request, c.asset)
    c.db.execute.return_value.mappings.return_value.one_or_none.return_value = {
        field.name: getattr(record, field.name) for field in fields(record)
    }
    assert service.get_maintenance_request(c.db, c.request.id, c.users["assistant"]) == record
    assert "JOIN equipment_assets" in str(c.db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    c.db.execute.return_value.mappings.return_value.all.return_value = []
    page = service.list_maintenance_events(c.db, c.request.id, c.users["admin"], limit=100, offset=3)
    assert page.items == () and not page.has_more
    assert "LIMIT" in str(c.db.execute.call_args.args[0])
    c.db.commit.assert_not_called()
    c.db.flush.assert_not_called()


@pytest.mark.parametrize("limit, offset", [(0, 0), (101, 0), (True, 0), (1, -1), (1, 2**63)])
def test_history_pagination_validation(case, limit, offset):
    assert_http(422, lambda: service.list_maintenance_events(case.db, case.request.id, case.users["faculty"], limit, offset))


@pytest.mark.parametrize("problem, expected", [("missing_asset", 404), ("retired", 409), ("missing", 409),
                                            ("borrowed_without_loan", 409), ("loan_without_borrowed", 409)])
def test_reporting_rejects_missing_or_inconsistent_assets(case, problem, expected):
    c = case
    c.request = None
    identifier = c.asset.id
    if problem == "missing_asset":
        c.asset = None
    elif problem == "retired":
        c.asset.availability_status = "RETIRED"
    elif problem == "missing":
        c.asset.condition_status = "MISSING"
    elif problem == "borrowed_without_loan":
        c.asset.availability_status = "BORROWED"
    else:
        c.checkout = EquipmentCheckout(id=uuid4(), asset_id=identifier, status="BORROWED")
    assert_http(expected, lambda: service.report_maintenance_issue(c.db, identifier, c.users["faculty"], "INSPECTION", "Inspect"))
    c.db.flush.assert_not_called()


def test_missing_request_and_unknown_actor_rejected(case):
    c = case
    c.request = None
    assert_http(404, lambda: service.start_repair(c.db, uuid4(), c.users["assistant"], 1))
    assert_http(403, lambda: service.report_maintenance_issue(c.db, c.asset.id,
                User(id=uuid4(), role="ADMIN", is_active=True), "DAMAGE", "Fault"))


@pytest.mark.parametrize("problem", ["missing", "retired", "no_hold"])
def test_verification_never_releases_ineligible_assets(case, problem):
    prepare(case)
    if problem == "missing":
        case.asset.condition_status = "MISSING"
    elif problem == "retired":
        case.asset.availability_status = "RETIRED"
    else:
        case.asset.maintenance_hold = False
    assert_http(409, lambda: run(case, service.verify_repair, outcome="PASS", notes="Check"))
    case.db.flush.assert_not_called()


@pytest.mark.parametrize("outcome, notes", [("INVALID", "Checked"), ([], "Checked"), ("PASS", None),
                                          ("PASS", " "), ("FAIL", "x" * 5001)])
def test_verification_outcome_and_notes_validation(case, outcome, notes):
    prepare(case)
    assert_http(422, lambda: run(case, service.verify_repair, outcome=outcome, notes=notes))
    assert case.request.status == "AWAITING_VERIFICATION"


@pytest.mark.parametrize("reason", [None, " \n", "x" * 4001])
def test_cancellation_reason_validation(case, reason):
    assert_http(422, lambda: run(case, service.cancel_maintenance_request, reason=reason))
    assert case.request.status == "OPEN"


def test_same_assignee_does_not_create_a_false_assignment_event(case):
    case.request.assigned_to = case.users["assistant"].id
    assert_http(409, lambda: run(case, service.assign_maintenance_request, assignee_id=case.users["assistant"].id))
    assert case.events == []


def test_database_clock_rollback_and_recovery_type_are_rejected(case):
    case.request.reported_at = case.now + timedelta(seconds=1)
    assert_http(409, lambda: run(case, service.cancel_maintenance_request, reason="Cancel"))
    assert_http(422, lambda: service.report_maintenance_issue(case.db, case.asset.id, case.users["faculty"],
               "DAMAGE", "Fault", recovery_of_request_id=uuid4()))
    case.db.flush.assert_not_called()


def test_paginated_event_snapshots_have_more_and_cannot_be_modified(case):
    c = case
    c.request = None
    service.report_maintenance_issue(c.db, c.asset.id, c.users["faculty"], "INSPECTION", "Inspect")
    run(c, service.assign_maintenance_request, assignee_id=c.users["assistant"].id)
    rows = [{column.name: getattr(event, column.name) for column in MaintenanceEvent.__table__.columns} for event in c.events]
    c.db.execute.return_value.mappings.return_value.all.return_value = rows
    page = service.list_maintenance_events(c.db, c.request.id, c.users["assistant"], limit=1)
    assert len(page.items) == 1 and page.has_more and page.items[0].sequence == 1
    with pytest.raises(FrozenInstanceError):
        page.items[0].note = "Rewrite"


def test_read_views_return_404_for_unknown_requests(case):
    case.db.execute.return_value.mappings.return_value.one_or_none.return_value = None
    assert_http(404, lambda: service.get_maintenance_request(case.db, uuid4(), case.users["faculty"]))
    case.request = None
    assert_http(404, lambda: service.list_maintenance_events(case.db, uuid4(), case.users["faculty"]))


@pytest.mark.parametrize("actor", ["faculty", "admin"])
@pytest.mark.parametrize("operation", ["assign", "cancel"])
def test_faculty_and_admin_management_permissions(case, actor, operation):
    if operation == "assign":
        result = run(case, service.assign_maintenance_request, actor, assignee_id=case.users["assistant"].id)
        assert result.assigned_to == case.users["assistant"].id
    else:
        result = run(case, service.cancel_maintenance_request, actor, reason="Inspection case replacement")
        assert result.status == "CANCELLED" and result.maintenance_hold
        assert result.cancelled_by == case.users[actor].id


@pytest.mark.parametrize("state", ["OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION"])
def test_cancellation_preserves_asset_state_from_every_active_status(case, state):
    prepare(case, state)
    before = (case.asset.condition_status, case.asset.availability_status, case.asset.maintenance_hold)
    result = run(case, service.cancel_maintenance_request, reason="Inspection replacement")
    assert result.status == "CANCELLED"
    assert (case.asset.condition_status, case.asset.availability_status, case.asset.maintenance_hold) == before
    assert case.events[-1].from_status == state and case.events[-1].to_status == "CANCELLED"


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_unrelated_work_appearing_after_validation_is_not_flushed(case, pending):
    c = case
    scalar = c.db.scalar.side_effect

    def introduce_pending(statement):
        result = scalar(statement)
        if "clock_timestamp()" in str(statement):
            setattr(c.db, pending, {object()})
        return result

    c.db.scalar.side_effect = introduce_pending
    assert_http(409, lambda: run(c, service.cancel_maintenance_request, reason="Cancel"))
    c.db.flush.assert_not_called()
    c.db.commit.assert_not_called()
    c.db.rollback.assert_called_once()


@pytest.mark.parametrize("after_stage", [1, 2, 3])
def test_unrelated_work_between_flush_stages_or_before_commit_rolls_back(case, after_stage):
    c = case
    prepare(c)
    flush = c.db.flush.side_effect

    def introduce_pending():
        flush()
        if len(c.flush_states) == after_stage:
            c.db.new = {object()}

    c.db.flush.side_effect = introduce_pending
    assert_http(409, lambda: run(c, service.verify_repair, outcome="PASS", notes="Verified"))
    assert c.db.flush.call_count == after_stage
    c.db.commit.assert_not_called()
    c.db.rollback.assert_called_once()


@pytest.mark.parametrize("operation, state, kwargs", [
    (service.start_repair, "OPEN", {}),
    (service.submit_repair_for_verification, "IN_PROGRESS", {"repair_notes": "Repair complete"}),
])
def test_nonrelease_stages_do_not_make_request_dirty_before_asset_flush(case, operation, state, kwargs):
    prepare(case, state)
    original_version = case.request.version
    run(case, operation, "assistant", **kwargs)
    assert case.flush_states[0]["status"] == state
    assert case.flush_states[0]["version"] == original_version
    assert case.flush_states[0]["events"] == 0
    assert case.flush_states[1]["version"] == original_version + 1
    assert case.flush_states[1]["events"] == 0
    assert case.flush_states[2]["events"] == 1


def test_maintenance_sources_never_use_selective_session_flush():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for relative in ("app/services/maintenance.py", "tests/test_maintenance_service.py",
                     "tests/integration/test_maintenance_service.py"):
        path = root / "backend" / relative
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "flush"]
        assert all(not call.args and not call.keywords for call in calls), relative
