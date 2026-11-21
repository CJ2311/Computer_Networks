"""Run only through the disposable PostgreSQL harness; never host databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.alembic.application_safety import HEAD, apply_application, verify_application
from backend.app.models.user import User
from backend.app.schemas.borrowers import BorrowerResolve
from backend.app.schemas.checkout import ReturnCreate
from backend.app.schemas.operations import QuantityLoanCreate, QuantityReturnCreate
from backend.app.schemas.stock import StockAdjustment
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.services.borrowers import resolve_borrower
from backend.app.services.checkout import create_checkout, record_return
from backend.app.services.quantity_loans import issue_quantity, return_quantity
from backend.app.services.stock import adjust_stock
from infra.nfc_postgres.harness import ADMIN
from scripts import deploy_nfc_demo as baseline


def upgrade_to_previous(database):
    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as c:
        database.verify_engine_identity(c)
        c.rollback()
        config = Config("alembic_nfc_application.ini")
        config.attributes.update(connection=c, nfc_demo_identity=baseline.identity(database.settings, ADMIN))
        command.upgrade(config, "f1a6b8c920de")
        c.commit()
    return admin


@pytest.fixture
def borrowers(database):
    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as c:
        database.verify_engine_identity(c)
        c.rollback()
        apply_application(c, database.settings)
    staff = User(id=uuid4(), college_id="BORROWER-FACULTY", full_name="Faculty",
                 email="borrower-faculty@college.invalid", role="FACULTY", is_active=True)
    with Session(admin, expire_on_commit=False) as db:
        db.add(staff)
        db.commit()
    return SimpleNamespace(h=database, admin=admin, runtime=database.engine("inventory_app"), staff=staff)


def resolve(case, registration="reg-123", name="Jane Doe", actor=None):
    with Session(case.runtime) as db:
        return resolve_borrower(db, BorrowerResolve(student_name=name, registration_number=registration), actor or case.staff)


def test_fresh_migration_and_creation_without_email(borrowers):
    c = borrowers
    result = resolve(c)
    assert result.created and result.registration_number == "REG-123"
    with Session(c.runtime) as db:
        row = db.get(User, result.student_id)
        assert (row.college_id, row.full_name, row.role, row.email, row.is_active) == ("REG-123", "Jane Doe", "STUDENT", None, True)
    with c.admin.connect() as connection:
        verify_application(connection, c.h.settings)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM external_identities WHERE user_id=:id)"), {"id": result.student_id})


def test_existing_legacy_case_whitespace_and_uuid_are_preserved(borrowers):
    c = borrowers
    legacy = User(id=uuid4(), college_id="\tlegacy-reg\u00a0", full_name="Jane Doe",
                  email="legacy@college.invalid", role="STUDENT", is_active=True)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(legacy)
        db.commit()
    result = resolve(c, " LEGACY-REG ")
    assert not result.created and result.student_id == legacy.id and result.registration_number == "LEGACY-REG"
    with Session(c.runtime) as db:
        row = db.get(User, legacy.id)
        assert row.college_id == "\tlegacy-reg\u00a0" and row.email == "legacy@college.invalid"


@pytest.mark.parametrize("role,active,name", [("STUDENT", True, "Different Name"), ("STUDENT", False, "Jane Doe"),
                                             ("FACULTY", True, "Jane Doe"), ("ADMIN", True, "Jane Doe")])
def test_identity_conflicts_never_overwrite_records(borrowers, role, active, name):
    c = borrowers
    user = User(id=uuid4(), college_id="REG-123", full_name=name, role=role,
                email="conflict@college.invalid", is_active=active)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(user)
        db.commit()
    with pytest.raises(HTTPException) as error:
        resolve(c)
    assert error.value.status_code == 409
    with Session(c.runtime) as db:
        row = db.get(User, user.id)
        assert (row.full_name, row.role, row.is_active, row.email) == (name, role, active, "conflict@college.invalid")
        assert db.scalar(select(func.count()).select_from(User).where(User.college_id == "REG-123")) == 1


def test_concurrent_creation_returns_one_stable_identity(borrowers):
    c = borrowers
    barrier = Barrier(4)
    def operation(_):
        barrier.wait(timeout=10)
        return resolve(c)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(operation, index) for index in range(4)]
        results = [future.result(timeout=30) for future in futures]
    assert len({row.student_id for row in results}) == 1
    assert sum(row.created for row in results) == 1
    with Session(c.runtime) as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.college_id == "REG-123")) == 1


def test_concurrent_conflicting_names_never_overwrite_winner(borrowers):
    c = borrowers
    barrier = Barrier(2)
    def operation(name):
        barrier.wait(timeout=10)
        try:
            return resolve(c, name=name)
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(operation, name) for name in ("Jane Doe", "Janet Doe")]
        results = [future.result(timeout=30) for future in futures]
    assert sum(result == 409 for result in results) == 1
    winner = next(result for result in results if not isinstance(result, int))
    with Session(c.runtime) as db:
        row = db.get(User, winner.student_id)
        assert row.full_name == winner.student_name
        assert db.scalar(select(func.count()).select_from(User).where(User.college_id == "REG-123")) == 1


def test_normalized_duplicates_are_rejected_by_database(borrowers):
    c = borrowers
    original = resolve(c)
    with pytest.raises(DBAPIError) as error:
        with c.admin.begin() as connection:
            connection.execute(text("""INSERT INTO users(id,college_id,full_name,email,role,is_active)
                VALUES(:id,E'\\treg-123 ', 'Other Name',NULL,'STUDENT',true)"""), {"id": uuid4()})
    assert error.value.orig.sqlstate == "23505"
    assert resolve(c).student_id == original.student_id


@pytest.mark.parametrize("statement", [
    "INSERT INTO users(id,college_id,full_name,email,role,is_active) VALUES(gen_random_uuid(),'FORGED','Forged',NULL,'STUDENT',true)",
    "UPDATE users SET role='FACULTY' WHERE role='STUDENT'",
    "DELETE FROM users WHERE role='STUDENT'",
    "SET ROLE inventory_metadata_audit_owner",
    "ALTER FUNCTION borrower_resolve_or_create(uuid,uuid,text,text) SECURITY INVOKER",
])
def test_runtime_privilege_boundaries(borrowers, statement):
    resolve(borrowers)
    with pytest.raises(DBAPIError):
        with borrowers.runtime.begin() as c:
            c.exec_driver_sql(statement)


@pytest.mark.parametrize("role", ["STUDENT", "ADMIN"])
def test_direct_routine_sql_cannot_use_nonstaff_actor(borrowers, role):
    c = borrowers
    actor = User(id=uuid4(), college_id="NONSTAFF", full_name="Not Staff", email="nonstaff@college.invalid", role=role, is_active=True)
    with Session(c.admin, expire_on_commit=False) as db:
        db.add(actor)
        db.commit()
    with pytest.raises(DBAPIError) as error:
        with c.runtime.begin() as db:
            db.execute(text("SELECT * FROM borrower_resolve_or_create(:actor,:id,'Jane Doe','NEW')"),
                       {"actor": actor.id, "id": uuid4()})
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("name,registration", [("", "NEW"), ("Jane\nDoe", "NEW"), ("A" * 151, "NEW"),
                                             ("Jane Doe", "lowercase"), ("Jane Doe", "REG 123"), ("Jane Doe", "A" * 51)])
def test_direct_sql_input_validation(borrowers, name, registration):
    c = borrowers
    with pytest.raises(DBAPIError) as error:
        with c.runtime.begin() as db:
            db.execute(text("SELECT * FROM borrower_resolve_or_create(:actor,:id,:name,:registration)"),
                       {"actor": c.staff.id, "id": uuid4(), "name": name, "registration": registration})
    assert error.value.orig.sqlstate == "22023"
    with Session(c.runtime) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_staff_email_requirement_and_email_uniqueness_remain(borrowers):
    c = borrowers
    for role, email in (("FACULTY", None), ("LAB_ASSISTANT", None), ("ADMIN", None),
                        ("STUDENT", "borrower-faculty@college.invalid")):
        with pytest.raises(DBAPIError):
            with c.admin.begin() as connection:
                connection.execute(text("""INSERT INTO users(id,college_id,full_name,email,role,is_active)
                    VALUES(:id,:registration,'Other Name',:email,:role,true)"""),
                    {"id": uuid4(), "registration": uuid4().hex.upper(), "email": email, "role": role})
    first, second = resolve(c, "ONE"), resolve(c, "TWO")
    assert first.student_id != second.student_id


@pytest.mark.parametrize("tamper", [
    "GRANT EXECUTE ON FUNCTION borrower_resolve_or_create(uuid,uuid,text,text) TO PUBLIC",
    "ALTER FUNCTION borrower_resolve_or_create(uuid,uuid,text,text) SECURITY INVOKER",
    "ALTER FUNCTION borrower_resolve_or_create(uuid,uuid,text,text) SET search_path=public",
    "GRANT INSERT ON users TO inventory_app",
    "GRANT UPDATE ON users TO inventory_metadata_audit_owner",
    "DROP INDEX uq_users_registration_normalized",
    "ALTER TABLE users DROP CONSTRAINT ck_users_staff_email",
])
def test_deployment_detects_tampered_borrower_boundary(borrowers, tamper):
    c = borrowers
    with c.admin.begin() as connection:
        connection.exec_driver_sql(tamper)
    with c.admin.connect() as connection:
        with pytest.raises(RuntimeError):
            verify_application(connection, c.h.settings)


def test_populated_upgrade_preserves_borrowers_checkout_quantity_and_returns(database):
    admin = upgrade_to_previous(database)
    staff = User(id=uuid4(), college_id="UPGRADE-F", full_name="Faculty", email="upgrade-f@college.invalid", role="FACULTY", is_active=True)
    student = User(id=uuid4(), college_id="legacy-s", full_name="Jane Doe", email="upgrade-s@college.invalid", role="STUDENT", is_active=True)
    item = Equipment(id=uuid4(), name="Scope", category="Lab", tracking_type="INDIVIDUAL", default_location="Lab")
    bulk = Equipment(id=uuid4(), name="Cables", category="Lab", tracking_type="QUANTITY", default_location="Lab")
    asset = EquipmentAsset(id=uuid4(), equipment_id=item.id, asset_code="UPGRADE-SCOPE", location="Lab")
    with Session(admin, expire_on_commit=False) as db:
        db.add_all([staff, student, item, bulk]); db.commit()
        db.add(asset); db.commit()
    runtime = database.engine("inventory_app")
    due = datetime.now(timezone.utc) + timedelta(days=1)
    with Session(runtime) as db:
        checkout = create_checkout(db, asset.id, student.id, staff, due, notes="Before borrower migration")
    with Session(runtime) as db:
        adjust_stock(db, bulk.id, StockAdjustment(expected_quantity=0, change=10), staff)
    with Session(runtime) as db:
        quantity = issue_quantity(db, QuantityLoanCreate(idempotency_key=uuid4(), equipment_id=bulk.id,
                                  student_id=student.id, quantity=2, due_at=due), staff)
    records = [("users", student.id), ("equipment_checkouts", checkout.id), ("quantity_loans", quantity.id)]
    with admin.connect() as connection:
        before = [connection.scalar(text(f"SELECT to_jsonb(r) FROM {table} r WHERE id=:id"), {"id": identity})
                  for table, identity in records]
    with admin.connect() as connection:
        apply_application(connection, database.settings)
    case = SimpleNamespace(runtime=runtime, staff=staff)
    resolved = resolve(case, "LEGACY-S")
    assert resolved.student_id == student.id and not resolved.created
    with admin.connect() as connection:
        after = [connection.scalar(text(f"SELECT to_jsonb(r) FROM {table} r WHERE id=:id"), {"id": identity})
                 for table, identity in records]
        assert after == before
        verify_application(connection, database.settings)
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(text("UPDATE equipment_checkouts SET student_id=:id WHERE id=:loan"),
                               {"id": staff.id, "loan": checkout.id})
    with Session(runtime) as db:
        assert record_return(db, checkout.id, staff, ReturnCreate(condition_status="WORKING")).status == "RETURNED"
    with Session(runtime) as db:
        assert return_quantity(db, quantity.id, QuantityReturnCreate(idempotency_key=uuid4(), condition_status="WORKING"), staff).returned_at


def test_legacy_normalized_collision_aborts_atomically(database):
    admin = upgrade_to_previous(database)
    with Session(admin) as db:
        db.add_all([User(id=uuid4(), college_id=registration, full_name="Jane Doe", email=f"collision-{index}@college.invalid",
                         role="STUDENT", is_active=True) for index, registration in enumerate(("case-reg", " CASE-REG "))])
        db.commit()
    with admin.connect() as connection:
        with pytest.raises(RuntimeError, match="collide"):
            apply_application(connection, database.settings)
    with admin.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "f1a6b8c920de"
        assert connection.scalar(text("SELECT count(*) FROM users")) == 2
        assert connection.scalar(text("SELECT attnotnull FROM pg_attribute WHERE attrelid='users'::regclass AND attname='email'"))
