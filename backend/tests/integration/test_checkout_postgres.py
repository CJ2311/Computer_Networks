"""Real PostgreSQL tests; never fall back to the application's database."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
import psycopg
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
    # Prove the same dedicated login is denied access to development before DDL.
    # A successful connection runs no SQL and is forced read-only, then fails setup.
    try:
        denied = False
        try:
            # No SQLAlchemy initialization queries and no application SQL.
            connection = psycopg.connect(
                host=url.host, port=url.port or 5432,
                dbname=DATABASE_URL.database, user=url.username, password=url.password,
                connect_timeout=5, options="-cdefault_transaction_read_only=on",
            )
            connection.close()
        except Exception as error:
            denied = getattr(error, "sqlstate", None) in {"28000", "42501"}
        if not denied:
            pytest.fail("Test login must be denied connection to inventory_db; no test writes performed", pytrace=False)
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

    def request(payload, dev_user="DEV-ASSISTANT"):
        async def run():
            body = json.dumps(payload).encode()
            headers = [(b"content-type", b"application/json")]
            if dev_user is not None:
                headers.append((b"x-dev-user", dev_user.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": "/checkouts",
                     "raw_path": b"/checkouts", "query_string": b"", "root_path": "",
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
