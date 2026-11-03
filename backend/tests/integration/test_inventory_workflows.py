from backend.tests.integration.inventory_helpers import database_now, label_transaction
"""Phase 3 on the dedicated, schema-isolated PostgreSQL database only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import time
from uuid import uuid4

from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services import checkout, maintenance
from backend.tests.integration.test_checkout_postgres import records, api, borrowed_checkout, payload


pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SADeprecationWarning")


def invoke(database, records, operation, role="FACULTY", **kwargs):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        actor = User(id=records["users"][role])
        if operation is checkout.record_return:
            return operation(db, received_by=actor, **kwargs)
        if operation is checkout.create_checkout:
            return operation(db, issued_by=actor, **kwargs)
        return operation(db, actor=actor, **kwargs)


def report(database, records):
    return invoke(database, records, maintenance.report_maintenance_issue, asset_id=records["asset"],
                  issue_type="DAMAGE", issue_description="Original fault while borrowed")


def returning(database, records, loan_id, condition="WORKING", **kwargs):
    return invoke(database, records, checkout.record_return, checkout_id=loan_id,
                  return_data=ReturnCreate(condition_status=condition, **kwargs))


def snapshot(database):
    with database.connect() as connection:
        return tuple(connection.execute(sa.select(table).order_by(table.c.id)).all() for table in (
            EquipmentAsset.__table__, EquipmentCheckout.__table__,
            MaintenanceRequest.__table__, MaintenanceEvent.__table__,
        ))


def assert_conflict(action, code=409):
    with pytest.raises(HTTPException) as error:
        action()
    assert error.value.status_code == code


@pytest.mark.parametrize("condition, issue", [("DAMAGED", "DAMAGE"), ("UNDER_REPAIR", "MALFUNCTION")])
def test_atomic_damaged_return_creates_report_audit_and_preserves_loan(database, records, borrowed_checkout, condition, issue):
    loan_id, original = borrowed_checkout
    result = returning(database, records, loan_id, condition, notes="Fault observed on return")
    with Session(database) as db:
        loan = db.get(EquipmentCheckout, loan_id)
        request = db.get(MaintenanceRequest, result.maintenance_request_id)
        events = db.scalars(sa.select(MaintenanceEvent)).all()
        assert request.status == "OPEN" and request.issue_type == issue and request.version == 1
        assert request.started_at is None and request.reported_at == loan.returned_at == result.returned_at
        assert request.reported_by == records["users"]["FACULTY"]
        assert len(events) == 1 and events[0].event_type == "REPORTED" and events[0].sequence == 1
        assert json.loads(events[0].note.split("\n", 1)[0])["checkout_id"] == str(loan_id)
        assert events[0].occurred_at == loan.returned_at
        assert loan.status == "RETURNED" and loan.notes.startswith(original["notes"])
        for field in ("id", "asset_id", "student_id", "issued_by", "checked_out_at", "due_at"):
            expected = datetime.fromisoformat(original[field]) if field.endswith("_at") else original[field]
            actual = getattr(result, field) if field.endswith("_at") else str(getattr(result, field))
            assert actual == expected
        assert result.maintenance_hold and result.availability_status == "MAINTENANCE"
    before = snapshot(database)
    assert_conflict(lambda: returning(database, records, loan_id, condition))
    assert snapshot(database) == before
    assert_conflict(lambda: invoke(database, records, checkout.create_checkout, asset_id=records["asset"],
                                  student_id=records["users"]["STUDENT"], due_at=result.due_at))


@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED", "UNDER_REPAIR"])
def test_borrowed_report_is_reused_on_return_preserving_report_and_assignment(database, records, borrowed_checkout, condition):
    loan_id, original = borrowed_checkout
    request = report(database, records)
    request = invoke(database, records, maintenance.assign_maintenance_request, request_id=request.id,
                     expected_version=request.version, assignee_id=records["users"]["LAB_ASSISTANT"])
    with Session(database) as db:
        assert db.get(EquipmentCheckout, loan_id).status == "BORROWED"
        before_events = db.execute(sa.select(MaintenanceEvent.__table__).order_by(MaintenanceEvent.sequence)).all()
    result = returning(database, records, loan_id, condition)
    with Session(database) as db:
        row = db.get(MaintenanceRequest, request.id)
        assert row.issue_description == request.issue_description and row.reported_by == request.reported_by
        assert row.reported_at == request.reported_at and row.assigned_to == request.assigned_to
        assert row.status == "OPEN" and row.version == request.version + 1
        events = db.execute(sa.select(MaintenanceEvent.__table__).order_by(MaintenanceEvent.sequence)).all()
        assert events[:-1] == before_events and events[-1].event_type == "RETURN_OBSERVED"
        assert events[-1].sequence == row.version
        assert json.loads(events[-1].note.split("\n", 1)[0])["checkout_id"] == str(loan_id)
        assert db.scalar(sa.select(sa.func.count()).select_from(MaintenanceRequest)) == 1
    assert result.maintenance_hold and result.availability_status == "MAINTENANCE"
    assert result.maintenance_request_id == request.id


def test_return_recovery_requires_explicit_parent_and_full_independent_verification(database, records, borrowed_checkout):
    loan_id, _ = borrowed_checkout
    request = report(database, records)
    cancelled = invoke(database, records, maintenance.cancel_maintenance_request, request_id=request.id,
                       expected_version=request.version, reason="Replace with inspection")
    before = snapshot(database)
    assert_conflict(lambda: returning(database, records, loan_id))
    # Standalone reporting must not bypass the same linked recovery policy.
    assert_conflict(lambda: report(database, records))
    assert snapshot(database) == before
    returned = returning(database, records, loan_id, recovery_of_request_id=cancelled.id)
    with Session(database) as db:
        recovery = db.get(MaintenanceRequest, returned.maintenance_request_id)
        assert recovery.recovery_of_request_id == cancelled.id and recovery.issue_type == "INSPECTION"
        request_id, version = recovery.id, recovery.version
    assigned = invoke(database, records, maintenance.assign_maintenance_request, request_id=request_id,
                      expected_version=version, assignee_id=records["users"]["FACULTY"])
    started = invoke(database, records, maintenance.start_repair, request_id=request_id, expected_version=assigned.version)
    submitted = invoke(database, records, maintenance.submit_repair_for_verification, request_id=request_id,
                       expected_version=started.version, repair_notes="Inspection complete")
    before = snapshot(database)
    assert_conflict(lambda: invoke(database, records, maintenance.verify_repair, request_id=request_id,
                                  expected_version=submitted.version, outcome="PASS", notes="Self verification"), 403)
    assert snapshot(database) == before
    verified = invoke(database, records, maintenance.verify_repair, "ADMIN", request_id=request_id,
                      expected_version=submitted.version, outcome="PASS", notes="Independent bench test")
    assert not verified.maintenance_hold and verified.availability_status == "AVAILABLE"
    with Session(database) as db:
        assert db.get(EquipmentCheckout, loan_id).status == "RETURNED"
        assert db.get(MaintenanceRequest, cancelled.id).status == "CANCELLED"


@pytest.mark.parametrize("state", ["IN_PROGRESS", "AWAITING_VERIFICATION"])
def test_inconsistent_advanced_request_with_loan_rejected_without_changes(database, records, borrowed_checkout, state):
    loan_id, _ = borrowed_checkout
    request = report(database, records)
    # Model constraints allow this imported/inconsistent custody state. Construct
    # it with matching audit/version; service policy must reject reconciliation.
    with Session(database) as db:
        db.scalar(sa.select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        row = db.get(MaintenanceRequest, request.id)
        row.status, row.assigned_to, row.started_at = state, records["users"]["LAB_ASSISTANT"], request.reported_at
        row.version += 1
        if state == "AWAITING_VERIFICATION":
            row.repair_completed_at, row.repair_completed_by = request.reported_at, records["users"]["LAB_ASSISTANT"]
        db.add(MaintenanceEvent(id=uuid4(), request_id=row.id, sequence=row.version,
                                event_type="NOTE_ADDED", actor_id=records["users"]["FACULTY"], note="Imported inconsistent custody"))
        db.commit()
    before = snapshot(database)
    assert_conflict(lambda: returning(database, records, loan_id, "DAMAGED"))
    assert snapshot(database) == before


def test_checkout_refreshes_stale_asset_and_return_refreshes_stale_hold(database, records, api):
    with Session(database, autoflush=False, expire_on_commit=False) as stale:
        asset = stale.get(EquipmentAsset, records["asset"])
        staff = stale.get(User, records["users"]["FACULTY"])
        stale.commit()  # End this test-owned read transaction; retain cached state.
        report(database, records)
        assert not asset.maintenance_hold  # The identity map is intentionally stale.
        assert_conflict(lambda: checkout.create_checkout(stale, asset.id, records["users"]["STUDENT"], staff,
                                                         database_now(database) + timedelta(days=1)))


def test_return_refreshes_cached_loan_and_maintenance_state(database, records, borrowed_checkout):
    loan_id, _ = borrowed_checkout
    with Session(database, autoflush=False, expire_on_commit=False) as stale:
        asset = stale.get(EquipmentAsset, records["asset"])
        loan = stale.get(EquipmentCheckout, loan_id)
        staff = stale.get(User, records["users"]["FACULTY"])
        stale.commit()  # End this test-owned read transaction; retain cached state.
        request = report(database, records)
        assert not asset.maintenance_hold
        result = checkout.record_return(stale, loan_id, staff, ReturnCreate(condition_status="WORKING"))
        assert result.maintenance_hold and result.maintenance_request_id == request.id
        assert loan.status == "RETURNED"


@pytest.mark.parametrize("operation", ["checkout", "return"])
@pytest.mark.parametrize("role, active", [("STUDENT", True), ("FACULTY", False)])
def test_persisted_staff_rechecks_override_cached_roles(database, records, api, operation, role, active):
    loan_id = None
    if operation == "return":
        from uuid import UUID
        status, result = api(payload(records))
        assert status == 201
        loan_id = UUID(result["id"])
    with Session(database, autoflush=False, expire_on_commit=False) as stale:
        staff = stale.get(User, records["users"]["FACULTY"])
        stale.commit()  # End this test-owned read transaction; retain cached state.
        with Session(database) as other:
            user = other.get(User, staff.id)
            user.role, user.is_active = role, active
            other.commit()
        before = snapshot(database)
        if operation == "return":
            assert_conflict(lambda: checkout.record_return(stale, loan_id, staff, ReturnCreate(condition_status="DAMAGED")), 403)
        else:
            assert_conflict(lambda: checkout.create_checkout(stale, records["asset"], records["users"]["STUDENT"], staff,
                                                             database_now(database) + timedelta(days=1)), 403)
        assert snapshot(database) == before


@pytest.mark.parametrize("stage", [1, 2, 3, 4, "commit", "response", "event_fk"])
@pytest.mark.parametrize("reuse", [False, True])
def test_integrated_return_failures_leave_no_partial_database_changes(database, records, borrowed_checkout, monkeypatch, stage, reuse):
    loan_id, _ = borrowed_checkout
    if reuse:
        report(database, records)
    before = snapshot(database)
    with Session(database, autoflush=False) as db:
        staff = User(id=records["users"]["FACULTY"])
        if type(stage) is int:
            original, count = db.flush, 0
            def failing_flush():
                nonlocal count
                original()
                count += 1
                if count == stage:
                    raise RuntimeError("Injected post-flush failure")
            monkeypatch.setattr(db, "flush", failing_flush)
        elif stage == "commit":
            sa.event.listen(db, "before_commit", lambda session: (_ for _ in ()).throw(RuntimeError("Injected commit failure")))
        elif stage == "response":
            monkeypatch.setattr(checkout, "ReturnResponse", lambda **kw: (_ for _ in ()).throw(ValueError("Invalid response")))
        else:
            def corrupt_event(session, context, instances):
                for obj in session.new:
                    if isinstance(obj, MaintenanceEvent):
                        obj.actor_id = uuid4()  # Real FK failure after checkout/asset writes.
            sa.event.listen(db, "before_flush", corrupt_event)
        with pytest.raises(HTTPException if stage == "event_fk" else (RuntimeError, ValueError)):
            checkout.record_return(db, loan_id, staff, ReturnCreate(condition_status="DAMAGED"))
    assert snapshot(database) == before


@pytest.mark.parametrize("after_stage", [1, 2, 3, 4])
def test_integrated_return_never_flushes_unrelated_pending_objects(database, records, borrowed_checkout, monkeypatch, after_stage):
    loan_id, _ = borrowed_checkout
    before = snapshot(database)
    with Session(database, autoflush=False) as db:
        staff = User(id=records["users"]["FACULTY"])
        original, count = db.flush, 0
        def injecting_flush():
            nonlocal count
            original()
            count += 1
            if count == after_stage:
                db.add(User(id=uuid4(), college_id="UNEXPECTED", full_name="Must not persist",
                            email="unexpected@example.invalid", role="STUDENT", is_active=True))
        monkeypatch.setattr(db, "flush", injecting_flush)
        assert_conflict(lambda: checkout.record_return(db, loan_id, staff, ReturnCreate(condition_status="DAMAGED")))
        assert count == after_stage
    assert snapshot(database) == before
    with Session(database) as db:
        assert db.scalar(sa.select(User.id).where(User.college_id == "UNEXPECTED")) is None


def race(database, asset_id, commands):
    """Require PostgreSQL to observe both workers blocked on the same asset."""
    label = "phase3_race_" + uuid4().hex
    def worker(command):
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            label_transaction(db, label)
            try:
                return 200, command(db)
            except HTTPException as error:
                return error.status_code, None
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(sa.select(EquipmentAsset).where(EquipmentAsset.id == asset_id).with_for_update())
        futures = [pool.submit(worker, command) for command in commands]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Both Phase 3 workers must wait on the PostgreSQL asset lock")
        finally:
            transaction.rollback()
        return [future.result(timeout=12) for future in futures]


@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED", "UNDER_REPAIR"])
@pytest.mark.parametrize("operation", ["report", "assign", "cancel", "return", "verify"])
def test_return_races_preserve_custody_versions_and_audit(database, records, borrowed_checkout, condition, operation):
    loan_id, _ = borrowed_checkout
    request = None if operation in {"report", "return"} else report(database, records)
    staff_id = records["users"]["FACULTY"]
    def ret(db):
        return checkout.record_return(db, loan_id, User(id=staff_id), ReturnCreate(condition_status=condition))
    def other(db):
        staff = User(id=staff_id)
        if operation == "report":
            return maintenance.report_maintenance_issue(db, records["asset"], staff, "DAMAGE", "Concurrent report")
        if operation == "return":
            return ret(db)
        if operation == "assign":
            return maintenance.assign_maintenance_request(db, request.id, staff, records["users"]["LAB_ASSISTANT"], request.version)
        if operation == "verify":
            # Verification of an OPEN borrowed request must fail regardless of
            # who wins; it cannot release custody/hold by racing the return.
            return maintenance.verify_repair(db, request.id, staff, request.version, "PASS", "Attempted premature verification")
        return maintenance.cancel_maintenance_request(db, request.id, staff, request.version, "Concurrent cancellation")
    results = race(database, records["asset"], [ret, other])
    assert all(code in {200, 409} for code, _ in results)
    assert any(code == 200 for code, _ in results)
    if operation == "verify":
        assert [code for code, _ in results] == [200, 409]
    with Session(database) as db:
        loan, asset = db.get(EquipmentCheckout, loan_id), db.get(EquipmentAsset, records["asset"])
        requests = db.scalars(sa.select(MaintenanceRequest)).all()
        assert len([r for r in requests if r.status in maintenance.ACTIVE]) <= 1
        assert (loan.status == "BORROWED") == (asset.availability_status == "BORROWED")
        if loan.status == "RETURNED":
            assert loan.notes.count("[RETURN]") == 1
        for row in requests:
            events = db.scalars(sa.select(MaintenanceEvent).where(MaintenanceEvent.request_id == row.id)
                                .order_by(MaintenanceEvent.sequence)).all()
            assert [e.sequence for e in events] == list(range(1, row.version + 1))
            assert sum(e.event_type == "RETURN_OBSERVED" for e in events) <= 1
        if asset.maintenance_hold:
            assert asset.availability_status != "AVAILABLE"


def test_checkout_report_race_preserves_winning_checkout_or_blocks_it(database, records):
    def borrow(db):
        staff = User(id=records["users"]["FACULTY"])
        return checkout.create_checkout(db, records["asset"], records["users"]["STUDENT"], staff,
                                        database_now(database) + timedelta(days=1))
    def fault(db):
        return maintenance.report_maintenance_issue(db, records["asset"], User(id=records["users"]["FACULTY"]), "DAMAGE", "Fault")
    results = race(database, records["asset"], [borrow, fault])
    assert results[1][0] == 200 and results[0][0] in {200, 409}
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        loans = db.scalars(sa.select(EquipmentCheckout)).all()
        assert asset.maintenance_hold and asset.condition_status == "DAMAGED"
        assert len(loans) == (1 if results[0][0] == 200 else 0)
        assert asset.availability_status == ("BORROWED" if loans else "MAINTENANCE")


def test_checkout_deadline_expiring_during_asset_lock_wait_rolls_back(database, records):
    label = "phase3_deadline_" + uuid4().hex
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        tx = blocker.begin()
        blocker.execute(sa.select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        due = blocker.scalar(sa.select(sa.func.clock_timestamp())) + timedelta(milliseconds=400)
        def borrow():
            with Session(database, autoflush=False) as db:
                label_transaction(db, label)
                assert_conflict(lambda: checkout.create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                                                 User(id=records["users"]["FACULTY"]), due), 400)
        future = pool.submit(borrow)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = :label "
                        "AND datname = current_database() AND wait_event_type = 'Lock'"), {"label": label})
                    expired = observer.scalar(sa.select(sa.func.clock_timestamp())) >= due
                if waiting == 1 and expired:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Checkout must be waiting after deadline expires")
        finally:
            tx.rollback()
        future.result(timeout=12)
    with Session(database) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 0
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "AVAILABLE"


def ordered_pair(database, asset_id, winner, waiter):
    """Force both lock orders inside roots begun by the inventory services."""
    label = "phase3_ordered_" + uuid4().hex
    def waiting_operation():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            label_transaction(db, label)
            try:
                return 200, waiter(db)
            except HTTPException as error:
                return error.status_code, None
    futures = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            def coordinate_owned_root(session, root, connection):
                connection.execute(sa.select(EquipmentAsset).where(EquipmentAsset.id == asset_id).with_for_update())
                futures.append(pool.submit(waiting_operation))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with database.connect() as observer:
                        waiting = observer.scalar(sa.text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND application_name = :label AND wait_event_type = 'Lock'"), {"label": label})
                    if waiting == 1:
                        return
                    time.sleep(0.05)
                pytest.fail("Expected competing service to wait on the asset lock")
            sa.event.listen(db, "after_begin", coordinate_owned_root)
            result = winner(db)
        return result, futures[0].result(timeout=12)


@pytest.mark.parametrize("winner", ["checkout", "report"])
def test_checkout_report_both_lock_orders(database, records, winner):
    def borrow(db):
        return checkout.create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                        User(id=records["users"]["FACULTY"]),
                                        database_now(database) + timedelta(days=1))
    def fault(db):
        return maintenance.report_maintenance_issue(db, records["asset"], User(id=records["users"]["FACULTY"]), "DAMAGE", "Ordered fault")
    first, second = ordered_pair(database, records["asset"], borrow if winner == "checkout" else fault,
                                 fault if winner == "checkout" else borrow)
    assert second[0] == (200 if winner == "checkout" else 409)
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.maintenance_hold
        assert asset.availability_status == ("BORROWED" if winner == "checkout" else "MAINTENANCE")
        assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == (winner == "checkout")


@pytest.mark.parametrize("winner", ["return", "report"])
@pytest.mark.parametrize("condition", ["WORKING", "DAMAGED"])
def test_return_report_both_lock_orders(database, records, borrowed_checkout, winner, condition):
    loan_id, _ = borrowed_checkout
    def ret(db):
        return checkout.record_return(db, loan_id, User(id=records["users"]["FACULTY"]), ReturnCreate(condition_status=condition))
    def fault(db):
        return maintenance.report_maintenance_issue(db, records["asset"], User(id=records["users"]["FACULTY"]), "DAMAGE", "Ordered report")
    first, second = ordered_pair(database, records["asset"], ret if winner == "return" else fault,
                                 fault if winner == "return" else ret)
    assert second[0] == (409 if winner == "return" and condition == "DAMAGED" else 200)
    with Session(database) as db:
        assert db.get(EquipmentCheckout, loan_id).status == "RETURNED"
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.maintenance_hold and asset.availability_status == "MAINTENANCE"
        requests = db.scalars(sa.select(MaintenanceRequest)).all()
        assert len(requests) == 1
        events = db.scalars(sa.select(MaintenanceEvent).order_by(MaintenanceEvent.sequence)).all()
        assert [e.sequence for e in events] == list(range(1, requests[0].version + 1))
        assert [e.event_type for e in events] == (["REPORTED", "RETURN_OBSERVED"] if winner == "report" else ["REPORTED"])


@pytest.mark.parametrize("statement", ["UPDATE maintenance_events SET note = 'rewrite'", "DELETE FROM maintenance_events"])
def test_return_maintenance_event_is_immutable(database, records, borrowed_checkout, statement):
    returning(database, records, borrowed_checkout[0], "DAMAGED")
    before = snapshot(database)
    with database.connect() as connection:
        tx = connection.begin()
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.text(statement))
        assert error.value.orig.sqlstate == "55000"
        tx.rollback()
    assert snapshot(database) == before


@pytest.mark.parametrize("reuse", [False, True])
def test_return_sql_stages_preserve_checkout_asset_request_event_order(database, records, borrowed_checkout, reuse):
    if reuse:
        report(database, records)
    writes = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        words = statement.strip().split()
        if words and words[0].upper() in {"UPDATE", "INSERT"}:
            writes.append(" ".join(words[:3]).lower())
    sa.event.listen(database, "before_cursor_execute", capture)
    try:
        returning(database, records, borrowed_checkout[0], "DAMAGED")
    finally:
        sa.event.remove(database, "before_cursor_execute", capture)
    assert len(writes) == 4
    assert writes[0].startswith("update equipment_checkouts")
    assert writes[1].startswith("update equipment_assets")
    assert writes[2].startswith("update maintenance_requests" if reuse else "insert into maintenance_requests")
    assert writes[3].startswith("insert into maintenance_events")
