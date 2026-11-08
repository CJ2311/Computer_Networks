"""Database-free execution of env.py with stubbed connections and Alembic calls."""
from contextlib import nullcontext
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from alembic import context
import pytest
import sqlalchemy

ENV = Path(__file__).resolve().parents[1] / "alembic_nfc_demo" / "env.py"
IDENTITY = dict(database="inventory_nfc_demo", session_user="inventory_migrator",
                current_user="inventory_migrator", schema="public")


@pytest.fixture
def harness(monkeypatch):
    # Never import application engines or load real credentials in these tests.
    monkeypatch.setitem(sys.modules, "backend.app.database", SimpleNamespace(Base=SimpleNamespace(metadata=object())))
    monkeypatch.setitem(sys.modules, "backend.app.models", SimpleNamespace())
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    connection = MagicMock()
    connection.dialect.name = "postgresql"
    connection.execute.return_value.one.return_value = (
        "inventory_nfc_demo", "inventory_migrator", "inventory_migrator", "public", ["pg_catalog", "public"])
    config = SimpleNamespace(attributes={"connection": connection, "nfc_demo_identity": dict(IDENTITY)},
                             config_file_name=None, config_ini_section="alembic", get_section=Mock(return_value={}))
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", Mock(return_value=False))
    monkeypatch.setattr(context, "configure", Mock())
    monkeypatch.setattr(context, "begin_transaction", Mock(side_effect=nullcontext))
    monkeypatch.setattr(context, "run_migrations", Mock())
    factory = Mock(side_effect=AssertionError("Unexpected engine creation"))
    monkeypatch.setattr(sqlalchemy, "engine_from_config", factory)
    return SimpleNamespace(config=config, connection=connection, factory=factory,
                           run=lambda: runpy.run_path(str(ENV)))


def test_external_connection_is_shared_and_not_closed(harness, monkeypatch):
    monkeypatch.delenv("INVENTORY_MIGRATOR_PASSWORD", raising=False)
    harness.run()
    context.run_migrations.assert_called_once_with()
    harness.factory.assert_not_called()
    for method in ("close", "commit", "rollback", "begin", "__enter__"):
        getattr(harness.connection, method).assert_not_called()
    harness.connection.engine.dispose.assert_not_called()
    assert context.configure.call_args.kwargs["connection"] is harness.connection
    assert context.configure.call_args.kwargs["version_table_schema"] == "public"


@pytest.mark.parametrize("field,value", [
    (0, "inventory_db"), (0, "inventory_checkout_test"), (0, "other"),
    (1, "unexpected_login"), (2, "unexpected_role"), (3, "other_schema"),
    (4, ["pg_catalog", "public", "other"]), (4, ["pg_catalog", "public", "pg_temp_1"]),
    (4, ["public", "pg_catalog"]),
])
def test_actual_identity_mismatch_blocks_migrations(harness, field, value):
    actual = list(harness.connection.execute.return_value.one.return_value)
    actual[field] = value
    harness.connection.execute.return_value.one.return_value = actual
    with pytest.raises(RuntimeError, match="identity or schema mismatch"):
        harness.run()
    context.run_migrations.assert_not_called()
    harness.connection.close.assert_not_called()


@pytest.mark.parametrize("identity", [None, {}, {"database": "inventory_nfc_demo"},
    {**IDENTITY, "database": "inventory_db"}, {**IDENTITY, "database": "inventory_checkout_test"},
    {**IDENTITY, "session_user": ""}, {**IDENTITY, "current_user": None},
    {**IDENTITY, "schema": "pg_catalog"}, {**IDENTITY, "schema": "public;other"}])
def test_missing_or_invalid_intent_fails_before_connection_use(harness, identity):
    harness.config.attributes["nfc_demo_identity"] = identity
    with pytest.raises(RuntimeError):
        harness.run()
    harness.connection.execute.assert_not_called()
    context.run_migrations.assert_not_called()


def test_no_identity_fallback_from_environment(harness, monkeypatch):
    harness.config.attributes.pop("nfc_demo_identity")
    monkeypatch.setenv("INVENTORY_DATABASE_NAME", "inventory_nfc_demo")
    with pytest.raises(RuntimeError, match="Explicit NFC"):
        harness.run()
    context.run_migrations.assert_not_called()


def test_non_postgres_rejected(harness):
    harness.connection.dialect.name = "sqlite"
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        harness.run()
    context.run_migrations.assert_not_called()


def test_failed_identity_query_never_runs_migrations_or_closes_caller(harness):
    harness.connection.execute.side_effect = RuntimeError("Query failed")
    with pytest.raises(RuntimeError, match="Query failed"):
        harness.run()
    context.run_migrations.assert_not_called()
    harness.connection.close.assert_not_called()


@pytest.mark.parametrize("offline", [True, False])
def test_offline_and_test_exception_are_rejected(harness, offline):
    context.is_offline_mode.return_value = offline
    harness.config.attributes["metadata_audit_isolated_test"] = True
    with pytest.raises(RuntimeError):
        harness.run()
    context.run_migrations.assert_not_called()



