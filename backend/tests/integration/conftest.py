"""Shared integration fixtures. Never fall back to the application database."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.database import DATABASE_URL, engine as development_engine

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
                for prefix in ("f9bc7a594a00", "df453131d3d8", "ef14e8f62260", "a3c7e91b6d20", "b7d2f90c4e61"):
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


