"""Checkout snapshots on the existing identity-checked, per-test schema harness."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.borrowing_history import HistoryFilters
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.checkout import create_checkout, record_return
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history
from backend.tests.integration.test_checkout_postgres import records, api, payload
from backend.tests.integration.inventory_helpers import database_now

FIELDS = ("equipment_name_at_checkout", "asset_code_at_checkout", "identifier_snapshot_status")


def revision(connection, direction):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/b7d2f90c4e61_checkout_identifier_snapshots.py"
    spec = importlib.util.spec_from_file_location("snapshot_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


def borrow(database, records):
    with Session(database) as db:
        return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                               User(id=records["users"]["FACULTY"]),
                               database_now(database) + timedelta(days=1))


def test_exact_snapshots_survive_rename_return_and_reborrowing(database, records):
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        asset.asset_code = "  Exact-Code  "
        db.get(Equipment, asset.equipment_id).name = "  Sensor CASE  "
        db.commit()
    first = borrow(database, records)
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        asset.asset_code = "NEW-CODE"
        db.get(Equipment, asset.equipment_id).name = "New name"
        db.commit()
    for role in ("FACULTY", "STUDENT"):
        with Session(database) as db:
            user = db.get(User, records["users"][role])
            item = get_borrowing_history(db, user, first.id)
            listed = list_borrowing_history(db, user, HistoryFilters()).items[0]
            assert item.asset_code == listed.asset_code == "  Exact-Code  "
            assert item.equipment_name == listed.equipment_name == "  Sensor CASE  "
            assert item.identifier_snapshot_status == "CAPTURED"
    with Session(database) as db:
        record_return(db, first.id, User(id=records["users"]["FACULTY"]),
                      ReturnCreate(condition_status="WORKING"))
    second = borrow(database, records)
    with Session(database) as db:
        old, new = db.get(EquipmentCheckout, first.id), db.get(EquipmentCheckout, second.id)
        assert (old.equipment_name_at_checkout, old.asset_code_at_checkout) == ("  Sensor CASE  ", "  Exact-Code  ")
        assert (new.equipment_name_at_checkout, new.asset_code_at_checkout) == ("New name", "NEW-CODE")
        for field in ("asset_id", "student_id", "issued_by", "checked_out_at", "due_at"):
            assert getattr(old, field) == getattr(first, field)


@pytest.mark.parametrize("field", FIELDS)
def test_snapshot_update_rejected_and_rolled_back(database, records, field):
    loan = borrow(database, records)
    with database.connect() as connection:
        root = connection.begin()
        before = connection.execute(sa.select(EquipmentCheckout.__table__)).one()
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.update(EquipmentCheckout).where(EquipmentCheckout.id == loan.id)
                               .values(**{field: "LEGACY_UNKNOWN" if field == FIELDS[2] else "FORGED"}))
        assert error.value.orig.sqlstate == "55000"
        root.rollback()
        assert connection.execute(sa.select(EquipmentCheckout.__table__)).one() == before


@pytest.mark.parametrize("field", FIELDS)
def test_checkout_api_rejects_snapshot_input(database, records, api, field):
    data = payload(records)
    data[field] = "LEGACY_UNKNOWN" if field == FIELDS[2] else "FORGED"
    assert api(data)[0] == 422
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 0


@pytest.mark.parametrize("field", FIELDS)
def test_direct_insert_cannot_fabricate_snapshot(database, records, field):
    with database.connect() as connection:
        root = connection.begin()
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.insert(EquipmentCheckout).values(
                id=uuid4(), asset_id=records["asset"], student_id=records["users"]["STUDENT"],
                issued_by=records["users"]["FACULTY"], due_at=database_now(database) + timedelta(days=1),
                **{field: "LEGACY_UNKNOWN" if field == FIELDS[2] else "FORGED"}))
        assert error.value.orig.sqlstate == "42501"
        root.rollback()


def test_upgrade_preserves_legacy_fields_and_refuses_destructive_downgrade(database):
    # Empty-schema downgrade provides a real pre-revision fixture without disabling guards.
    with database.begin() as connection:
        revision(connection, "downgrade")
    seeded = records.__wrapped__(database)
    loan_id = uuid4()
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO equipment_checkouts (id,asset_id,student_id,issued_by,checked_out_at,due_at,notes) "
            "VALUES (:id,:asset,:student,:staff,clock_timestamp(),clock_timestamp()+interval '1 day','Original notes')"),
            dict(id=loan_id, asset=seeded["asset"], student=seeded["users"]["STUDENT"], staff=seeded["users"]["FACULTY"]))
        before = dict(connection.execute(sa.text("SELECT * FROM equipment_checkouts")).mappings().one())
        revision(connection, "upgrade")
        after = dict(connection.execute(sa.text("SELECT * FROM equipment_checkouts")).mappings().one())
        assert all(after[key] == value for key, value in before.items())
        assert tuple(after[field] for field in FIELDS) == (None, None, "LEGACY_UNKNOWN")
    with Session(database) as db:
        user = db.get(User, seeded["users"]["STUDENT"])
        item = get_borrowing_history(db, user, loan_id)
        assert item.asset_code is None and item.equipment_name is None
        assert item.identifier_snapshot_status == "LEGACY_UNKNOWN"
    with database.connect() as connection:
        root = connection.begin()
        with pytest.raises(sa.exc.DBAPIError) as error:
            revision(connection, "downgrade")
        assert error.value.orig.sqlstate == "55000"
        root.rollback()
        assert dict(connection.execute(sa.text("SELECT * FROM equipment_checkouts")).mappings().one()) == after


def test_empty_downgrade_upgrade_and_constraint(database):
    with database.begin() as connection:
        revision(connection, "downgrade")
        assert not set(FIELDS) & {c["name"] for c in sa.inspect(connection).get_columns("equipment_checkouts")}
        revision(connection, "upgrade")
        checks = {c["name"] for c in sa.inspect(connection).get_check_constraints("equipment_checkouts")}
        assert "ck_checkout_identifier_snapshots" in checks


@pytest.mark.parametrize("kind", ["asset", "equipment"])
def test_checkout_waits_for_label_writer_and_captures_committed_value(database, records, kind):
    from backend.tests.integration.inventory_helpers import label_transaction
    import time
    label = "snapshot_wait_" + uuid4().hex
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        root = blocker.begin()
        equipment_id = blocker.scalar(sa.select(EquipmentAsset.equipment_id).where(EquipmentAsset.id == records["asset"]))
        model = Equipment if kind == "equipment" else EquipmentAsset
        identifier = equipment_id if kind == "equipment" else records["asset"]
        field = "name" if kind == "equipment" else "asset_code"
        blocker.execute(sa.update(model).where(model.id == identifier).values(**{field: "Committed label"}))
        def checkout():
            with Session(database) as db:
                label_transaction(db, label)
                return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                       User(id=records["users"]["FACULTY"]),
                                       database_now(database) + timedelta(days=1))
        future = pool.submit(checkout)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND application_name=:label AND wait_event_type='Lock'"), {"label": label})
                if waiting:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Checkout did not wait for the label writer")
            root.commit()
        finally:
            if root.is_active:
                root.rollback()
        loan = future.result(timeout=12)
    with Session(database) as db:
        row = db.get(EquipmentCheckout, loan.id)
        assert getattr(row, FIELDS[0 if kind == "equipment" else 1]) == "Committed label"

@pytest.mark.parametrize("kind", ["asset", "equipment"])
def test_label_writer_waits_until_checkout_commits(database, records, kind):
    from threading import Event
    from backend.tests.integration.inventory_helpers import label_transaction
    import time
    ready, release = Event(), Event()
    label = "snapshot_writer_" + uuid4().hex
    with Session(database) as db:
        equipment_id = db.get(EquipmentAsset, records["asset"]).equipment_id
    model = Equipment if kind == "equipment" else EquipmentAsset
    identifier = equipment_id if kind == "equipment" else records["asset"]
    field = "name" if kind == "equipment" else "asset_code"
    def checkout():
        with Session(database) as db:
            def pause(session):
                ready.set()
                if not release.wait(7):
                    raise RuntimeError("Test checkout release timed out")
            sa.event.listen(db, "before_commit", pause)
            return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                   User(id=records["users"]["FACULTY"]),
                                   database_now(database) + timedelta(days=1))
    def writer():
        with Session(database) as db:
            label_transaction(db, label)
            db.execute(sa.update(model).where(model.id == identifier).values(**{field: "Later label"}))
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        checkout_future = pool.submit(checkout)
        try:
            assert ready.wait(5), "Checkout must reach its commit boundary"
            writer_future = pool.submit(writer)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND application_name=:label AND wait_event_type='Lock'"), {"label": label})
                if waiting:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Label writer must wait for checkout commit")
        finally:
            release.set()
        loan = checkout_future.result(timeout=12)
        writer_future.result(timeout=12)
    with Session(database) as db:
        row = db.get(EquipmentCheckout, loan.id)
        assert (row.equipment_name_at_checkout, row.asset_code_at_checkout) == ("Test sensor", "TEST-001")
