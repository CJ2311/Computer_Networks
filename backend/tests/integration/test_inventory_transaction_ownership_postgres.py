"""Ownership regressions using only the existing isolated PostgreSQL fixtures."""
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.services.checkout import create_checkout
from backend.app.services.inventory_transactions import transaction
from backend.tests.integration.inventory_helpers import database_now
from backend.tests.integration.test_checkout_postgres import records


pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SADeprecationWarning")


def borrow(db, records, due):
    return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                           User(id=records["users"]["FACULTY"]), due)


def rejected(action):
    with pytest.raises(HTTPException) as error:
        action()
    assert error.value.status_code == 409


def assert_no_inventory_writes(database, records):
    with Session(database) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 0
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "AVAILABLE"


@pytest.mark.parametrize("kind", ["explicit", "read", "read_only", "flushed", "sql", "savepoint", "pending"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_rejected_caller_root_keeps_its_work_and_lifetime(database, records, kind, finish):
    due = database_now(database) + timedelta(days=1)
    caller_user = records["users"]["STUDENT"]
    with Session(database, autoflush=False) as db:
        original = None
        if kind == "explicit":
            db.begin()
        elif kind == "read_only":
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
        elif kind == "savepoint":
            db.begin_nested()
        else:
            user = db.get(User, caller_user)
            original = user.full_name
            if kind in {"flushed", "pending"}:
                user.full_name = "Caller-owned change"
                if kind == "flushed":
                    db.flush()
            elif kind == "sql":
                db.execute(sa.update(User).where(User.id == caller_user).values(full_name="Caller-owned change"))
        root, nested = db.get_transaction(), db.get_nested_transaction()
        rejected(lambda: borrow(db, records, due))
        assert db.get_transaction() is root and root.is_active
        assert db.get_nested_transaction() is nested
        if kind in {"flushed", "sql"}:
            assert db.scalar(sa.select(User.full_name).where(User.id == caller_user)) == "Caller-owned change"
        if finish == "commit":
            db.commit()  # Caller, not the service, chooses when its work commits.
        else:
            db.rollback()
    assert_no_inventory_writes(database, records)
    with Session(database) as db:
        expected = "Caller-owned change" if finish == "commit" and kind in {"pending", "flushed", "sql"} else "Integration test"
        assert db.get(User, caller_user).full_name == expected


def test_external_connection_is_rejected_without_ending_external_transaction(database, records):
    due = database_now(database) + timedelta(days=1)
    caller_user = records["users"]["STUDENT"]
    with database.connect() as connection:
        external = connection.begin()
        connection.execute(sa.update(User).where(User.id == caller_user).values(full_name="External transaction"))
        with Session(connection) as db:
            rejected(lambda: borrow(db, records, due))
            assert not db.in_transaction() and external.is_active
            assert connection.scalar(sa.select(User.full_name).where(User.id == caller_user)) == "External transaction"
        external.commit()
    with Session(database) as db:
        assert db.get(User, caller_user).full_name == "External transaction"
    assert_no_inventory_writes(database, records)


def test_failed_session_rejection_preserves_failed_root_for_caller_cleanup(database, records):
    due = database_now(database) + timedelta(days=1)
    with Session(database) as db:
        db.add(User(id=uuid4(), college_id="DEV-FACULTY", full_name="Duplicate",
                    email="duplicate@example.invalid", role="FACULTY", is_active=True))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        assert not db.is_active
        root = db.get_transaction()
        rejected(lambda: borrow(db, records, due))
        assert db.get_transaction() is root and not db.is_active
        db.rollback()
    assert_no_inventory_writes(database, records)


def test_authentication_read_and_write_have_independent_transaction_lifetimes(database, records):
    due = database_now(database) + timedelta(days=1)
    with Session(database) as authentication:
        staff = authentication.get(User, records["users"]["FACULTY"])
        authentication_root = authentication.get_transaction()
        with Session(database, autoflush=False) as write:
            response = create_checkout(write, records["asset"], records["users"]["STUDENT"], staff, due)
            assert not write.in_transaction()
        assert authentication.get_transaction() is authentication_root and authentication_root.is_active
        with Session(database) as observer:
            assert observer.get(EquipmentCheckout, response.id).status == "BORROWED"
            assert observer.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.mark.parametrize("late", [False, True])
def test_commit_callback_cannot_persist_unrelated_objects(database, records, late):
    due = database_now(database) + timedelta(days=1)
    with Session(database, autoflush=False) as db:
        def inject(session):
            session.add(User(id=uuid4(), college_id="UNRELATED", full_name="Unrelated callback",
                             email="unrelated@example.invalid", role="STUDENT", is_active=True))
        if late:
            def install_after_guard(session, context, instances):
                if not sa.event.contains(session, "before_commit", inject):
                    sa.event.listen(session, "before_commit", inject)
            sa.event.listen(db, "before_flush", install_after_guard)
        else:
            sa.event.listen(db, "before_commit", inject)
        rejected(lambda: borrow(db, records, due))
        assert not db.in_transaction() and not db.info
    assert_no_inventory_writes(database, records)
    with Session(database) as db:
        assert db.scalar(sa.select(User.id).where(User.college_id == "UNRELATED")) is None


@pytest.mark.parametrize("relationship", [False, True])
def test_late_flush_callback_cannot_persist_cascaded_or_unrelated_records(database, records, relationship):
    due = database_now(database) + timedelta(days=1)
    with Session(database, autoflush=False) as db:
        def inject(session, context, instances):
            asset = next(obj for obj in session.dirty if isinstance(obj, EquipmentAsset))
            if relationship:
                asset.equipment = Equipment(id=uuid4(), name="UNRELATED EQUIPMENT", category="IoT",
                                            tracking_type="INDIVIDUAL", default_location="Test")
            else:
                session.add(User(id=uuid4(), college_id="UNRELATED", full_name="Unrelated callback",
                                 email="unrelated@example.invalid", role="STUDENT", is_active=True))
        def install_after_guard(session, root, connection):
            sa.event.listen(session, "before_flush", inject)
        sa.event.listen(db, "after_begin", install_after_guard)
        rejected(lambda: borrow(db, records, due))
        assert not db.in_transaction() and not db.info
    assert_no_inventory_writes(database, records)
    with Session(database) as db:
        assert db.scalar(sa.select(User.id).where(User.college_id == "UNRELATED")) is None
        assert db.scalar(sa.select(Equipment.id).where(Equipment.name == "UNRELATED EQUIPMENT")) is None


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_interruption_after_real_inventory_writes_rolls_back_owned_transaction(database, records, monkeypatch, error_type):
    due = database_now(database) + timedelta(days=1)
    with Session(database, autoflush=False) as db:
        flush, count = db.flush, 0
        def interrupt():
            nonlocal count
            flush()
            count += 1
            if count == 2:
                raise error_type("Interrupted after checkout insert")
        monkeypatch.setattr(db, "flush", interrupt)
        with pytest.raises(error_type):
            borrow(db, records, due)
        assert not db.in_transaction() and not db.info
    assert_no_inventory_writes(database, records)


def test_transaction_replacement_is_detected_without_ending_replacement(database, records):
    replacement = []
    @transaction
    def operation(db):
        db.get_transaction().rollback()
        replacement.append(db.begin())
    with Session(database) as db:
        rejected(lambda: operation(db))
        assert db.get_transaction() is replacement[0] and replacement[0].is_active
        assert not db.info
        db.rollback()  # Explicit cleanup by caller.
    assert_no_inventory_writes(database, records)
