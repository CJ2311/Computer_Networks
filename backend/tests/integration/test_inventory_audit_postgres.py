"""NFC audit integration tests on identity-verified, disposable test schemas."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib
import json
import time
from unittest.mock import Mock
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.database import Base, get_db, get_inventory_write_db
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset, EquipmentStock
from backend.app.models.inventory_audit import (
    InventoryAudit, InventoryAuditExpectedAsset, InventoryAuditScan, InventoryAuditDiscrepancy,
)
from backend.app.models.maintenance import MaintenanceRequest, MaintenanceEvent
from backend.app.models.metadata_audit import MetadataAuditEvent
from backend.app.models.user import User
from backend.app.routers.inventory_audit import router
from backend.app.schemas.asset import AssetUpdate
from backend.app.schemas.inventory_audit import (
    InventoryAuditCreate, InventoryAuditScanCreate, InventoryAuditFinalize, InventoryAuditCancel,
)
from backend.app.services import inventory_audit as service
from backend.app.services.asset import update_asset
from backend.app.services.checkout import create_checkout, record_return
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.maintenance import report_maintenance_issue
from backend.tests.integration.test_checkout_postgres import records
from backend.tests.integration.inventory_helpers import database_now, label_transaction


@pytest.fixture
def lab(database, records):
    actor = User(id=records["users"]["FACULTY"])
    with Session(database) as db:
        update_asset(db, records["asset"], AssetUpdate(nfc_tag_id="TAG-1"), actor)
    with Session(database) as db:
        equipment_id = db.get(EquipmentAsset, records["asset"]).equipment_id
        outside = EquipmentAsset(id=uuid4(), equipment_id=equipment_id, asset_code="OUTSIDE",
                                 location="Other lab", nfc_tag_id="TAG-OUTSIDE")
        untagged = EquipmentAsset(id=uuid4(), equipment_id=equipment_id, asset_code="UNTAGGED",
                                  location="Test lab")
        quantity = Equipment(id=uuid4(), name="Quantity stock", category="IoT",
                             tracking_type="QUANTITY", default_location="Test lab")
        db.add_all([outside, untagged, quantity])
        db.flush()
        db.add(EquipmentStock(equipment_id=quantity.id, quantity=17))
        db.commit()
        return records | {"outside": outside.id, "untagged": untagged.id, "quantity": quantity.id,
                          "equipment": equipment_id}


def invoke(database, lab, action, *args, role="FACULTY", db=None):
    actor = User(id=lab["users"][role])
    if db is not None:
        return action(db, *args, actor)
    with Session(database, autoflush=False) as session:
        return action(session, *args, actor)


def create(database, lab, **changes):
    return invoke(database, lab, service.create_inventory_audit,
                  InventoryAuditCreate(name="Weekly NFC audit", location="Test lab", **changes))


def scan_data(tag="TAG-1", key=None, location="Test lab"):
    return InventoryAuditScanCreate(idempotency_key=key or uuid4(), nfc_tag_id=tag, observed_location=location)


def report(database, lab, audit_id, **pagination):
    with Session(database) as db:
        return service.get_inventory_audit_report(db, audit_id, User(id=lab["users"]["FACULTY"]), **pagination)


def current(database, lab, audit_id):
    with Session(database) as db:
        return service.get_inventory_audit(db, audit_id, User(id=lab["users"]["FACULTY"]))


def operational_state(database):
    with database.connect() as connection:
        return tuple(connection.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key)).all()
                     for model in (Equipment, EquipmentAsset, EquipmentStock, EquipmentCheckout,
                                   MaintenanceRequest, MaintenanceEvent, MetadataAuditEvent))


def expect_error(connection, statement, *states):
    with pytest.raises(sa.exc.DBAPIError) as error:
        with connection.begin_nested():
            connection.execute(statement)
    assert error.value.orig.sqlstate in states


def test_schema_matches_models_and_indexes(database):
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, Base.metadata) == []
        indexes = {index["name"]: index for index in sa.inspect(connection).get_indexes("inventory_audit_scans")}
        assert indexes["uq_inventory_scan_first_tag"]["unique"]
        assert "duplicate_of_scan_id IS NULL" in indexes["uq_inventory_scan_first_tag"]["dialect_options"]["postgresql_where"]
        assert all(key["options"]["ondelete"] == "RESTRICT" for key in
                   sa.inspect(connection).get_foreign_keys("inventory_audit_discrepancies"))


def test_an_empty_inventory_can_be_audited(database):
    actor_id = uuid4()
    actor = User(id=actor_id, college_id="EMPTY-FACULTY", full_name="Faculty",
                 email="empty@example.invalid", role="FACULTY", is_active=True)
    with Session(database) as db:
        db.add(actor)
        db.commit()
    with Session(database) as db:
        audit = service.create_inventory_audit(db, InventoryAuditCreate(name="Empty", location="Empty lab"),
                                               User(id=actor_id))
    with Session(database) as db:
        summary = service.get_inventory_audit_report(db, audit.id, User(id=actor_id))
        assert summary.expected_count == 0 and summary.findings == []
    with Session(database) as db:
        done = service.finalize_inventory_audit(db, audit.id, InventoryAuditFinalize(expected_version=2),
                                               User(id=actor_id))
        assert done.status == "COMPLETED"


def test_lifecycle_snapshot_retries_duplicates_and_historical_report(database, lab):
    before = operational_state(database)
    audit = create(database, lab)
    assert audit.snapshot_sealed and audit.version == 2 and audit.status == "IN_PROGRESS"
    with Session(database) as db:
        rows = db.scalars(sa.select(InventoryAuditExpectedAsset).where(
            InventoryAuditExpectedAsset.audit_id == audit.id,
        )).all()
        assert len(rows) == 3 and sum(row.in_scope for row in rows) == 2
    data = scan_data()
    first = invoke(database, lab, service.record_inventory_scan, audit.id, data)
    replay = invoke(database, lab, service.record_inventory_scan, audit.id, data)
    duplicate = invoke(database, lab, service.record_inventory_scan, audit.id, scan_data())
    assert first.result == "MATCHED" and first.id == replay.id
    assert duplicate.result == "DUPLICATE" and duplicate.duplicate_of_scan_id == first.id
    preview = report(database, lab, audit.id)
    assert preview.preview and preview.verified_count == 1 and preview.scan_count == 2 and preview.duplicate_count == 1
    assert [finding.discrepancy_type for finding in preview.findings] == ["NOT_VERIFIED"]
    done = invoke(database, lab, service.finalize_inventory_audit, audit.id,
                  InventoryAuditFinalize(expected_version=preview.audit.version, notes="Faculty reviewed."))
    assert done.status == "COMPLETED" and done.completed_at.tzinfo is not None
    historical = report(database, lab, audit.id)
    assert not historical.preview and historical.reconciliation_complete
    assert historical.findings == preview.findings
    assert invoke(database, lab, service.record_inventory_scan, audit.id, data).id == first.id
    with pytest.raises(HTTPException) as error:
        invoke(database, lab, service.record_inventory_scan, audit.id, scan_data())
    assert error.value.status_code == 409
    assert operational_state(database) == before
    # Later inventory edits cannot change the closed report.
    invoke(database, lab, update_asset, lab["asset"], AssetUpdate(nfc_tag_id="NEW-TAG", asset_code="NEW-CODE"))
    assert report(database, lab, audit.id) == historical


@pytest.mark.parametrize("tag,location,result", [
    ("UNKNOWN", "Test lab", "UNKNOWN_TAG"), ("TAG-OUTSIDE", "Test lab", "UNEXPECTED"),
    ("TAG-1", "Wrong lab", "MISPLACED"),
])
def test_server_classification_and_findings(database, lab, tag, location, result):
    audit = create(database, lab)
    first = invoke(database, lab, service.record_inventory_scan, audit.id, scan_data(tag=tag, location=location))
    assert first.result == result
    second = invoke(database, lab, service.record_inventory_scan, audit.id, scan_data(tag=tag, location=location))
    assert second.result == "DUPLICATE"
    if result == "UNKNOWN_TAG":
        assert first.asset_id is None and second.asset_id is None
    preview = report(database, lab, audit.id)
    findings = [finding for finding in preview.findings if finding.scan_id == first.id]
    assert len(findings) == 1 and findings[0].discrepancy_type == result
    invoke(database, lab, service.finalize_inventory_audit, audit.id,
           InventoryAuditFinalize(expected_version=preview.audit.version))
    assert any(finding.scan_id == first.id for finding in report(database, lab, audit.id).findings)


@pytest.mark.parametrize("difference", ["tag", "location", "actor"])
def test_idempotency_key_cannot_be_reused_for_different_payload(database, lab, difference):
    audit = create(database, lab)
    data = scan_data()
    first = invoke(database, lab, service.record_inventory_scan, audit.id, data)
    changed = data.model_copy(update={"nfc_tag_id": "OTHER"}) if difference == "tag" else data
    if difference == "location":
        changed = data.model_copy(update={"observed_location": "Other lab"})
    with pytest.raises(HTTPException) as error:
        invoke(database, lab, service.record_inventory_scan, audit.id, changed,
               role="LAB_ASSISTANT" if difference == "actor" else "FACULTY")
    assert error.value.status_code == 409
    assert report(database, lab, audit.id).scan_count == 1
    assert invoke(database, lab, service.record_inventory_scan, audit.id, data).id == first.id


@pytest.mark.parametrize("custody", ["checkout", "maintenance"])
@pytest.mark.parametrize("at_start", [False, True])
def test_custody_and_maintenance_changes_are_explicit(database, lab, custody, at_start):
    def start_workflow():
        with Session(database) as db:
            actor = User(id=lab["users"]["FACULTY"])
            if custody == "checkout":
                return create_checkout(db, lab["asset"], lab["users"]["STUDENT"], actor,
                                       database_now(database) + timedelta(days=1))
            return report_maintenance_issue(db, lab["asset"], actor, "DAMAGE", "Fault")
    if at_start:
        start_workflow()
    audit = create(database, lab)
    if not at_start:
        start_workflow()
    before = operational_state(database)
    preview = report(database, lab, audit.id)
    types = {finding.discrepancy_type for finding in preview.findings if finding.asset_id == lab["asset"]}
    if at_start:
        assert ("EXPECTED_ON_LOAN" if custody == "checkout" else "EXPECTED_MAINTENANCE") in types
    else:
        assert types == {"NOT_VERIFIED", "STATE_CHANGED"}
    invoke(database, lab, service.record_inventory_scan, audit.id, scan_data())
    preview = report(database, lab, audit.id)
    assert "CUSTODY_REVIEW" in {finding.discrepancy_type for finding in preview.findings}
    invoke(database, lab, service.finalize_inventory_audit, audit.id,
           InventoryAuditFinalize(expected_version=preview.audit.version))
    assert operational_state(database) == before


def test_snapshot_remains_consistent_when_inventory_changes_before_persistence(database, lab):
    added = uuid4()
    fired = False
    def after_snapshot(connection, cursor, statement, parameters, context, executemany):
        nonlocal fired
        if not fired and "statement_timestamp()" in statement and "LEFT OUTER JOIN" in statement:
            fired = True
            with Session(database) as db:
                db.add(EquipmentAsset(id=added, equipment_id=lab["equipment"], asset_code="LATER",
                                     location="Test lab", nfc_tag_id="LATER-TAG"))
                db.commit()
    sa.event.listen(database, "after_cursor_execute", after_snapshot)
    try:
        audit = create(database, lab)
    finally:
        sa.event.remove(database, "after_cursor_execute", after_snapshot)
    assert fired
    with Session(database) as db:
        assert db.scalar(sa.select(InventoryAuditExpectedAsset).where(
            InventoryAuditExpectedAsset.audit_id == audit.id, InventoryAuditExpectedAsset.asset_id == added,
        )) is None
    assert invoke(database, lab, service.record_inventory_scan, audit.id, scan_data("LATER-TAG")).result == "UNKNOWN_TAG"


def test_identifier_mapping_is_frozen_and_drift_is_reported(database, lab):
    audit = create(database, lab)
    invoke(database, lab, update_asset, lab["asset"], AssetUpdate(nfc_tag_id="CHANGED"))
    assert invoke(database, lab, service.record_inventory_scan, audit.id, scan_data()).asset_id == lab["asset"]
    assert invoke(database, lab, service.record_inventory_scan, audit.id, scan_data("CHANGED")).result == "UNKNOWN_TAG"
    assert "STATE_CHANGED" in {finding.discrepancy_type for finding in report(database, lab, audit.id).findings}


def test_checkout_after_reconciliation_observation_does_not_deadlock_or_rewrite_report(database, lab):
    audit = create(database, lab)
    loan = None
    def after_observation(connection, cursor, statement, parameters, context, executemany):
        nonlocal loan
        if loan is None and "statement_timestamp()" in statement and "LEFT OUTER JOIN" in statement:
            with Session(database) as db:
                loan = create_checkout(db, lab["asset"], lab["users"]["STUDENT"],
                                       User(id=lab["users"]["FACULTY"]),
                                       database_now(database) + timedelta(days=1))
    sa.event.listen(database, "after_cursor_execute", after_observation)
    try:
        done = invoke(database, lab, service.finalize_inventory_audit, audit.id, InventoryAuditFinalize(expected_version=2))
    finally:
        sa.event.remove(database, "after_cursor_execute", after_observation)
    assert loan is not None and done.reconciled_at <= loan.checked_out_at <= done.completed_at
    saved = report(database, lab, audit.id)
    assert {finding.discrepancy_type for finding in saved.findings} == {"NOT_VERIFIED"}
    with Session(database) as db:
        assert db.get(EquipmentAsset, lab["asset"]).availability_status == "BORROWED"
        assert db.get(EquipmentCheckout, loan.id).status == "BORROWED"


def test_quantity_assets_are_excluded_even_if_legacy_data_contains_one(database, lab):
    with Session(database) as db:
        db.add(EquipmentAsset(equipment_id=lab["quantity"], asset_code="QUANTITY-LEGACY",
                              nfc_tag_id="QTY-TAG", location="Test lab"))
        db.commit()
    audit = create(database, lab)
    assert report(database, lab, audit.id).expected_count == 2
    assert invoke(database, lab, service.record_inventory_scan, audit.id, scan_data("QTY-TAG")).result == "UNKNOWN_TAG"


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
def test_staff_can_create_and_scan(database, lab, role):
    audit = invoke(database, lab, service.create_inventory_audit, InventoryAuditCreate(name="N", location="Test lab"), role=role)
    assert invoke(database, lab, service.record_inventory_scan, audit.id, scan_data(), role=role).result == "MATCHED"


@pytest.mark.parametrize("operation", ["create", "scan", "finalize", "cancel", "report"])
@pytest.mark.parametrize("invalid", ["student", "inactive", "unknown"])
def test_service_revalidates_persisted_actor(database, lab, operation, invalid):
    audit = create(database, lab)
    actor_id = lab["users"]["STUDENT"] if invalid == "student" else lab["users"]["FACULTY"]
    if invalid == "inactive":
        with Session(database) as db:
            db.get(User, actor_id).is_active = False
            db.commit()
    elif invalid == "unknown":
        actor_id = uuid4()
    forged = User(id=actor_id, role="FACULTY", is_active=True)
    with Session(database) as db:
        commands = {
            "create": lambda: service.create_inventory_audit(db, InventoryAuditCreate(name="N", location="Test lab"), forged),
            "scan": lambda: service.record_inventory_scan(db, audit.id, scan_data(), forged),
            "finalize": lambda: service.finalize_inventory_audit(db, audit.id, InventoryAuditFinalize(expected_version=2), forged),
            "cancel": lambda: service.cancel_inventory_audit(db, audit.id, InventoryAuditCancel(expected_version=2, reason="X"), forged),
            "report": lambda: service.get_inventory_audit_report(db, audit.id, forged),
        }
        with pytest.raises(HTTPException) as error:
            commands[operation]()
        assert error.value.status_code == 403


@pytest.mark.parametrize("role", ["ADMIN", "LAB_ASSISTANT"])
def test_only_faculty_can_finalize(database, lab, role):
    audit = create(database, lab)
    with pytest.raises(HTTPException) as error:
        invoke(database, lab, service.finalize_inventory_audit, audit.id, InventoryAuditFinalize(expected_version=2), role=role)
    assert error.value.status_code == 403 and current(database, lab, audit.id).status == "IN_PROGRESS"


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY"])
def test_cancellation_retains_history_and_replay(database, lab, role):
    audit = create(database, lab)
    data = scan_data()
    scan = invoke(database, lab, service.record_inventory_scan, audit.id, data)
    state = current(database, lab, audit.id)
    cancelled = invoke(database, lab, service.cancel_inventory_audit, audit.id,
                       InventoryAuditCancel(expected_version=state.version, reason="Room unavailable"), role=role)
    assert cancelled.status == "CANCELLED" and cancelled.cancelled_by == lab["users"][role]
    assert invoke(database, lab, service.record_inventory_scan, audit.id, data).id == scan.id
    closed = report(database, lab, audit.id)
    assert not closed.reconciliation_complete and closed.scan_count == 1 and closed.expected_count == 2
    with pytest.raises(HTTPException) as error:
        invoke(database, lab, service.finalize_inventory_audit, audit.id, InventoryAuditFinalize(expected_version=cancelled.version))
    assert error.value.status_code == 409


@pytest.mark.parametrize("operation", ["finalize", "cancel"])
def test_stale_version_is_rejected(database, lab, operation):
    audit = create(database, lab)
    invoke(database, lab, service.record_inventory_scan, audit.id, scan_data())
    with pytest.raises(HTTPException) as error:
        if operation == "finalize":
            invoke(database, lab, service.finalize_inventory_audit, audit.id, InventoryAuditFinalize(expected_version=audit.version))
        else:
            invoke(database, lab, service.cancel_inventory_audit, audit.id,
                   InventoryAuditCancel(expected_version=audit.version, reason="X"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("operation", ["scan", "finalize", "cancel"])
def test_staff_deactivation_during_lock_wait_is_rechecked(database, lab, operation):
    audit = create(database, lab)
    label = "audit_actor_wait_" + uuid4().hex
    def worker():
        with Session(database) as db:
            label_transaction(db, label)
            commands = {
                "scan": lambda: invoke(database, lab, service.record_inventory_scan, audit.id, scan_data(), db=db),
                "finalize": lambda: invoke(database, lab, service.finalize_inventory_audit, audit.id,
                                          InventoryAuditFinalize(expected_version=2), db=db),
                "cancel": lambda: invoke(database, lab, service.cancel_inventory_audit, audit.id,
                                        InventoryAuditCancel(expected_version=2, reason="X"), db=db),
            }
            with pytest.raises(HTTPException) as error:
                commands[operation]()
            assert error.value.status_code == 403
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        root = blocker.begin()
        blocker.execute(sa.select(InventoryAudit).where(InventoryAudit.id == audit.id).with_for_update())
        future = pool.submit(worker)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND application_name=:label AND wait_event_type='Lock'"), {"label": label})
                if waiting == 1:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Audit writer must wait for the parent lock")
            with Session(database) as db:
                db.get(User, lab["users"]["FACULTY"]).is_active = False
                db.commit()
        finally:
            root.rollback()
        future.result(timeout=12)
    with Session(database) as db:
        assert db.get(InventoryAudit, audit.id).status == "IN_PROGRESS"
        assert db.scalar(sa.select(sa.func.count()).select_from(InventoryAuditScan)) == 0


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_does_not_promise_rollback(database, lab, committed, monkeypatch):
    from sqlalchemy.orm import SessionTransaction
    commit = SessionTransaction.commit
    def uncertain(root, *args, **kwargs):
        if root.parent is None:
            if committed:
                commit(root, *args, **kwargs)
            raise sa.exc.OperationalError("Sensitive SQL", {}, Exception("Private connection information"))
        return commit(root, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(SessionTransaction, "commit", uncertain)
        with pytest.raises(HTTPException) as error:
            create(database, lab)
        assert error.value.status_code == 503
        assert "could not be confirmed" in error.value.detail
        assert "Sensitive" not in error.value.detail and "Private" not in error.value.detail
    with Session(database) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(InventoryAudit)) == int(committed)


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "CANCELLED"])
def test_closed_records_cannot_be_changed_or_extended(database, lab, terminal_status):
    audit = create(database, lab)
    scan = invoke(database, lab, service.record_inventory_scan, audit.id, scan_data())
    if terminal_status == "COMPLETED":
        done = invoke(database, lab, service.finalize_inventory_audit, audit.id, InventoryAuditFinalize(expected_version=3))
    else:
        # Cancellation does not create findings. Seed a valid preview finding
        # while open so UPDATE/DELETE exercise populated discrepancy history.
        preview = report(database, lab, audit.id)
        finding, = preview.findings
        with database.begin() as connection:
            connection.execute(sa.insert(InventoryAuditDiscrepancy).values(
                id=uuid4(), audit_id=audit.id, detected_at=preview.observed_at, **finding.model_dump(),
            ))
        done = invoke(database, lab, service.cancel_inventory_audit, audit.id,
                      InventoryAuditCancel(expected_version=3, reason="Room unavailable"))
    assert done.status == terminal_status
    with database.begin() as connection:
        models = (InventoryAudit, InventoryAuditExpectedAsset, InventoryAuditScan, InventoryAuditDiscrepancy)
        def history():
            return tuple(connection.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key)).all()
                         for model in models)
        before = history()
        assert all(before), "Every protected table must contain rows"
        for model, changes in (
            (InventoryAudit, {"final_notes": "Rewritten"}),
            (InventoryAuditExpectedAsset, {"asset_code_at_start": "Rewritten"}),
            (InventoryAuditScan, {"result": "MISPLACED"}),
            (InventoryAuditDiscrepancy, {"description": "Rewritten"}),
        ):
            expect_error(connection, sa.update(model).values(**changes), "55000")
            assert history() == before
            expect_error(connection, sa.delete(model), "55000")
            assert history() == before
            # Individual tables can be rejected by FK dependencies before
            # their no-truncate trigger executes.
            expect_error(connection, sa.text(f"TRUNCATE {model.__tablename__}"), "55000", "0A000")
            assert history() == before
        # Include the entire dependency closure to require trigger rejection.
        expect_error(connection, sa.text(
            "TRUNCATE inventory_audits, inventory_audit_expected_assets, "
            "inventory_audit_scans, inventory_audit_discrepancies"
        ), "55000")
        assert history() == before
        expect_error(connection, sa.insert(InventoryAuditScan).values(
            id=uuid4(), audit_id=done.id, idempotency_key=uuid4(), nfc_tag_id="UNKNOWN",
            scanned_by=lab["users"]["FACULTY"], observed_location="Test lab", result="UNKNOWN_TAG",
        ), "55000")
        assert history() == before
        assert connection.scalar(sa.select(InventoryAuditScan.id)) == scan.id


def test_cross_audit_and_scan_asset_references_are_rejected(database, lab):
    first, other = create(database, lab), create(database, lab)
    scan = invoke(database, lab, service.record_inventory_scan, first.id, scan_data("UNKNOWN"))
    known = invoke(database, lab, service.record_inventory_scan, first.id, scan_data())
    with database.begin() as connection:
        snapshots = connection.execute(sa.select(InventoryAuditExpectedAsset.__table__)).mappings().all()
        expected = next(row for row in snapshots if row.audit_id == first.id and row.asset_id == lab["asset"])
        wrong = next(row for row in snapshots if row.audit_id == other.id and row.asset_id == lab["asset"])
        base = dict(id=uuid4(), audit_id=other.id, asset_id=None, expected_asset_id=None,
                    scan_id=scan.id, discrepancy_type="UNKNOWN_TAG", description="X", details={})
        expect_error(connection, sa.insert(InventoryAuditDiscrepancy).values(**base), "23503")
        base.update(id=uuid4(), audit_id=first.id, scan_id=known.id, asset_id=lab["outside"],
                    expected_asset_id=expected.id, discrepancy_type="MISPLACED")
        expect_error(connection, sa.insert(InventoryAuditDiscrepancy).values(**base), "23503")
        expect_error(connection, sa.insert(InventoryAuditScan).values(
            id=uuid4(), audit_id=first.id, idempotency_key=uuid4(), asset_id=lab["asset"],
            expected_asset_id=wrong.id, duplicate_of_scan_id=known.id, nfc_tag_id="TAG-1",
            observed_location="Test lab", scanned_by=lab["users"]["FACULTY"], result="DUPLICATE",
        ), "23503")
        expect_error(connection, sa.insert(InventoryAuditScan).values(
            id=uuid4(), audit_id=other.id, idempotency_key=uuid4(), nfc_tag_id="UNKNOWN",
            observed_location="Test lab", scanned_by=lab["users"]["FACULTY"], result="MATCHED",
        ), "23514")


@pytest.mark.parametrize("stage", ["snapshot", "scan", "finding", "response"])
def test_failed_operations_roll_back_all_new_records(database, lab, stage, monkeypatch):
    audit = create(database, lab) if stage != "snapshot" else None
    before = operational_state(database)
    save = service._save
    def fail_after_save(db, obj):
        save(db, obj)
        targeted = {
            "snapshot": InventoryAuditExpectedAsset, "scan": InventoryAuditScan, "finding": InventoryAuditDiscrepancy,
        }.get(stage)
        if targeted is not None and isinstance(obj, targeted):
            raise RuntimeError("Injected persistence failure")
    monkeypatch.setattr(service, "_save", fail_after_save)
    if stage == "response":
        monkeypatch.setattr(service.InventoryAuditResponse, "model_validate", Mock(side_effect=ValueError("Invalid response")))
    with Session(database) as db:
        with pytest.raises((RuntimeError, ValueError)):
            if stage == "snapshot":
                service.create_inventory_audit(db, InventoryAuditCreate(name="N", location="Test lab"), User(id=lab["users"]["FACULTY"]))
            elif stage == "scan":
                service.record_inventory_scan(db, audit.id, scan_data(), User(id=lab["users"]["FACULTY"]))
            else:
                service.finalize_inventory_audit(db, audit.id, InventoryAuditFinalize(expected_version=2),
                                                User(id=lab["users"]["FACULTY"]))
        assert not db.in_transaction() and not db.info
    with Session(database) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(InventoryAudit)) == int(audit is not None)
        assert db.scalar(sa.select(sa.func.count()).select_from(InventoryAuditScan)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(InventoryAuditDiscrepancy)) == 0
        if audit is not None:
            assert db.get(InventoryAudit, audit.id).version == 2
    assert operational_state(database) == before


def race(database, audit, commands):
    label = "inventory_audit_race_" + uuid4().hex
    def worker(command):
        with Session(database, autoflush=False) as db:
            label_transaction(db, label)
            try:
                return 200, command(db)
            except HTTPException as error:
                return error.status_code, None
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        root = blocker.begin()
        blocker.execute(sa.select(InventoryAudit).where(InventoryAudit.id == audit.id).with_for_update())
        futures = [pool.submit(worker, command) for command in commands]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND application_name=:label AND wait_event_type='Lock'"), {"label": label})
                if waiting == 2:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Both audit writers must wait for the parent lock")
        finally:
            root.rollback()
        return [future.result(timeout=12) for future in futures]


@pytest.mark.parametrize("mode", ["same-key", "physical-duplicate", "key-conflict", "finalize", "cancel", "double-finalize"])
def test_concurrent_scan_and_closure_are_serialized(database, lab, mode):
    audit = create(database, lab)
    data = scan_data()
    def scan(db):
        return invoke(database, lab, service.record_inventory_scan, audit.id, data, db=db)
    def close(db):
        return invoke(database, lab, service.finalize_inventory_audit, audit.id,
                      InventoryAuditFinalize(expected_version=2), db=db)
    first = close if mode == "double-finalize" else scan
    if mode in {"finalize", "double-finalize"}:
        second = close
    elif mode == "cancel":
        second = lambda db: invoke(database, lab, service.cancel_inventory_audit, audit.id,
                                   InventoryAuditCancel(expected_version=2, reason="X"), db=db)
    else:
        other = data if mode == "same-key" else scan_data(
            "OTHER" if mode == "key-conflict" else "TAG-1",
            key=data.idempotency_key if mode == "key-conflict" else None,
        )
        second = lambda db: invoke(database, lab, service.record_inventory_scan, audit.id, other, db=db)
    outcomes = race(database, audit, [first, second])
    statuses = sorted(code for code, _ in outcomes)
    if mode in {"same-key", "physical-duplicate"}:
        assert statuses == [200, 200]
        if mode == "same-key":
            assert outcomes[0][1].id == outcomes[1][1].id
        else:
            assert {row.result for _, row in outcomes} == {"MATCHED", "DUPLICATE"}
    else:
        assert statuses == [200, 409]
    summary = report(database, lab, audit.id)
    if mode == "physical-duplicate":
        assert summary.scan_count == 2
    elif mode == "double-finalize":
        assert summary.scan_count == 0
    elif mode in {"finalize", "cancel"}:
        assert summary.scan_count == int(summary.audit.status == "IN_PROGRESS")
    else:
        assert summary.scan_count == 1



def test_empty_upgrade_downgrade_and_populated_downgrade_refusal(database, lab):
    revision = importlib.import_module("backend.alembic.versions.d6f2b8a931c0_inventory_audits")
    with database.begin() as connection:
        with Operations.context(MigrationContext.configure(connection, opts={"metadata_audit_isolated_test": True})):
            revision.downgrade()
            assert not sa.inspect(connection).has_table("inventory_audits")
            revision.upgrade()
    create(database, lab)
    with database.begin() as connection:
        with Operations.context(MigrationContext.configure(connection, opts={"metadata_audit_isolated_test": True})):
            with pytest.raises(sa.exc.DBAPIError) as error:
                with connection.begin_nested():
                    revision.downgrade()
            assert error.value.orig.sqlstate == "55000"
            assert connection.scalar(sa.select(sa.func.count()).select_from(InventoryAudit)) == 1


@pytest.fixture
def api(database, monkeypatch):
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    app = FastAPI()
    app.include_router(router)
    def sessions():
        with Session(database, autoflush=False) as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_inventory_write_db] = sessions
    def request(path, data=None, method="POST", user="DEV-FACULTY", query=""):
        async def run():
            messages = []
            headers = [(b"content-type", b"application/json")]
            if user is not None:
                headers.append((b"x-dev-user", user.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                     "query_string": query.encode(), "root_path": "", "headers": headers,
                     "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80)}
            async def receive():
                return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}
            async def send(message):
                messages.append(message)
            await app(scope, receive, send)
            return next(message["status"] for message in messages if message["type"] == "http.response.start"), json.loads(
                b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body"))
        return asyncio.run(run())
    return request


def test_http_workflow_and_pagination(database, lab, api):
    status, audit = api("/inventory-audits", {"name": "HTTP audit", "location": "Test lab"})
    assert status == 201
    base = f"/inventory-audits/{audit['id']}"
    data = {"idempotency_key": str(uuid4()), "nfc_tag_id": "TAG-1", "observed_location": "Test lab"}
    assert api(base + "/scans", data)[0] == 200
    assert api(base + "/scans", data)[0] == 200
    for path in ("/inventory-audits", base + "/scans", base + "/expected-assets"):
        code, page = api(path, method="GET", query="limit=1&offset=0")
        assert code == 200 and len(page["items"]) == 1
    code, preview = api(base + "/report", method="GET", query="limit=1")
    assert code == 200 and preview["scan_count"] == 1
    assert api(base + "/finalize", {"expected_version": preview["audit"]["version"]})[0] == 200
    assert api(base + "/report", method="GET")[1]["reconciliation_complete"]
    assert api(base + "/scans", data)[0] == 200


@pytest.mark.parametrize("user,status", [(None, 401), ("DEV-STUDENT", 403)])
def test_http_unauthorized_users_cannot_read_or_write(database, lab, api, user, status):
    audit = create(database, lab)
    assert api("/inventory-audits", {"name": "N", "location": "Test lab"}, user=user)[0] == status
    assert api(f"/inventory-audits/{audit.id}/report", method="GET", user=user)[0] == status
    assert api(f"/inventory-audits/{audit.id}/scans", scan_data().model_dump(mode="json"), user=user)[0] == status


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=BAD", "offset=9223372036854775808"])
def test_http_invalid_filters_rejected(database, lab, api, query):
    assert api("/inventory-audits", method="GET", query=query)[0] == 422


@pytest.mark.parametrize("suffix,method,payload", [
    ("", "GET", None), ("/report", "GET", None), ("/scans", "POST", {"tag": "X"}),
    ("/finalize", "POST", {"expected_version": 2}),
    ("/cancel", "POST", {"expected_version": 2, "reason": "X"}),
])
def test_missing_audit_is_404(database, lab, api, suffix, method, payload):
    if suffix == "/scans":
        payload = scan_data().model_dump(mode="json")
    assert api(f"/inventory-audits/{uuid4()}{suffix}", payload, method=method)[0] == 404


def test_http_classification_and_actor_forgery_are_rejected(database, lab, api):
    audit = create(database, lab)
    base = f"/inventory-audits/{audit.id}"
    for extra in ({"result": "MATCHED"}, {"asset_id": str(lab["asset"])}, {"scanned_by": str(lab["users"]["FACULTY"])}):
        assert api(base + "/scans", scan_data().model_dump(mode="json") | extra)[0] == 422
    assert api(base + "/scans", scan_data().model_dump(mode="json") | {"nfc_tag_id": "bad\0tag"})[0] == 422
    assert report(database, lab, audit.id).scan_count == 0