def test_demo_requires_external_connection(harness):
    harness.config.attributes.pop("connection")
    with pytest.raises(RuntimeError, match="externally supplied"):
        harness.run()
    harness.factory.assert_not_called()
    context.run_migrations.assert_not_called()


def test_ordinary_external_connection_needs_no_demo_identity(harness):
    harness.config.attributes.pop("nfc_demo_identity")
    runpy.run_path(str(ENV.parents[1] / "alembic" / "env.py"))
    context.run_migrations.assert_called_once_with()
    harness.connection.execute.assert_not_called()
    harness.connection.close.assert_not_called()


@pytest.mark.parametrize("target", [None, "inventory_db", "production_inventory"])
def test_ordinary_internal_connection_preserves_selector(harness, monkeypatch, target):
    harness.config.attributes.clear()
    monkeypatch.setenv("INVENTORY_MIGRATOR_PASSWORD", "test-placeholder")
    if target is None:
        monkeypatch.delenv("INVENTORY_DATABASE_NAME", raising=False)
    else:
        monkeypatch.setenv("INVENTORY_DATABASE_NAME", target)
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = harness.connection
    harness.factory.side_effect = None
    harness.factory.return_value = engine
    runpy.run_path(str(ENV.parents[1] / "alembic" / "env.py"))
    assert harness.factory.call_args.args[0]["sqlalchemy.url"].database == (target or "inventory_db")
    engine.dispose.assert_called_once_with()
    context.run_migrations.assert_called_once_with()


def test_ordinary_offline_still_supported(harness, monkeypatch):
    harness.config.attributes.clear()
    monkeypatch.setenv("INVENTORY_MIGRATOR_PASSWORD", "test-placeholder")
    context.is_offline_mode.return_value = True
    runpy.run_path(str(ENV.parents[1] / "alembic" / "env.py"))
    context.run_migrations.assert_called_once_with()
    harness.factory.assert_not_called()


def test_migration_failure_propagates_without_closing_external_connection(harness):
    context.run_migrations.side_effect = RuntimeError("migration failed")
    with pytest.raises(RuntimeError, match="migration failed"):
        harness.run()
    harness.connection.close.assert_not_called()
    harness.connection.commit.assert_not_called()
    harness.connection.rollback.assert_not_called()


def test_demo_import_does_not_load_application_credentials(monkeypatch):
    import subprocess
    import os
    env = dict(os.environ)
    for key in ("INVENTORY_APP_PASSWORD", "INVENTORY_MIGRATOR_PASSWORD"):
        env.pop(key, None)
    code = '''
import sys
from unittest.mock import Mock
from types import SimpleNamespace
from contextlib import nullcontext
import runpy
from alembic import context
import sqlalchemy
import dotenv
sqlalchemy.create_engine = Mock(side_effect=AssertionError("No application engine"))
dotenv.load_dotenv = Mock(side_effect=AssertionError("No dotenv"))
context.config = SimpleNamespace(attributes={})
context.is_offline_mode = lambda: False
try:
    runpy.run_path("backend/alembic_nfc_demo/env.py")
except RuntimeError as error:
    assert "Explicit NFC" in str(error)
else:
    raise AssertionError("Missing identity accepted")
assert "backend.app.database" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_demo_uses_existing_single_revision_history():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    demo = ScriptDirectory.from_config(Config("alembic_nfc_demo.ini"))
    ordinary = ScriptDirectory.from_config(Config("alembic.ini"))
    assert [r.revision for r in ordinary.walk_revisions()] == ["e8a4c2d190ab"] + [r.revision for r in demo.walk_revisions()]
    assert len(list(demo.walk_revisions())) == 7


def test_caller_transaction_survives_success_and_failure():
    # In-memory SQLite exercises real Alembic/SQLAlchemy transaction ownership;
    # the PostgreSQL identity query itself is covered by the environment tests.
    from alembic.migration import MigrationContext
    engine = sqlalchemy.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            migration = MigrationContext.configure(connection)
            with migration.begin_transaction():
                pass
            assert transaction.is_active
            with pytest.raises(RuntimeError):
                with migration.begin_transaction():
                    raise RuntimeError("failure")
            assert transaction.is_active
            transaction.rollback()
    finally:
        engine.dispose()

@pytest.mark.parametrize("caller_owned", [True, False])
@pytest.mark.parametrize("fail", [True, False])
def test_shared_helper_transaction_semantics(caller_owned, fail):
    from alembic.migration import MigrationContext
    from backend.alembic.safety import run_on_connection
    engine = sqlalchemy.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            outer = connection.begin() if caller_owned else None
            facade = SimpleNamespace()
            def configure(**options):
                facade.migration = MigrationContext.configure(
                    connection, opts={"transactional_ddl": True})
            facade.configure = configure
            facade.begin_transaction = lambda: facade.migration.begin_transaction()
            facade.run_migrations = Mock(side_effect=RuntimeError("migration failed") if fail else None)
            if fail:
                with pytest.raises(RuntimeError, match="migration failed"):
                    run_on_connection(facade, connection, None)
            else:
                run_on_connection(facade, connection, None)
            assert not connection.closed
            assert connection.in_transaction() == caller_owned
            if outer is not None:
                assert outer.is_active
                outer.rollback()
    finally:
        engine.dispose()
