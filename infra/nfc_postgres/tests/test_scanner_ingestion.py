"""Only the opt-in disposable PostgreSQL harness may execute these tests."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.alembic.application_safety import HEAD, apply_application, verify_application
from backend.app.auth.scanners import ScannerCredential, token_digest
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.scanners import EquipmentScanner, ScannerScanEvent
from backend.app.models.user import User
from backend.app.schemas.scanners import ScanSubmit, ScanQuery
from backend.app.services.scanners import ingest_scan, read_scans
from infra.nfc_postgres.harness import ADMIN
from scripts import deploy_nfc_demo as baseline

KEY = base64.urlsafe_b64encode(b"device-synthetic-key-material-123"[:32]).rstrip(b"=").decode()
CAP = base64.urlsafe_b64encode(b"capture-synthetic-key-material-12"[:32]).rstrip(b"=").decode()
OTHER_KEY = base64.urlsafe_b64encode(b"o" * 32).rstrip(b"=").decode()
OTHER_CAP = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode()


def seed_case(database, admin):
    at = datetime.now(timezone.utc)
    owner = User(id=uuid4(), college_id="SCAN-F", full_name="Faculty", email="scan-f@college.invalid", role="FACULTY", is_active=True)
    other = User(id=uuid4(), college_id="SCAN-L", full_name="Assistant", email="scan-l@college.invalid", role="LAB_ASSISTANT", is_active=True)
    equipment = Equipment(id=uuid4(), name="RFID Equipment", category="Lab", tracking_type="INDIVIDUAL", default_location="Lab")
    asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id, asset_code="SCAN-ONE", nfc_tag_id="04a1b2c3", location="Lab")
    second = EquipmentAsset(id=uuid4(), equipment_id=equipment.id, asset_code="SCAN-TWO", nfc_tag_id="11223344", location="Lab")
    scanner = EquipmentScanner(id=uuid4(), secret_sha256=token_digest(KEY), capture_session_id=uuid4(),
        capture_user_id=owner.id, capture_secret_sha256=token_digest(CAP),
        capture_started_at=at, capture_expires_at=at + timedelta(minutes=14))
    with Session(admin, expire_on_commit=False) as db:
        db.add_all([owner, other, equipment]); db.commit()
        db.add_all([asset, second, scanner]); db.commit()
    return SimpleNamespace(h=database, admin=admin, runtime=database.engine("inventory_app"), owner=owner, other=other,
                           equipment=equipment, asset=asset, second=second, scanner=scanner,
                           credential=ScannerCredential(scanner.id, token_digest(KEY)))


@pytest.fixture
def case(database):
    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as c:
        database.verify_engine_identity(c); c.rollback(); apply_application(c, database.settings)
    return seed_case(database, admin)


def data(case, sequence=1, uid="04A1B2C3", scan_id=None, session_id=None):
    return ScanSubmit(scan_id=scan_id or uuid4(), capture_session_id=session_id or case.scanner.capture_session_id,
                      sequence=sequence, uid=uid)


def send(case, payload, credential=None):
    with Session(case.runtime) as db:
        return ingest_scan(db, payload, credential or case.credential)


def read(case, actor=None, session_id=None, secret=CAP, after=0, limit=25):
    with Session(case.runtime) as db:
        return read_scans(db, session_id or case.scanner.capture_session_id,
                          ScanQuery(after_sequence=after, limit=limit), actor or case.owner, token_digest(secret))


def inventory_snapshot(case):
    tables = ("users", "equipment", "equipment_assets", "equipment_stock", "equipment_checkouts", "quantity_loans",
              "stock_ledger", "metadata_audit_events", "maintenance_requests", "maintenance_events",
              "missing_equipment_incidents", "missing_equipment_events")
    with case.admin.connect() as c:
        return {name: c.execute(text(f"SELECT to_jsonb(r)::text FROM {name} r ORDER BY to_jsonb(r)::text")).scalars().all() for name in tables}


def test_registered_scanner_resolves_case_and_preserves_all_inventory(case):
    before = inventory_snapshot(case)
    result = send(case, data(case, uid="04a1b2c3"))
    assert result["status"] == 200 and result["data"].asset_id == case.asset.id
    assert result["data"].uid == "04A1B2C3" and not result["data"].duplicate
    assert result["data"].expires_at - result["data"].received_at == timedelta(seconds=120)
    assert inventory_snapshot(case) == before
    page = read(case)
    assert page["status"] == 200 and page["data"].items[0].asset_id == case.asset.id
    with case.admin.connect() as c:
        verify_application(c, case.h.settings)


@pytest.mark.parametrize("problem", ["unknown", "wrong_secret", "disabled"])
def test_invalid_device_credentials_never_stage_events(case, problem):
    credential = ScannerCredential(uuid4() if problem == "unknown" else case.scanner.id,
                                   token_digest(OTHER_KEY) if problem == "wrong_secret" else token_digest(KEY))
    if problem == "disabled":
        with case.admin.begin() as c:
            c.execute(update(EquipmentScanner).where(EquipmentScanner.id == case.scanner.id).values(enabled=False))
    assert send(case, data(case), credential)["status"] == 401
    with Session(case.admin) as db:
        assert db.scalar(select(func.count()).select_from(ScannerScanEvent)) == 0


def test_unknown_and_case_ambiguous_tags_never_mutate_inventory(case):
    before = inventory_snapshot(case)
    assert send(case, data(case, uid="CAFEBABE"))["status"] == 404
    assert inventory_snapshot(case) == before
    with Session(case.admin) as db:
        db.add(EquipmentAsset(id=uuid4(), equipment_id=case.equipment.id, asset_code="AMBIGUOUS",
                              nfc_tag_id="04A1B2C3", location="Lab")); db.commit()
    before = inventory_snapshot(case)
    assert send(case, data(case, sequence=2))["status"] == 409
    assert inventory_snapshot(case) == before
    assert read(case)["data"].items == []


def test_exact_retries_deduplicate_and_changed_or_stale_payloads_fail(case):
    original = data(case)
    first = send(case, original)
    retry = send(case, original)
    assert first["status"] == retry["status"] == 200
    assert not first["data"].duplicate and retry["data"].duplicate
    assert first["data"].received_at == retry["data"].received_at
    assert first["data"].expires_at == retry["data"].expires_at
    assert send(case, data(case, sequence=2, uid="11223344", scan_id=original.scan_id))["status"] == 409
    assert send(case, data(case, sequence=1, uid="11223344"))["status"] == 409
    assert send(case, data(case, sequence=2))["error"] == "duplicate_uid"
    assert len(read(case)["data"].items) == 1


def test_concurrent_exact_retries_create_one_event(case):
    barrier = Barrier(4)
    payload = data(case)
    def submit():
        barrier.wait(timeout=10)
        return send(case, payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(submit) for _ in range(4)]
        receipts = [future.result(timeout=30) for future in results]
    assert all(result["status"] == 200 for result in receipts)
    assert sum(not result["data"].duplicate for result in receipts) == 1
    assert len(read(case)["data"].items) == 1


def test_unknown_tags_still_consume_database_rate_and_sequence_budget(case):
    for sequence in range(1, 61):
        assert send(case, data(case, sequence=sequence, uid="CAFEBABE"))["status"] == 404
    assert send(case, data(case, sequence=61, uid="CAFEBABE"))["status"] == 429
    with Session(case.admin) as db:
        scanner = db.get(EquipmentScanner, case.scanner.id)
        assert scanner.request_count == 61 and scanner.last_sequence == 60
        assert db.scalar(select(func.count()).select_from(ScannerScanEvent)) == 0
    with case.admin.begin() as c:
        c.execute(update(EquipmentScanner).where(EquipmentScanner.id == case.scanner.id).values(
            window_started_at=datetime.now(timezone.utc) - timedelta(seconds=61)))
    assert send(case, data(case, sequence=61))["status"] == 200


def test_capture_proof_owner_and_expiry_are_required(case):
    send(case, data(case))
    assert read(case, actor=case.other)["status"] == 404
    assert read(case, secret=OTHER_CAP)["status"] == 404
    assert read(case, session_id=uuid4())["status"] == 404
    with case.admin.begin() as c:
        at = datetime.now(timezone.utc) - timedelta(minutes=2)
        c.execute(update(EquipmentScanner).where(EquipmentScanner.id == case.scanner.id).values(
            capture_started_at=at, capture_expires_at=at + timedelta(minutes=1)))
    assert read(case)["status"] == 404
    assert send(case, data(case, sequence=2, uid="11223344"))["status"] == 409


def test_rebinding_same_user_does_not_reveal_old_browser_session_or_delayed_scans(case):
    old_payload = data(case)
    assert send(case, old_payload)["status"] == 200
    new_session = uuid4()
    with case.admin.begin() as c:
        at = datetime.now(timezone.utc)
        c.execute(update(EquipmentScanner).where(EquipmentScanner.id == case.scanner.id).values(
            capture_session_id=new_session, capture_secret_sha256=token_digest(OTHER_CAP),
            capture_started_at=at, capture_expires_at=at + timedelta(minutes=14)))
    assert read(case)["status"] == 404
    assert read(case, session_id=new_session, secret=CAP)["status"] == 404
    assert send(case, old_payload)["status"] == 409
    assert send(case, data(case, sequence=2, uid="11223344"))["status"] == 409  # Delayed old-context scan.
    fresh = data(case, sequence=3, uid="11223344", session_id=new_session)
    assert send(case, fresh)["status"] == 200
    page = read(case, session_id=new_session, secret=OTHER_CAP)["data"]
    assert [row.scan_id for row in page.items] == [fresh.scan_id]


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_nonstaff_cannot_read_with_valid_capture_secret(case, role):
    with case.admin.begin() as c:
        c.execute(update(User).where(User.id == case.owner.id).values(role=role))
    with pytest.raises(HTTPException) as error:
        read(case)
    assert error.value.status_code == 403
    with case.runtime.begin() as c:
        result = c.scalar(text("SELECT scanner_read_events(:staff,:session,:digest,0,25)"),
            {"staff": case.owner.id, "session": case.scanner.capture_session_id, "digest": token_digest(CAP)})
        assert result["status"] == 404


def test_expired_events_are_invisible_and_cannot_be_replayed(case):
    payload = data(case)
    assert send(case, payload)["status"] == 200
    old = datetime.now(timezone.utc) - timedelta(seconds=121)
    with case.admin.begin() as c:
        c.execute(update(ScannerScanEvent).values(received_at=old, expires_at=old + timedelta(seconds=120)))
    assert read(case)["data"].items == []
    assert send(case, payload)["status"] == 409
    with Session(case.admin) as db:
        assert db.scalar(select(func.count()).select_from(ScannerScanEvent)) == 0


def test_event_count_and_page_size_are_bounded(case):
    at = datetime.now(timezone.utc)
    with Session(case.admin) as db:
        db.add_all([ScannerScanEvent(scanner_id=case.scanner.id, scan_id=uuid4(), sequence=index,
            session_id=case.scanner.capture_session_id, staff_user_id=case.owner.id, asset_id=case.asset.id,
            uid="04A1B2C3", received_at=at, expires_at=at + timedelta(seconds=120)) for index in range(1, 106)])
        db.execute(update(EquipmentScanner).where(EquipmentScanner.id == case.scanner.id).values(last_sequence=105))
        db.commit()
    assert send(case, data(case, sequence=106, uid="11223344"))["status"] == 200
    with Session(case.admin) as db:
        assert db.scalar(select(func.count()).select_from(ScannerScanEvent)) == 100
    page = read(case, limit=50)["data"]
    assert len(page.items) == 50 and page.has_more
    next_page = read(case, after=page.items[-1].sequence, limit=50)["data"]
    assert len(next_page.items) == 50 and not next_page.has_more
    assert not {row.scan_id for row in page.items} & {row.scan_id for row in next_page.items}


@pytest.mark.parametrize("statement", [
    "SELECT * FROM equipment_scanners", "SELECT secret_sha256 FROM equipment_scanners", "SELECT * FROM scanner_scan_events",
    "UPDATE equipment_scanners SET last_sequence=0", "DELETE FROM scanner_scan_events", "TRUNCATE scanner_scan_events",
    "SET ROLE inventory_metadata_audit_owner",
])
def test_runtime_cannot_bypass_staging_routines(case, statement):
    with pytest.raises(DBAPIError):
        with case.runtime.begin() as c:
            c.exec_driver_sql(statement)


@pytest.mark.parametrize("tamper", [
    "GRANT SELECT ON equipment_scanners TO inventory_app",
    "GRANT INSERT ON scanner_scan_events TO inventory_app",
    "ALTER TABLE scanner_scan_events DROP CONSTRAINT ck_scanner_event_expiry",
    "DROP INDEX ix_scanner_events_expiry",
    "ALTER FUNCTION scanner_ingest(uuid,text,uuid,uuid,bigint,text) SECURITY INVOKER",
    "ALTER FUNCTION scanner_read_events(uuid,uuid,text,bigint,integer) SET search_path=public",
    "GRANT EXECUTE ON FUNCTION scanner_ingest(uuid,text,uuid,uuid,bigint,text) TO PUBLIC",
])
def test_deployment_detects_altered_scanner_boundary(case, tamper):
    with case.admin.begin() as c:
        c.exec_driver_sql(tamper)
    with case.admin.connect() as c:
        with pytest.raises(RuntimeError):
            verify_application(c, case.h.settings)


def test_populated_step_12b_upgrade_preserves_source_data(database):
    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as c:
        database.verify_engine_identity(c); c.rollback()
        config = Config("alembic_nfc_application.ini")
        config.attributes.update(connection=c, nfc_demo_identity=baseline.identity(database.settings, ADMIN))
        command.upgrade(config, "b2d4e6f810ac"); c.commit()
    at = datetime.now(timezone.utc)
    borrower_id = uuid4()
    with admin.begin() as c:
        c.execute(text("""INSERT INTO users(id,college_id,full_name,email,role,is_active)
            VALUES(:id,'PRE-SCAN-BORROWER','Existing Borrower',NULL,'STUDENT',true)"""), {"id": borrower_id})
    # Seed normal checkout/custody through the existing services at the predecessor.
    from backend.app.services.checkout import create_checkout, record_return
    from backend.app.schemas.checkout import ReturnCreate
    owner = User(id=uuid4(), college_id="PRE-SCAN-F", full_name="Faculty", email="pre-scan@college.invalid", role="FACULTY", is_active=True)
    equipment = Equipment(id=uuid4(), name="Existing Item", category="Lab", tracking_type="INDIVIDUAL", default_location="Lab")
    asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id, asset_code="PRE-SCAN-ASSET", location="Lab")
    with Session(admin, expire_on_commit=False) as db:
        db.add_all([owner, equipment]); db.commit(); db.add(asset); db.commit()
    runtime = database.engine("inventory_app")
    with Session(runtime) as db:
        checkout = create_checkout(db, asset.id, borrower_id, owner, at + timedelta(days=1))
    snapshot_case = SimpleNamespace(admin=admin)
    before = inventory_snapshot(snapshot_case)
    with admin.connect() as c:
        apply_application(c, database.settings)
    assert inventory_snapshot(snapshot_case) == before
    with admin.connect() as c:
        verify_application(c, database.settings)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    with pytest.raises(DBAPIError):
        with runtime.begin() as c:
            c.execute(text("UPDATE equipment_checkouts SET student_id=:id WHERE id=:checkout"), {"id": owner.id, "checkout": checkout.id})
    with Session(runtime) as db:
        assert record_return(db, checkout.id, owner, ReturnCreate(condition_status="WORKING")).status == "RETURNED"
