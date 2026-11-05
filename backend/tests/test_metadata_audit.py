"""Database-free checks for the audit migration's schema and security boundary."""
import importlib
from io import StringIO
from unittest.mock import Mock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from backend.app.models.metadata_audit import MetadataAuditEvent
from backend.tests.update_contract_helpers import AUDIT_PARENT_REVISION, CONTRACTS

migration = importlib.import_module("backend.alembic.versions.c4e8a17d92f6_metadata_audit")


def test_migration_matches_model_and_contract():
    assert migration.down_revision == AUDIT_PARENT_REVISION
    assert {entity.lower(): set(fields) for _, entity, _, _, fields in migration.SOURCES} == {
        kind: contract["editable"] for kind, contract in CONTRACTS.items()
    }
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration._create_table("inventory")
    migration_ddl = output.getvalue().replace("inventory.", "")
    model_ddl = str(CreateTable(MetadataAuditEvent.__table__).compile(dialect=postgresql.dialect()))
    migration_table = migration_ddl.split("CREATE INDEX")[0].strip().rstrip(";")
    assert migration_table.split() == model_ddl.split()


def test_single_owner_mode_fails_closed_outside_verified_schema(monkeypatch):
    monkeypatch.setattr(migration, "op", Mock())
    migration.op.get_context.return_value.opts = {"metadata_audit_isolated_test": True}
    connection = Mock()
    connection.execute.return_value.one.return_value = (
        "inventory_db", "inventory_checkout_test_login", "inventory_checkout_test_login",
    )
    schema = "checkout_it_" + "a" * 32
    with pytest.raises(RuntimeError, match="restricted to isolated test schemas"):
        migration._writer(connection, schema, schema)
    connection.execute.return_value.one.return_value = (
        "inventory_checkout_test", "inventory_checkout_test_login", "inventory_checkout_test_login",
    )
    with pytest.raises(RuntimeError, match="restricted to isolated test schemas"):
        migration._writer(connection, "public", "public")
    assert migration._writer(connection, schema, schema) == ("inventory_checkout_test_login", False)


def test_production_requires_preprovisioned_roles_and_safe_source_privileges(monkeypatch):
    monkeypatch.setattr(migration, "op", Mock())
    migration.op.get_context.return_value.opts = {}
    connection = Mock()
    connection.execute.return_value.one_or_none.return_value = None
    with pytest.raises(RuntimeError, match="Unsafe or missing metadata audit role"):
        migration._writer(connection, "public", '"public"')
    connection.execute.return_value.one_or_none.side_effect = [
        (False, False, False), (False, False, False), (True, False, False),
    ]
    connection.scalar.return_value = False
    with pytest.raises(RuntimeError, match="staff identity records"):
        migration._writer(connection, "public", '"public"')

def test_incoming_membership_checks_granted_role_even_without_set_option():
    connection = Mock()
    connection.scalar.return_value = True
    with pytest.raises(RuntimeError, match="Incoming membership"):
        migration._validate_incoming_memberships(connection)
    statement, params = connection.scalar.call_args.args
    assert "granted.oid = membership.roleid" in str(statement)
    assert "set_option" not in str(statement)
    assert params == {"owner": migration.OWNER, "writer": migration.WRITER}


def test_effective_validation_rejects_an_extra_writer_owned_definer_routine():
    connection = Mock()
    # Three function ACL checks, one table check, then the routine inventory.
    connection.scalar.side_effect = [False, False, False, False, True]
    with pytest.raises(RuntimeError, match="Unexpected SECURITY DEFINER routine"):
        migration._validate_effective_privileges(
            connection, '"public"', '"public".metadata_audit_events', '"public".metadata_audit_protect()',
        )
    statement, params = connection.scalar.call_args.args
    assert "proowner = CAST(:writer AS regrole) AND prosecdef" in str(statement)
    assert set(params.values()) == {
        migration.WRITER, '"public".metadata_audit_equipment_change()', '"public".metadata_audit_asset_change()',
    }
