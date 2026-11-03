import importlib.util
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers

from backend.app.database import Base
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.equipment import EquipmentAsset
from backend.app.schemas.asset import AssetResponse
from backend.app.schemas.checkout import CheckoutResponse, ReturnResponse


REVISION = "a3c7e91b6d20"
VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def load_revision(prefix):
    path = next(VERSIONS.glob(prefix + "_*.py"))
    spec = importlib.util.spec_from_file_location("maintenance_test_" + prefix, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SchemaRecorder:
    """Execute frozen migration definitions against metadata, never a database."""

    def __init__(self):
        self.metadata = sa.MetaData()
        self.statements = []

    def create_table(self, name, *elements, **kwargs):
        return sa.Table(name, self.metadata, *elements)

    def create_index(self, name, table, columns, **kwargs):
        return sa.Index(name, *(self.metadata.tables[table].c[c] for c in columns), **kwargs)

    def add_column(self, table, column):
        self.metadata.tables[table].append_column(column)

    def drop_constraint(self, name, table, **kwargs):
        target = self.metadata.tables[table]
        target.constraints.remove(next(c for c in target.constraints if c.name == name))

    def create_check_constraint(self, name, table, expression):
        self.metadata.tables[table].append_constraint(sa.CheckConstraint(expression, name=name))

    def create_foreign_key(self, name, source, target, local, remote, **kwargs):
        self.metadata.tables[source].append_constraint(sa.ForeignKeyConstraint(
            local, [target + "." + column for column in remote], name=name, **kwargs,
        ))

    def execute(self, statement):
        self.statements.append(str(statement))


@pytest.fixture
def recorded_schema():
    recorder = SchemaRecorder()
    for prefix in ("f9bc7a594a00", "df453131d3d8", "ef14e8f62260", REVISION):
        migration = load_revision(prefix)
        with patch.object(migration, "op", recorder):
            migration.upgrade()
    return recorder


def constraint_signature(constraint):
    if isinstance(constraint, sa.CheckConstraint):
        return ("check", constraint.name, str(constraint.sqltext))
    if isinstance(constraint, sa.ForeignKeyConstraint):
        return ("fk", tuple(constraint.column_keys), tuple(e.target_fullname for e in constraint.elements),
                constraint.ondelete, constraint.deferrable, constraint.initially)
    return (type(constraint).__name__, constraint.name, tuple(c.name for c in constraint.columns))


@pytest.mark.parametrize("table", ["equipment_assets", "maintenance_requests", "maintenance_events"])
def test_frozen_migration_matches_model_metadata(recorded_schema, table):
    expected, actual = Base.metadata.tables[table], recorded_schema.metadata.tables[table]
    dialect = postgresql.dialect()
    assert set(expected.c.keys()) == set(actual.c.keys())
    for column in expected.c:
        other = actual.c[column.name]
        assert str(column.type.compile(dialect=dialect)) == str(other.type.compile(dialect=dialect))
        assert column.nullable == other.nullable
        assert (str(column.server_default.arg) if column.server_default else None) == (
            str(other.server_default.arg) if other.server_default else None
        )
    assert {constraint_signature(c) for c in expected.constraints} == {
        constraint_signature(c) for c in actual.constraints
    }
    indexes = lambda t: {(i.name, i.unique, tuple(c.name for c in i.columns),
                          str(i.dialect_options["postgresql"].get("where"))) for i in t.indexes}
    assert indexes(expected) == indexes(actual)


def test_mapper_registration_and_audit_foreign_keys():
    configure_mappers()
    assert MaintenanceRequest.__table__ is Base.metadata.tables["maintenance_requests"]
    assert MaintenanceEvent.__table__ is Base.metadata.tables["maintenance_events"]
    assert all(fk.ondelete == "RESTRICT" for table in (MaintenanceRequest.__table__, MaintenanceEvent.__table__)
               for fk in table.foreign_key_constraints)
    event_fk = next(c for c in MaintenanceRequest.__table__.foreign_key_constraints if c.name == "fk_maintenance_current_event")
    assert event_fk.deferrable and event_fk.initially == "DEFERRED"


def test_active_unique_index_and_asset_safety_checks():
    active = next(i for i in MaintenanceRequest.__table__.indexes if i.name == "uq_active_maintenance_per_asset")
    assert active.unique
    assert str(active.dialect_options["postgresql"]["where"]) == "status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION')"
    checks = {c.name: str(c.sqltext) for c in EquipmentAsset.__table__.constraints if isinstance(c, sa.CheckConstraint)}
    assert "MAINTENANCE" in checks["ck_asset_availability"]
    assert "NOT maintenance_hold" in checks["ck_asset_available_serviceable"]
    assert "OR maintenance_hold" in checks["ck_asset_maintenance_requires_hold"]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_renders_offline_without_environment_or_connection(direction):
    output = StringIO()
    context = MigrationContext.configure(dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": output})
    migration = load_revision(REVISION)
    with Operations.context(context):
        getattr(migration, direction)()
    sql = output.getvalue()
    assert "inventory_db" not in sql and "inventory_app" not in sql
    assert "CASCADE" not in sql
    if direction == "upgrade":
        assert "CREATE TABLE maintenance_requests" in sql and "CREATE TABLE maintenance_events" in sql
        assert "DEFERRABLE INITIALLY DEFERRED" in sql
        assert "CREATE TRIGGER maintenance_event_no_rewrite" in sql
        assert "SECURITY DEFINER" not in sql
        assert "SECURITY INVOKER SET search_path = pg_catalog" in sql
        assert "UPDATE equipment_assets" not in sql and "INSERT INTO" not in sql
    else:
        assert sql.index("LOCK TABLE") < sql.index("Refusing downgrade") < sql.index("DROP TABLE")
        assert "SELECT 1 FROM maintenance_requests" in sql
        assert "SELECT 1 FROM maintenance_events" in sql
        assert "WHERE maintenance_hold OR availability_status = 'MAINTENANCE'" in sql


def test_database_guards_cover_retention_versions_recovery_and_hold(recorded_schema):
    sql = "\n".join(recorded_schema.statements)
    for phrase in (
        "BEFORE UPDATE OR DELETE ON maintenance_events", "BEFORE TRUNCATE ON maintenance_events",
        "BEFORE DELETE ON maintenance_requests", "BEFORE TRUNCATE ON maintenance_requests",
        "Terminal maintenance requests are immutable", "Original maintenance report fields are immutable",
        "NEW.version != OLD.version + 1", "Recovery requires a cancelled request",
        "Active maintenance requires an asset hold", "Cannot clear hold with active maintenance",
        "REVOKE ALL ON maintenance_requests, maintenance_events FROM PUBLIC",
    ):
        assert phrase in sql
    assert "%I.equipment_assets" in sql and "%I.maintenance_requests" in sql


def test_existing_checkout_and_return_api_schemas_are_unchanged():
    assert set(CheckoutResponse.model_fields) == {
        "id", "asset_id", "student_id", "issued_by", "checked_out_at", "due_at",
        "returned_at", "returned_to", "status", "notes",
    }
    assert set(ReturnResponse.model_fields) == set(CheckoutResponse.model_fields) | {
        "condition_status", "availability_status", "is_overdue",
    }
    assert "maintenance_hold" not in AssetResponse.model_fields
    assert "MAINTENANCE" not in str(ReturnResponse.model_fields["availability_status"].annotation)


def test_revision_extends_existing_head():
    migration = load_revision(REVISION)
    assert migration.revision == REVISION
    assert migration.down_revision == "ef14e8f62260"
