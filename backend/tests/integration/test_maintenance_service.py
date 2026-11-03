"""Phase 2 services against the unchanged Phase 1 PostgreSQL foundation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import time
from uuid import uuid4

from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User
from backend.app.services import maintenance as service


pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SADeprecationWarning")


@pytest.fixture
def service_rows(database):
    with Session(database) as db:
        users = {}
        for label, role, active in (("faculty", "FACULTY", True), ("assistant", "LAB_ASSISTANT", True),
                                     ("admin", "ADMIN", True), ("student", "STUDENT", True),
                                     ("inactive", "FACULTY", False)):
            user = User(id=uuid4(), college_id="SERVICE-" + label, full_name="Test staff",
                        email=label + "@example.invalid", role=role, is_active=active)
            db.add(user)
            users[label] = user.id
        equipment = Equipment(id=uuid4(), name="Service sensor", category="IoT",
                              tracking_type="INDIVIDUAL", default_location="Test lab")
        db.add(equipment)
        db.flush()
        asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id, asset_code="SERVICE-ASSET", location="Test lab")
        db.add(asset)
        db.commit()
        return dict(users=users, asset=asset.id)


@pytest.fixture
def call(database, service_rows):
    def invoke(operation, actor="faculty", **kwargs):
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            if operation in {service.get_maintenance_request, service.list_maintenance_events}:
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
            return operation(db, actor=db.get(User, service_rows["users"][actor]), **kwargs)
    return invoke


@pytest.fixture
def reported(call, service_rows):
    return call(service.report_maintenance_issue, asset_id=service_rows["asset"],
                issue_type="DAMAGE", issue_description="Original fault report")


@pytest.fixture
def assigned(call, service_rows, reported):
    return call(service.assign_maintenance_request, request_id=reported.id,
                expected_version=reported.version, assignee_id=service_rows["users"]["assistant"])


@pytest.fixture
def awaiting(call, assigned):
    started = call(service.start_repair, "assistant", request_id=assigned.id, expected_version=assigned.version)
    return call(service.submit_repair_for_verification, "assistant", request_id=started.id,
                expected_version=started.version, repair_notes="Replaced faulty connector")


def snapshot(database):
    with database.connect() as connection:
        return tuple(connection.execute(sa.select(table).order_by(table.c.id)).all() for table in (
            EquipmentAsset.__table__, EquipmentCheckout.__table__,
            MaintenanceRequest.__table__, MaintenanceEvent.__table__,
        ))


def assert_http(code, action):
    with pytest.raises(HTTPException) as error:
        action()
    assert error.value.status_code == code


def test_service_full_lifecycle_database_clock_audit_and_release(database, call, service_rows):
    with database.connect() as connection:
        before = connection.scalar(sa.select(sa.func.clock_timestamp()))
    report = call(service.report_maintenance_issue, asset_id=service_rows["asset"], issue_type="MALFUNCTION",
                  issue_description=" Original report with formatting\n ")
    assignment = call(service.assign_maintenance_request, "admin", request_id=report.id,
                      expected_version=1, assignee_id=service_rows["users"]["assistant"])
    start = call(service.start_repair, "assistant", request_id=report.id, expected_version=assignment.version)
    submission = call(service.submit_repair_for_verification, "assistant", request_id=report.id,
                      expected_version=start.version, repair_notes="Repaired component")
    assert submission.maintenance_hold and submission.availability_status == "MAINTENANCE"
    completed = call(service.verify_repair, request_id=report.id, expected_version=submission.version,
                     outcome="PASS", notes="Independent electrical and functional inspection passed")
    with Session(database) as db:
        after = db.scalar(sa.select(sa.func.clock_timestamp()))
        request = db.get(MaintenanceRequest, report.id)
        asset = db.get(EquipmentAsset, service_rows["asset"])
        assert request.status == "COMPLETED" and request.version == 5
        assert request.issue_description == report.issue_description
        assert before <= request.reported_at <= request.started_at <= request.repair_completed_at <= request.verified_at <= after
        assert request.returned_to_service_at == request.verified_at == completed.verified_at
        assert request.repair_completed_by == service_rows["users"]["assistant"]
        assert request.verified_by == service_rows["users"]["faculty"]
        assert not asset.maintenance_hold and asset.condition_status == "WORKING" and asset.availability_status == "AVAILABLE"
        events = db.scalars(sa.select(MaintenanceEvent).order_by(MaintenanceEvent.sequence)).all()
        assert [e.sequence for e in events] == [1, 2, 3, 4, 5]
        assert [e.event_type for e in events] == ["REPORTED", "ASSIGNED", "STARTED", "REPAIR_COMPLETED", "VERIFIED"]
        assert events[0].from_availability == "AVAILABLE" and events[0].to_availability == "MAINTENANCE"
        assert events[-1].from_maintenance_hold and not events[-1].to_maintenance_hold
        assert events[-1].to_availability == "AVAILABLE"
        assert events[3].note == "Repaired component" and "Independent" in events[4].note
        assert all(before <= event.occurred_at <= after for event in events)
        assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 0
    assert report.status == "OPEN" and report.version == 1


def test_service_preserves_genuine_reservation_through_verification(database, call, service_rows):
    with Session(database) as db:
        db.get(EquipmentAsset, service_rows["asset"]).availability_status = "RESERVED"
        db.commit()
    request = call(service.report_maintenance_issue, asset_id=service_rows["asset"], issue_type="INSPECTION",
                   issue_description="Inspect reserved asset")
    for operation, actor, kwargs in (
        (service.assign_maintenance_request, "faculty", {"assignee_id": service_rows["users"]["assistant"]}),
        (service.start_repair, "assistant", {}),
        (service.submit_repair_for_verification, "assistant", {"repair_notes": "Inspection complete"}),
        (service.verify_repair, "admin", {"outcome": "PASS", "notes": "Independent inspection passed"}),
    ):
        assert request.availability_status == "RESERVED"
        request = call(operation, actor, request_id=request.id, expected_version=request.version, **kwargs)
    assert request.availability_status == "RESERVED" and not request.maintenance_hold


def test_service_reporting_and_assignment_preserve_active_checkout(database, call, service_rows):
    with Session(database) as db:
        now = db.scalar(sa.select(sa.func.clock_timestamp()))
        asset = db.get(EquipmentAsset, service_rows["asset"])
        asset.availability_status = "BORROWED"
        checkout = EquipmentCheckout(id=uuid4(), asset_id=asset.id, student_id=service_rows["users"]["student"],
                                     issued_by=service_rows["users"]["faculty"], checked_out_at=now,
                                     due_at=now + timedelta(days=1), notes="Preserve this loan")
        db.add(checkout)
        db.commit()
    with database.connect() as connection:
        before = connection.execute(sa.select(EquipmentCheckout.__table__)).all()
    request = call(service.report_maintenance_issue, asset_id=service_rows["asset"], issue_type="DAMAGE",
                   issue_description="Fault noticed while borrowed")
    assert request.availability_status == "BORROWED" and request.maintenance_hold
    request = call(service.assign_maintenance_request, request_id=request.id, expected_version=request.version,
                   assignee_id=service_rows["users"]["assistant"])
    assert_http(409, lambda: call(service.start_repair, "assistant", request_id=request.id, expected_version=request.version))
    with database.connect() as connection:
        assert connection.execute(sa.select(EquipmentCheckout.__table__)).all() == before


@pytest.mark.parametrize("actor", ["student", "inactive"])
@pytest.mark.parametrize("operation", ["report", "assign", "start", "submit", "verify", "cancel", "recover", "view", "events"])
def test_service_student_and_inactive_authorization(database, call, service_rows, reported, actor, operation):
    actions = {
        "report": lambda: call(service.report_maintenance_issue, actor, asset_id=service_rows["asset"], issue_type="DAMAGE", issue_description="Fault"),
        "assign": lambda: call(service.assign_maintenance_request, actor, request_id=reported.id, expected_version=1, assignee_id=service_rows["users"]["assistant"]),
        "start": lambda: call(service.start_repair, actor, request_id=reported.id, expected_version=1),
        "submit": lambda: call(service.submit_repair_for_verification, actor, request_id=reported.id, expected_version=1, repair_notes="Repair"),
        "verify": lambda: call(service.verify_repair, actor, request_id=reported.id, expected_version=1, outcome="PASS", notes="Checked"),
        "cancel": lambda: call(service.cancel_maintenance_request, actor, request_id=reported.id, expected_version=1, reason="Cancel"),
        "recover": lambda: call(service.recover_cancelled_request, actor, request_id=reported.id, issue_description="Inspect"),
        "view": lambda: call(service.get_maintenance_request, actor, request_id=reported.id),
        "events": lambda: call(service.list_maintenance_events, actor, request_id=reported.id),
    }
    before = snapshot(database)
    assert_http(403, actions[operation])
    assert snapshot(database) == before


@pytest.mark.parametrize("assignee", ["admin", "student", "inactive", "missing"])
def test_service_invalid_assignees(database, call, service_rows, reported, assignee):
    before = snapshot(database)
    identifier = uuid4() if assignee == "missing" else service_rows["users"][assignee]
    assert_http(404 if assignee == "missing" else 403, lambda: call(
        service.assign_maintenance_request, request_id=reported.id, expected_version=1, assignee_id=identifier))
    assert snapshot(database) == before


def test_service_duplicate_and_stale_commands_do_not_append_events(database, call, service_rows, reported):
    before = snapshot(database)
    assert_http(409, lambda: call(service.report_maintenance_issue, asset_id=service_rows["asset"], issue_type="INSPECTION", issue_description="Duplicate"))
    assert_http(409, lambda: call(service.cancel_maintenance_request, request_id=reported.id, expected_version=2, reason="Stale"))
    assert snapshot(database) == before


@pytest.mark.parametrize("actor", ["faculty", "admin"])
def test_service_independent_verification_failure_and_rework(call, awaiting, actor):
    failed = call(service.verify_repair, actor, request_id=awaiting.id, expected_version=awaiting.version,
                  outcome="FAIL", notes="Intermittent fault remains")
    assert failed.status == "IN_PROGRESS" and failed.maintenance_hold and failed.verified_at is None
    assert failed.repair_completed_at == awaiting.repair_completed_at
    resubmitted = call(service.submit_repair_for_verification, "assistant", request_id=failed.id,
                       expected_version=failed.version, repair_notes="Second repair attempt")
    completed = call(service.verify_repair, actor, request_id=failed.id, expected_version=resubmitted.version,
                     outcome="PASS", notes="Independent retest passed")
    assert completed.status == "COMPLETED" and not completed.maintenance_hold
    events = call(service.list_maintenance_events, "assistant", request_id=failed.id, limit=100)
    assert [e.sequence for e in events.items] == list(range(1, 8))
    assert events.items[4].note == "Intermittent fault remains"
    assert events.items[5].note == "Second repair attempt"


def test_service_single_faculty_repairer_cannot_self_verify(database, call, service_rows, reported):
    request = call(service.assign_maintenance_request, request_id=reported.id, expected_version=1,
                   assignee_id=service_rows["users"]["faculty"])
    request = call(service.start_repair, request_id=request.id, expected_version=request.version)
    request = call(service.submit_repair_for_verification, request_id=request.id, expected_version=request.version,
                   repair_notes="Faculty performed repair")
    before = snapshot(database)
    assert_http(403, lambda: call(service.verify_repair, request_id=request.id, expected_version=request.version,
                                 outcome="PASS", notes="Self verification attempt"))
    assert snapshot(database) == before
    assert call(service.verify_repair, "admin", request_id=request.id, expected_version=request.version,
                outcome="PASS", notes="Independent administrator inspection").status == "COMPLETED"


def test_service_cancellation_recovery_requires_new_verification(database, call, service_rows, awaiting):
    cancelled = call(service.cancel_maintenance_request, request_id=awaiting.id,
                     expected_version=awaiting.version, reason="Replacement inspection needed")
    assert cancelled.status == "CANCELLED" and cancelled.maintenance_hold
    recovered = call(service.recover_cancelled_request, "assistant", request_id=cancelled.id,
                     issue_description="Inspect cancelled repair")
    assert recovered.recovery_of_request_id == cancelled.id and recovered.id != cancelled.id
    assert recovered.status == "OPEN" and recovered.issue_type == "INSPECTION" and recovered.maintenance_hold
    assert_http(409, lambda: call(service.verify_repair, request_id=recovered.id, expected_version=1,
                                 outcome="PASS", notes="Cannot skip repair/inspection stages"))
    request = call(service.assign_maintenance_request, request_id=recovered.id, expected_version=1,
                   assignee_id=service_rows["users"]["assistant"])
    request = call(service.start_repair, "assistant", request_id=request.id, expected_version=request.version)
    request = call(service.submit_repair_for_verification, "assistant", request_id=request.id,
                   expected_version=request.version, repair_notes="Inspection passed")
    assert call(service.verify_repair, "admin", request_id=request.id, expected_version=request.version,
                outcome="PASS", notes="Independent inspection passed").status == "COMPLETED"
    original = call(service.get_maintenance_request, request_id=cancelled.id)
    assert original.status == "CANCELLED" and original.cancellation_reason == cancelled.cancellation_reason
    assert original.issue_description == cancelled.issue_description


def test_service_real_event_fk_failure_rolls_back_report(database, service_rows, monkeypatch):
    before = snapshot(database)
    with Session(database, autoflush=False) as db:
        actor = db.get(User, service_rows["users"]["faculty"])
        add = db.add

        def corrupt_event(obj):
            if isinstance(obj, MaintenanceEvent):
                obj.actor_id = uuid4()
            add(obj)

        monkeypatch.setattr(db, "add", corrupt_event)
        assert_http(409, lambda: service.report_maintenance_issue(db, service_rows["asset"], actor, "DAMAGE", "Fault"))
    assert snapshot(database) == before


@pytest.mark.parametrize("point", [1, 2, 3, "commit"])
def test_service_verification_failure_rolls_back_request_event_and_asset(database, service_rows, awaiting, monkeypatch, point):
    before = snapshot(database)
    with Session(database, autoflush=False) as db:
        actor = db.get(User, service_rows["users"]["faculty"])
        if point == "commit":
            def fail_commit():
                raise RuntimeError("Injected commit failure")
            monkeypatch.setattr(db, "commit", fail_commit)
        else:
            flush = db.flush
            count = 0

            def fail_after_flush():
                nonlocal count
                flush()
                count += 1
                if count == point:
                    raise RuntimeError("Injected flush failure")

            monkeypatch.setattr(db, "flush", fail_after_flush)
        with pytest.raises(RuntimeError):
            service.verify_repair(db, awaiting.id, actor, awaiting.version, "PASS", "Independent inspection")
    assert snapshot(database) == before


@pytest.mark.parametrize("race", ["report", "assign", "verify", "cancel_start"])
def test_service_concurrent_operations_have_one_winner(database, call, service_rows, race):
    if race == "report":
        commands = [(service.report_maintenance_issue, "faculty", dict(asset_id=service_rows["asset"], issue_type="DAMAGE", issue_description="Fault"))] * 2
        expected_version = 1
    else:
        request = call(service.report_maintenance_issue, asset_id=service_rows["asset"], issue_type="DAMAGE", issue_description="Fault")
        if race == "assign":
            commands = [(service.assign_maintenance_request, actor, dict(request_id=request.id, expected_version=1,
                         assignee_id=service_rows["users"][target])) for actor, target in (("faculty", "assistant"), ("admin", "faculty"))]
            expected_version = 2
        else:
            request = call(service.assign_maintenance_request, request_id=request.id, expected_version=1, assignee_id=service_rows["users"]["assistant"])
            if race == "cancel_start":
                commands = [(service.cancel_maintenance_request, "faculty", dict(request_id=request.id, expected_version=2, reason="Cancel")),
                            (service.start_repair, "assistant", dict(request_id=request.id, expected_version=2))]
                expected_version = 3
            else:
                request = call(service.start_repair, "assistant", request_id=request.id, expected_version=2)
                request = call(service.submit_repair_for_verification, "assistant", request_id=request.id, expected_version=3, repair_notes="Repaired")
                commands = [(service.verify_repair, actor, dict(request_id=request.id, expected_version=4, outcome=outcome, notes="Independent test"))
                            for actor, outcome in (("faculty", "PASS"), ("admin", "FAIL"))]
                expected_version = 5
    label = "service_race_" + uuid4().hex

    def attempt(command):
        operation, role, kwargs = command
        with Session(database, autoflush=False) as db:
            db.execute(sa.text("SELECT set_config('application_name', :label, true)"), {"label": label})
            try:
                actor = db.get(User, service_rows["users"][role])
                return 200, operation(db, actor=actor, **kwargs)
            except HTTPException as error:
                return error.status_code, None

    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(sa.select(EquipmentAsset).where(EquipmentAsset.id == service_rows["asset"]).with_for_update())
        futures = [pool.submit(attempt, command) for command in commands]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"
                    ), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            assert waiting == 2
            released_at = blocker.scalar(sa.select(sa.func.clock_timestamp()))
        finally:
            transaction.rollback()
        results = [future.result(timeout=15) for future in futures]
    assert sorted(status for status, _ in results) == [200, 409]
    winner = next(result for status, result in results if status == 200)
    with Session(database) as db:
        requests = db.scalars(sa.select(MaintenanceRequest)).all()
        events = db.scalars(sa.select(MaintenanceEvent).order_by(MaintenanceEvent.sequence)).all()
        asset = db.get(EquipmentAsset, service_rows["asset"])
        assert len(requests) == 1 and requests[0].version == expected_version
        assert requests[0].status == winner.status and len(events) == expected_version
        assert [e.sequence for e in events] == list(range(1, expected_version + 1))
        assert events[-1].occurred_at >= released_at
        assert asset.maintenance_hold == winner.maintenance_hold
        assert asset.availability_status == winner.availability_status
        assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 0


@pytest.mark.parametrize("actor", ["faculty", "assistant", "admin"])
def test_service_views_are_authorized_paginated_and_read_only(database, call, reported, actor):
    before = snapshot(database)
    result = call(service.get_maintenance_request, actor, request_id=reported.id)
    assert result.id == reported.id
    page = call(service.list_maintenance_events, actor, request_id=reported.id, limit=1)
    assert len(page.items) == 1 and page.items[0].event_type == "REPORTED" and not page.has_more
    assert call(service.list_maintenance_events, actor, request_id=reported.id, offset=1).items == ()
    assert snapshot(database) == before


@pytest.mark.parametrize("actor", ["faculty", "admin"])
@pytest.mark.parametrize("state", ["OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION"])
def test_service_cancels_each_active_state_without_release(database, call, service_rows, reported, actor, state):
    request = reported
    if state != "OPEN":
        request = call(service.assign_maintenance_request, request_id=request.id, expected_version=request.version,
                       assignee_id=service_rows["users"]["assistant"])
        request = call(service.start_repair, "assistant", request_id=request.id, expected_version=request.version)
    if state == "AWAITING_VERIFICATION":
        request = call(service.submit_repair_for_verification, "assistant", request_id=request.id,
                       expected_version=request.version, repair_notes="Work complete")
    cancelled = call(service.cancel_maintenance_request, actor, request_id=request.id,
                     expected_version=request.version, reason="Needs replacement inspection")
    assert cancelled.status == "CANCELLED" and cancelled.maintenance_hold
    assert cancelled.condition_status == request.condition_status
    assert cancelled.availability_status == request.availability_status
    assert cancelled.cancelled_by == service_rows["users"][actor]
    history = call(service.list_maintenance_events, request_id=request.id, limit=100)
    assert history.items[-1].event_type == "CANCELLED" and history.items[-1].from_status == state
    assert_http(409, lambda: call(service.start_repair, "assistant", request_id=request.id, expected_version=cancelled.version))


def test_verification_flushes_request_then_event_then_asset(database, call, awaiting):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(("UPDATE maintenance_requests ", "INSERT INTO maintenance_events ", "UPDATE equipment_assets ")):
            statements.append(statement.split()[1 if statement.startswith("UPDATE") else 2])

    sa.event.listen(database, "before_cursor_execute", record)
    try:
        result = call(service.verify_repair, request_id=awaiting.id, expected_version=awaiting.version,
                      outcome="PASS", notes="Independent inspection")
    finally:
        sa.event.remove(database, "before_cursor_execute", record)
    assert result.status == "COMPLETED" and not result.maintenance_hold
    assert statements == ["maintenance_requests", "maintenance_events", "equipment_assets"]


@pytest.mark.parametrize("after_stage", [1, 2, 3])
def test_unrelated_pending_work_is_never_flushed_or_committed(database, service_rows, awaiting, monkeypatch, after_stage):
    before = snapshot(database)
    extra_id = uuid4()
    unexpected_inserts = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO equipment "):
            unexpected_inserts.append(statement)

    sa.event.listen(database, "before_cursor_execute", record)
    try:
        with Session(database, autoflush=False) as db:
            actor = db.get(User, service_rows["users"]["faculty"])
            flush = db.flush
            count = 0

            def introduce_pending():
                nonlocal count
                flush()
                count += 1
                if count == after_stage:
                    db.add(Equipment(id=extra_id, name="Unrelated", category="IoT",
                                     tracking_type="INDIVIDUAL", default_location="Test lab"))

            monkeypatch.setattr(db, "flush", introduce_pending)
            assert_http(409, lambda: service.verify_repair(db, awaiting.id, actor, awaiting.version,
                                                         "PASS", "Independent inspection"))
            assert count == after_stage
    finally:
        sa.event.remove(database, "before_cursor_execute", record)
    assert not unexpected_inserts
    assert snapshot(database) == before
    with database.connect() as connection:
        assert connection.scalar(sa.select(Equipment.id).where(Equipment.id == extra_id)) is None
