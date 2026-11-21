"""Database-free migration, catalog-verifier and deployment-chain checks."""
from importlib import import_module
import re
from unittest.mock import Mock

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy.dialects import postgresql

from backend.alembic import application_safety


migration = import_module("backend.alembic.application_versions.f1a6b8c920de_history_guards")


@pytest.fixture
def ddl(monkeypatch):
    operations = Mock()
    connection = operations.get_bind.return_value
    connection.dialect = postgresql.dialect()
    # Administrator, schema, two protected roles, runtime schema privilege.
    connection.scalar.side_effect = [True, "inventory", False, False, False]
    monkeypatch.setattr(migration, "op", operations)
    return operations, connection


def test_revision_is_a_forward_extension_of_all_approved_heads():
    scripts = ScriptDirectory.from_config(Config("alembic_nfc_application.ini"))
    assert scripts.get_current_head() == application_safety.HEAD
    assert migration.revision in {item.revision for item in scripts.iterate_revisions(application_safety.HEAD, migration.down_revision)}
    assert migration.down_revision == "c7d3e91024ab"
    assert application_safety.PREVIOUS_REVISIONS == {
        "e8a4c2d190ab", "c7d3e91024ab", "f1a6b8c920de", "b2d4e6f810ac", "c3a7e9b120df", "a9c2e4f610bd",
    }
    with pytest.raises(RuntimeError, match="forward migration"):
        migration.downgrade()


def test_migration_adds_guards_without_rewriting_history_or_changing_grants(ddl):
    operations, connection = ddl
    migration.upgrade()
    statements = [str(call.args[0]) for call in operations.execute.call_args_list]
    assert statements[0] == (
        "LOCK TABLE inventory.equipment_checkouts, inventory.missing_equipment_incidents "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    for name, body in migration.guard_bodies("inventory").items():
        assert any(f"AS $guard${body}$guard$" in statement for statement in statements)
        assert any(f"ALTER FUNCTION inventory.{name}() OWNER TO {migration.OWNER}" == statement
                   for statement in statements)
        assert any(f"REVOKE ALL ON FUNCTION inventory.{name}() FROM PUBLIC, inventory_app, "
                   "inventory_migrator, inventory_metadata_audit_writer" == statement for statement in statements)
    assert sum("BEFORE UPDATE ON" in statement for statement in statements) == 2
    assert all("DROP " not in statement and "ALTER TABLE " not in statement
               and not statement.startswith(("UPDATE ", "DELETE ", "INSERT ", "GRANT "))
               for statement in statements)
    assert all("SECURITY INVOKER SET search_path=pg_catalog,pg_temp" in statement
               for statement in statements if statement.startswith("CREATE FUNCTION"))
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()


@pytest.mark.parametrize("responses", [
    [False], [None], [True, "pg_temp_1"], [True, "public;other"],
    [True, "public", True], [True, "public", None],
    [True, "public", False, False, True],
    [True, "public", False, False, None],
])
def test_migration_fails_closed_before_ddl_on_unsafe_identity_or_privileges(ddl, responses):
    operations, connection = ddl
    connection.scalar.side_effect = responses
    with pytest.raises(RuntimeError):
        migration.upgrade()
    operations.execute.assert_not_called()


def test_migration_rejects_non_postgres_before_queries(ddl):
    operations, connection = ddl
    connection.dialect = Mock(name="sqlite")
    connection.dialect.name = "sqlite"
    with pytest.raises(RuntimeError, match="administrator"):
        migration.upgrade()
    connection.scalar.assert_not_called()
    operations.execute.assert_not_called()


def test_verifier_binds_exact_bodies_and_schema_qualified_function_names():
    connection = Mock()
    connection.scalar.return_value = False
    application_safety._verify_history_protection(connection, '"inventory"')
    calls = connection.scalar.call_args_list
    assert len(calls) == 3
    assert calls[0].args[1] == {"table": '"inventory".equipment_checkouts'}
    assert "DELETE,TRUNCATE,TRIGGER" in str(calls[0].args[0])
    for call, (name, body) in zip(calls[1:], migration.guard_bodies('"inventory"').items()):
        statement, parameters = call.args
        assert parameters == {"body": body, "name": f'"inventory".{name}()'}
        assert "p.prosrc<>:body" in str(statement)
        assert "p.prolang" in str(statement) and "p.prokind" in str(statement)
        assert name in application_safety.FUNCTIONS


@pytest.mark.parametrize("results", [[False, True], [False, None], [False, False, True], [False, False, None]])
def test_verifier_rejects_altered_or_missing_history_functions(results):
    connection = Mock()
    connection.scalar.side_effect = results
    with pytest.raises(RuntimeError, match="historical inventory"):
        application_safety._verify_history_protection(connection, "public")


@pytest.mark.parametrize("unsafe", [True, None])
def test_verifier_rejects_unsafe_or_unknown_checkout_privileges(unsafe):
    connection = Mock()
    connection.scalar.return_value = unsafe
    with pytest.raises(RuntimeError, match="checkout history privileges"):
        application_safety._verify_history_protection(connection, "public")


@pytest.mark.parametrize("function,mutable", [
    ("checkout_history_guard", {"status", "returned_at", "returned_to", "notes"}),
    ("missing_incident_history_guard", {"status", "version", "reconciled_by", "reconciled_at",
        "reconcile_reason", "recovered_condition", "resolved_availability_status",
        "resolved_condition_status", "reconcile_key", "reconcile_fingerprint"}),
])
def test_only_existing_transition_fields_are_exempted_from_history_comparison(function, mutable):
    body = migration.guard_bodies("public")[function]
    comparisons = re.findall(r"to_jsonb\((NEW|OLD)\) - ARRAY\[(.*?)\]", body, re.DOTALL)
    assert [side for side, fields in comparisons] == ["NEW", "OLD"]
    for side, fields in comparisons:
        assert set(re.findall(r"'([^']+)'", fields)) == mutable
