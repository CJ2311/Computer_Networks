"""Real PostgreSQL tests; never fall back to the application's database."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.app.database import Base, DATABASE_URL, engine as development_engine, get_db
from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.routers.checkout import router
from backend.app.services.checkout import create_checkout
from backend.app.services.checkout import record_return
from backend.app.schemas.checkout import ReturnCreate


@pytest.fixture(scope="module", autouse=True)
def block_development_database():
    def forbidden(*args, **kwargs):
        raise RuntimeError("Application database engine is forbidden in integration tests")

    # Block both new and pooled connections, including SessionLocal's bind.
    # Restore the engine afterwards so other tests retain their existing behavior.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(development_engine, "connect", forbidden)
        patch.setattr(development_engine, "raw_connection", forbidden)
        yield


@pytest.fixture(scope="module")
def test_database(block_development_database):
    raw_url = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("INVENTORY_TEST_DATABASE_URL is unset; dedicated test database required")
    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail("Invalid test database URL (value hidden)", pytrace=False)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "inventory_checkout_test"
        or url.username != "inventory_checkout_test_login"
        or url.database == DATABASE_URL.database
        or url.username == DATABASE_URL.username
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.port not in {None, 5432}
        or url.query
        or not url.password
    ):
        pytest.fail(
            "Require local postgresql+psycopg, database inventory_checkout_test, "
            "login inventory_checkout_test_login, port 5432, and no URL query options",
            pytrace=False,
        )
    engine = create_engine(url, connect_args={"connect_timeout": 5}, hide_parameters=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_database(), current_user, session_user"
            )).one()
            if tuple(identity) != (
                "inventory_checkout_test", "inventory_checkout_test_login", "inventory_checkout_test_login"
            ):
                raise ValueError("Unexpected test connection identity")
            role = connection.execute(text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls, rolinherit, EXISTS (SELECT 1 FROM pg_auth_members "
                "WHERE member = r.oid) AS memberships FROM pg_roles r WHERE rolname = session_user"
            )).one()
            if any(role):
                raise ValueError("Test role has elevated attributes or memberships")
            if int(connection.scalar(text("SHOW server_version_num"))) < 180000:
                raise ValueError("PostgreSQL 18 or newer required")
    except Exception:
        engine.dispose()
        pytest.fail("Dedicated PostgreSQL 18 database unavailable or unsafe role (details hidden)", pytrace=False)
    # Development access denial is verified out of band by the administrator.
    # This suite must never attempt a connection to the development database.
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def database(test_database):
    # Unique schema per case: real commits and independent connections are safe.
    schema = "checkout_it_" + uuid4().hex
    with test_database.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = None
    try:
        engine = create_engine(
            test_database.url,
            connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=8000"},
            hide_parameters=True,
        )
        # Run the existing revision functions directly. env.py hardcodes inventory_db,
        # so invoking `alembic upgrade` here would target the development database.
        revisions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for prefix in ("f9bc7a594a00", "df453131d3d8", "ef14e8f62260"):
                    path = next(revisions.glob(prefix + "_*.py"))
                    spec = importlib.util.spec_from_file_location("test_revision_" + prefix, path)
                    revision = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(revision)
                    revision.upgrade()
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        # Only this generated schema, only in the explicitly named test database.
        with test_database.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def records(database):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        users = {}
        for role, college_id in (
            ("ADMIN", "DEV-ADMIN"), ("FACULTY", "DEV-FACULTY"),
            ("LAB_ASSISTANT", "DEV-ASSISTANT"), ("STUDENT", "DEV-STUDENT"),
        ):
            user = User(id=uuid4(), college_id=college_id, full_name="Integration test",
                        email=college_id + "@example.invalid", role=role, is_active=True)
            db.add(user)
            users[role] = user.id
        equipment = Equipment(id=uuid4(), name="Test sensor", category="IoT",
                              tracking_type="INDIVIDUAL", default_location="Test lab")
        db.add(equipment)
        db.flush()
        asset = EquipmentAsset(id=uuid4(), equipment_id=equipment.id,
                               asset_code="TEST-001", location="Test lab")
        db.add(asset)
        db.commit()
        return {"users": users, "asset": asset.id}


@pytest.fixture
def api(database, monkeypatch):
    # Exercise the actual router and auth dependencies with only get_db replaced.
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    app = FastAPI()
    app.include_router(router)

    def sessions():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            yield db

    app.dependency_overrides[get_db] = sessions

    def request(payload, dev_user="DEV-ASSISTANT", path="/checkouts"):
        async def run():
            body = json.dumps(payload).encode()
            headers = [(b"content-type", b"application/json")]
            if dev_user is not None:
                headers.append((b"x-dev-user", dev_user.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": headers, "client": ("127.0.0.1", 12345),
                     "server": ("127.0.0.1", 80)}
            messages = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            response = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(response)

        return asyncio.run(run())

    return request


def payload(records):
    return {"asset_id": str(records["asset"]), "student_id": str(records["users"]["STUDENT"]),
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "notes": "PostgreSQL integration test"}


def assert_no_checkout(database, asset_id, availability="AVAILABLE"):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == 0
        assert db.get(EquipmentAsset, asset_id).availability_status == availability


def test_migrated_schema_matches_model(database):
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, Base.metadata) == []
        inspector = inspect(connection)
        assert inspector.get_pk_constraint("equipment_checkouts")["constrained_columns"] == ["id"]
        assert {c["name"] for c in inspector.get_check_constraints("equipment_checkouts")} == {
            "ck_checkout_status", "ck_checkout_return_consistency",
            "ck_checkout_due_after_issue", "ck_checkout_return_after_issue",
        }


@pytest.mark.parametrize("issuer", ["DEV-ADMIN", "DEV-FACULTY", "DEV-ASSISTANT"])
def test_success_and_persistence(database, records, api, issuer):
    data = payload(records)
    status, response = api(data, issuer)
    assert status == 201
    from uuid import UUID
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkout = db.get(EquipmentCheckout, UUID(response["id"]))
        assert checkout is not None
        assert checkout.asset_id == records["asset"]
        assert checkout.student_id == records["users"]["STUDENT"]
        assert checkout.issued_by == records["users"][{"DEV-ADMIN": "ADMIN", "DEV-FACULTY": "FACULTY", "DEV-ASSISTANT": "LAB_ASSISTANT"}[issuer]]
        assert checkout.status == response["status"] == "BORROWED"
        assert checkout.notes == data["notes"]
        assert checkout.due_at == datetime.fromisoformat(data["due_at"])
        assert checkout.checked_out_at.tzinfo is not None
        assert checkout.returned_at is None and checkout.returned_to is None
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.mark.parametrize("availability", ["BORROWED", "RESERVED", "RETIRED"])
def test_unavailable_asset(database, records, api, availability):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        db.get(EquipmentAsset, records["asset"]).availability_status = availability
        db.commit()
    assert api(payload(records))[0] == 409
    assert_no_checkout(database, records["asset"], availability)


@pytest.mark.parametrize("borrower, expected", [("missing", 404), ("staff", 400), ("inactive", 403)])
def test_invalid_borrower(database, records, api, borrower, expected):
    data = payload(records)
    if borrower == "missing":
        data["student_id"] = str(uuid4())
    elif borrower == "staff":
        data["student_id"] = str(records["users"]["FACULTY"])
    else:
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            db.get(User, records["users"]["STUDENT"]).is_active = False
            db.commit()
    assert api(data)[0] == expected
    assert_no_checkout(database, records["asset"])


@pytest.mark.parametrize("dev_user, expected", [(None, 401), ("DEV-UNKNOWN", 401), ("DEV-STUDENT", 403)])
def test_auth_and_unauthorized_role(database, records, api, dev_user, expected):
    assert api(payload(records), dev_user)[0] == expected
    assert_no_checkout(database, records["asset"])


def test_inactive_issuer(database, records, api):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        db.get(User, records["users"]["LAB_ASSISTANT"]).is_active = False
        db.commit()
    assert api(payload(records))[0] == 403
    assert_no_checkout(database, records["asset"])


def test_database_constraint_failure_rolls_back(database, records):
    # A missing issuer FK fails at commit, after the asset UPDATE is flushed.
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        issuer = User(id=uuid4(), role="ADMIN", is_active=True)
        with pytest.raises(HTTPException) as error:
            create_checkout(db, records["asset"], records["users"]["STUDENT"], issuer,
                            datetime.now(timezone.utc) + timedelta(days=1))
        assert error.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == 0
    assert_no_checkout(database, records["asset"])


def test_concurrent_checkout_only_one_succeeds(database, records):
    label = "checkout_race_" + uuid4().hex

    def attempt():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            db.execute(text("SELECT set_config('application_name', :label, true)"), {"label": label})
            staff = db.get(User, records["users"]["LAB_ASSISTANT"])
            try:
                checkout = create_checkout(db, records["asset"], records["users"]["STUDENT"], staff,
                                           datetime.now(timezone.utc) + timedelta(days=1))
                return 201, checkout.id
            except HTTPException as error:
                return error.status_code, None

    # Hold the row until PostgreSQL confirms both workers are actually waiting.
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        futures = [pool.submit(attempt) for _ in range(2)]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"
                    ), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            assert waiting == 2, "Both workers must contend for the same PostgreSQL row lock"
        finally:
            transaction.rollback()
        results = [future.result(timeout=15) for future in futures]
    assert sorted(status for status, _ in results) == [201, 409]
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkouts = db.scalars(select(EquipmentCheckout)).all()
        assert len(checkouts) == 1
        assert checkouts[0].id == next(checkout_id for status, checkout_id in results if status == 201)
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.fixture
def borrowed_checkout(records, api):
    status, response = api(payload(records))
    assert status == 201
    return UUID(response["id"]), response


def return_path(checkout_id):
    return f"/checkouts/{checkout_id}/return"


def assert_still_borrowed(database, records, checkout_id, original_notes):
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == "BORROWED"
        assert checkout.returned_at is None and checkout.returned_to is None
        assert checkout.notes == original_notes
        assert db.get(EquipmentAsset, records["asset"]).condition_status == "WORKING"


@pytest.mark.parametrize("issuer", ["DEV-FACULTY", "DEV-ASSISTANT"])
@pytest.mark.parametrize("condition, availability", [
    ("WORKING", "AVAILABLE"), ("DAMAGED", "RESERVED"), ("UNDER_REPAIR", "RESERVED"),
])
def test_return_success_history_and_reborrowing(database, records, api, borrowed_checkout,
                                              issuer, condition, availability):
    checkout_id, original = borrowed_checkout
    with database.connect() as connection:
        before = connection.scalar(select(func.clock_timestamp()))
    status, response = api({"condition_status": condition, "notes": "Return inspection"},
                           issuer, path=return_path(checkout_id))
    assert status == 200
    assert response["is_overdue"] is False
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        after = db.scalar(select(func.clock_timestamp()))
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == response["status"] == "RETURNED"
        assert before <= checkout.returned_at <= after
        assert datetime.fromisoformat(response["returned_at"]) == checkout.returned_at
        expected_role = "FACULTY" if issuer == "DEV-FACULTY" else "LAB_ASSISTANT"
        assert checkout.returned_to == records["users"][expected_role]
        for field in ("id", "asset_id", "student_id", "issued_by", "checked_out_at", "due_at"):
            assert response[field] == original[field]
        assert checkout.notes.startswith(original["notes"] + "\n\n[RETURN] ")
        annotation = json.loads(checkout.notes.split("\n\n[RETURN] ", 1)[1])
        assert annotation["condition_status"] == condition
        assert annotation["notes"] == "Return inspection"
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.condition_status == response["condition_status"] == condition
        assert asset.availability_status == response["availability_status"] == availability

    next_status, next_response = api(payload(records))
    assert next_status == (201 if condition == "WORKING" else 409)
    with Session(database) as db:
        # The first checkout remains present after subsequent borrowing attempts.
        first = db.get(EquipmentCheckout, checkout_id)
        assert first.status == "RETURNED"
        assert json.loads(first.notes.split("\n\n[RETURN] ", 1)[1])["condition_status"] == condition
        assert db.scalar(select(func.count()).select_from(EquipmentCheckout)) == (2 if next_status == 201 else 1)


@pytest.mark.parametrize("overdue", [False, True])
def test_return_overdue_from_actual_timestamp(database, records, api, borrowed_checkout, overdue):
    checkout_id, _ = borrowed_checkout
    with Session(database) as db:
        now = db.scalar(select(func.clock_timestamp()))
        checkout = db.get(EquipmentCheckout, checkout_id)
        checkout.checked_out_at = now - timedelta(days=2)
        checkout.due_at = now + timedelta(days=-1 if overdue else 1)
        db.commit()
    status, response = api({"condition_status": "WORKING"}, path=return_path(checkout_id))
    assert status == 200
    assert response["is_overdue"] is overdue
    assert (datetime.fromisoformat(response["returned_at"]) > datetime.fromisoformat(response["due_at"])) is overdue


@pytest.mark.parametrize("issuer, expected", [
    (None, 401), ("DEV-UNKNOWN", 401), ("DEV-STUDENT", 403), ("DEV-ADMIN", 403),
    ("DEV-FACULTY", 403), ("DEV-ASSISTANT", 403),
])
def test_return_rejects_unauthorized_and_inactive_users(database, records, api, borrowed_checkout,
                                                     issuer, expected):
    checkout_id, original = borrowed_checkout
    if issuer in {"DEV-FACULTY", "DEV-ASSISTANT"}:
        role = "FACULTY" if issuer == "DEV-FACULTY" else "LAB_ASSISTANT"
        with Session(database) as db:
            db.get(User, records["users"][role]).is_active = False
            db.commit()
    assert api({"condition_status": "DAMAGED"}, issuer, path=return_path(checkout_id))[0] == expected
    assert_still_borrowed(database, records, checkout_id, original["notes"])


@pytest.mark.parametrize("availability", ["AVAILABLE", "RESERVED", "RETIRED"])
def test_return_rejects_unexpected_asset_state(database, records, api, borrowed_checkout, availability):
    checkout_id, original = borrowed_checkout
    with Session(database) as db:
        db.get(EquipmentAsset, records["asset"]).availability_status = availability
        db.commit()
    assert api({"condition_status": "WORKING"}, path=return_path(checkout_id))[0] == 409
    assert_still_borrowed(database, records, checkout_id, original["notes"])
    with Session(database) as db:
        assert db.get(EquipmentAsset, records["asset"]).availability_status == availability


def test_return_missing_checkout(database, records, api):
    assert api({"condition_status": "WORKING"}, path=return_path(uuid4()))[0] == 404
    assert_no_checkout(database, records["asset"])


@pytest.mark.parametrize("data", [
    {}, {"condition_status": "MISSING"}, {"condition_status": "MALFUNCTIONING"},
    {"condition_status": "WORKING", "notes": "x" * 1001},
    {"condition_status": "WORKING", "returned_to": str(uuid4())},
    {"condition_status": "WORKING", "returned_at": "2000-01-01T00:00:00Z"},
])
def test_return_rejects_invalid_payload(database, records, api, borrowed_checkout, data):
    checkout_id, original = borrowed_checkout
    assert api(data, path=return_path(checkout_id))[0] == 422
    assert_still_borrowed(database, records, checkout_id, original["notes"])


@pytest.mark.parametrize("reborrow", [False, True])
def test_duplicate_return_preserves_first_return_and_new_loan(database, records, api,
                                                            borrowed_checkout, reborrow):
    checkout_id, _ = borrowed_checkout
    status, original_return = api({"condition_status": "WORKING", "notes": "First return"},
                                  path=return_path(checkout_id))
    assert status == 200
    if reborrow:
        assert api(payload(records))[0] == 201
    assert api({"condition_status": "DAMAGED", "notes": "Duplicate"},
               "DEV-FACULTY", path=return_path(checkout_id))[0] == 409
    with Session(database) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.notes == original_return["notes"]
        assert checkout.returned_at == datetime.fromisoformat(original_return["returned_at"])
        assert str(checkout.returned_to) == original_return["returned_to"]
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.condition_status == "WORKING"
        assert asset.availability_status == ("BORROWED" if reborrow else "AVAILABLE")


def test_return_real_foreign_key_failure_rolls_back(database, records, borrowed_checkout):
    checkout_id, original = borrowed_checkout
    with Session(database, autoflush=False, expire_on_commit=False) as db:
        fabricated_receiver = User(id=uuid4(), role="FACULTY", is_active=True)
        with pytest.raises(HTTPException) as error:
            record_return(db, checkout_id, fabricated_receiver,
                          ReturnCreate(condition_status="DAMAGED", notes="Must roll back"))
        assert error.value.status_code == 409
        assert db.get(EquipmentCheckout, checkout_id).status == "BORROWED"
    assert_still_borrowed(database, records, checkout_id, original["notes"])
    with Session(database) as db:
        assert db.get(EquipmentAsset, records["asset"]).availability_status == "BORROWED"


@pytest.mark.parametrize("race_with_checkout", [False, True])
def test_return_concurrency_and_database_clock_after_lock_wait(database, records,
                                                             borrowed_checkout, race_with_checkout):
    checkout_id, original = borrowed_checkout
    label = "return_race_" + uuid4().hex

    def attempt(is_checkout):
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            db.execute(text("SELECT set_config('application_name', :label, true)"), {"label": label})
            staff = db.get(User, records["users"]["FACULTY"])
            try:
                if is_checkout:
                    result = create_checkout(db, records["asset"], records["users"]["STUDENT"],
                                             staff, datetime.now(timezone.utc) + timedelta(days=1))
                    return 201, result.id
                result = record_return(db, checkout_id, staff, ReturnCreate(condition_status="WORKING"))
                return 200, result.returned_at
            except HTTPException as error:
                return error.status_code, None

    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        transaction = blocker.begin()
        blocker.execute(select(EquipmentAsset).where(EquipmentAsset.id == records["asset"]).with_for_update())
        futures = [pool.submit(attempt, False), pool.submit(attempt, race_with_checkout)]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = :label AND wait_event_type = 'Lock'"
                    ), {"label": label})
                if waiting == 2:
                    break
                time.sleep(0.05)
            assert waiting == 2, "Both requests must wait on the same asset row lock"
            released_at = blocker.scalar(select(func.clock_timestamp()))
        finally:
            transaction.rollback()
        results = [future.result(timeout=15) for future in futures]

    if race_with_checkout:
        assert results[0][0] == 200
        assert results[1][0] in {201, 409}
    else:
        assert sorted(status for status, _ in results) == [200, 409]
    returned_at = next(value for status, value in results if status == 200)
    assert returned_at >= released_at  # now() would incorrectly record transaction start.
    with Session(database) as db:
        checkout = db.get(EquipmentCheckout, checkout_id)
        assert checkout.status == "RETURNED"
        assert checkout.returned_at == returned_at
        assert checkout.notes.startswith(original["notes"])
        assert checkout.notes.count("\n\n[RETURN] ") == 1
        borrowed = db.scalars(select(EquipmentCheckout).where(EquipmentCheckout.status == "BORROWED")).all()
        reborrowed = race_with_checkout and results[1][0] == 201
        assert len(borrowed) == (1 if reborrowed else 0)
        if reborrowed:
            assert borrowed[0].id == results[1][1]
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.availability_status == ("BORROWED" if reborrowed else "AVAILABLE")
        assert asset.condition_status == "WORKING"
