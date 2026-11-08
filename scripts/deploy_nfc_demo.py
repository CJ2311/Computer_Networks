"""Interactive, staged deployment to inventory_nfc_demo only. Never imported by app."""
import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import getpass
import ipaddress
import logging
from pathlib import Path
import re
import sys
import warnings

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.alembic.safety import verify_identity
from backend.app.base import Base
import backend.app.models  # noqa: F401

DATABASE = "inventory_nfc_demo"
MIGRATOR = "inventory_migrator"
B7, C4, HEAD = "b7d2f90c4e61", "c4e8a17d92f6", "d6f2b8a931c0"
STAGES = ((None, B7, MIGRATOR), (B7, C4, "administrator"), (C4, HEAD, MIGRATOR))
LOCK_KEY = 0x4E464344454D4F


class DeploymentError(RuntimeError):
    """Safe, credential-free error suitable for the terminal."""


@dataclass(frozen=True)
class Settings:
    database: str
    server_address: str
    port: int
    administrator: str
    schema: str = "public"

    def validate(self):
        if self.database != DATABASE:
            raise DeploymentError("Only inventory_nfc_demo is permitted")
        try:
            address = ipaddress.ip_address(self.server_address)
        except ValueError:
            raise DeploymentError("An explicit local server IP address is required") from None
        if not address.is_loopback or not 1 <= self.port <= 65535:
            raise DeploymentError("Only an explicit local PostgreSQL server is permitted")
        for name in (self.administrator, self.schema):
            if not re.fullmatch(r"[a-z_][a-z_0-9]*", name or "") or name.startswith("pg_"):
                raise DeploymentError("Explicit simple administrator and schema names required")
        if self.administrator in {MIGRATOR, "inventory_app", "inventory_metadata_audit_owner", "inventory_metadata_audit_writer"}:
            raise DeploymentError("An independent deployment administrator is required")


def identity(settings, user):
    return dict(database=DATABASE, session_user=user, current_user=user, schema=settings.schema)


def check_connection(connection, settings, user):
    verify_identity(connection, identity(settings, user))
    row = connection.execute(text("""
        SELECT host(inet_server_addr()), inet_server_port(),
               current_setting('server_version_num')::int,
               (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
               current_setting('transaction_isolation')
    """)).one()
    if (ipaddress.ip_address(row[0]) != ipaddress.ip_address(settings.server_address)
            or row[1] != settings.port or not 180004 <= row[2] < 190000
            or row[3] != (user == settings.administrator) or row[4] != "read committed"):
        raise DeploymentError("Server, version, isolation, or deployment role mismatch")


def make_engine(settings, user, password):
    # Explicit connect parameters override libpq environment/service defaults.
    return create_engine(URL.create("postgresql+psycopg", username=user, password=password,
        host=settings.server_address, port=settings.port, database=DATABASE),
        poolclass=NullPool, echo=False, hide_parameters=True,
        isolation_level="READ COMMITTED", connect_args={"connect_timeout": 10,
        "options": f"-csearch_path={settings.schema} -cstatement_timeout=120000 -clock_timeout=10000",
        "application_name": "inventory_nfc_demo_deployment"})


def acquire_stage_lock(connection):
    """Lock the transaction that performs the work, never a guardian session.

    No explicit unlock exists: PostgreSQL releases this lock at transaction end.
    Contention aborts this invocation; rerun to inspect the latest checkpoint.
    """
    if not connection.in_transaction():
        raise DeploymentError("A stage transaction is required")
    if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY}):
        raise DeploymentError("Another NFC deployment stage holds the lock")


def read_checkpoint(engine, settings):
    # Also serialize inspection with active stages; never inspect partial DDL.
    with engine.connect() as connection, connection.begin():
        acquire_stage_lock(connection)
        check_connection(connection, settings, MIGRATOR)
        return checkpoint(connection, settings)


def expected_tables(revision):
    if revision not in {None, B7, C4, HEAD}:
        raise DeploymentError("Unknown deployment revision")
    if revision is None:
        return set()
    tables = {"users", "equipment", "equipment_assets", "equipment_stock", "equipment_checkouts",
              "maintenance_requests", "maintenance_events", "metadata_audit_events", "inventory_audits",
              "inventory_audit_expected_assets", "inventory_audit_scans", "inventory_audit_discrepancies"}
    if revision != HEAD:
        tables -= {name for name in tables if name.startswith("inventory_audit")}
    if revision == B7:
        tables.remove("metadata_audit_events")
    return tables | {"alembic_version"}


def checkpoint(connection, settings):
    """Reject incomplete markers, extra objects and mismatched table shapes."""
    schema = settings.schema
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=schema))
    revision = None
    if "alembic_version" in tables:
        quoted = connection.dialect.identifier_preparer.quote_schema(schema)
        rows = connection.execute(text(f"SELECT version_num FROM {quoted}.alembic_version")).scalars().all()
        if len(rows) != 1 or rows[0] not in {B7, C4, HEAD}:
            raise DeploymentError("Unexpected or partial Alembic revision state")
        revision = rows[0]
    if tables != expected_tables(revision) or inspector.get_view_names(schema=schema):
        raise DeploymentError("Unexpected tables, views, or partial deployment")
    if connection.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname=:schema AND c.relkind='r'
            AND pg_get_userbyid(c.relowner) <> CASE WHEN c.relname='metadata_audit_events'
              THEN 'inventory_metadata_audit_owner' ELSE 'inventory_migrator' END)
    """), {"schema": schema}):
        raise DeploymentError("Unexpected table ownership")
    # No foreign/partitioned/materialized relations, standalone sequences or types.
    if connection.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname=:schema AND c.relkind NOT IN ('r','i'))
        OR EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
          WHERE n.nspname=:schema AND t.typrelid=0 AND t.typelem=0)
    """), {"schema": schema}):
        raise DeploymentError("Unexpected schema objects")
    for name in tables - {"alembic_version"}:
        model = Base.metadata.tables[name]
        columns = inspector.get_columns(name, schema=schema)
        if {c["name"] for c in columns} != set(model.columns.keys()):
            raise DeploymentError("Table columns do not match deployment checkpoint")
        for column in columns:
            expected = model.columns[column["name"]]
            if (column["nullable"] != expected.nullable or
                str(column["type"].compile(dialect=connection.dialect)).upper() != str(expected.type.compile(dialect=connection.dialect)).upper()):
                raise DeploymentError("Column type or nullability mismatch")
        checks = {c["name"] for c in inspector.get_check_constraints(name, schema=schema)}
        expected_checks = {c.name for c in model.constraints if c.__class__.__name__ == "CheckConstraint"}
        if checks != expected_checks:
            raise DeploymentError("Check constraints do not match checkpoint")
        if set(inspector.get_pk_constraint(name, schema=schema)["constrained_columns"]) != {c.name for c in model.primary_key}:
            raise DeploymentError("Primary key mismatch")
        indexes = {i["name"] for i in inspector.get_indexes(name, schema=schema) if not i.get("duplicates_constraint")}
        if indexes != {i.name for i in model.indexes}:
            raise DeploymentError("Index mismatch")
        unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name, schema=schema)}
        expected_unique = {tuple(c.name for c in constraint.columns) for constraint in model.constraints
                           if constraint.__class__.__name__ == "UniqueConstraint"}
        if unique != expected_unique:
            raise DeploymentError("Unique constraint mismatch")
        foreign = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                   for f in inspector.get_foreign_keys(name, schema=schema)}
        expected_foreign = {(tuple(c.name for c in constraint.columns),
                             constraint.elements[0].column.table.name,
                             tuple(e.column.name for e in constraint.elements))
                            for constraint in model.foreign_key_constraints}
        if foreign != expected_foreign:
            raise DeploymentError("Foreign key mismatch")
    validate_guards(connection, schema, revision)
    return revision


def validate_guards(connection, schema, revision):
    functions = {"maintenance_history_no_rewrite", "maintenance_request_guard", "maintenance_asset_hold_guard", "checkout_identifier_snapshots"} if revision else set()
    triggers = {
        ("maintenance_requests", "maintenance_request_validate", "maintenance_request_guard"),
        ("maintenance_requests", "maintenance_request_no_delete", "maintenance_history_no_rewrite"),
        ("maintenance_requests", "maintenance_request_no_truncate", "maintenance_history_no_rewrite"),
        ("maintenance_events", "maintenance_event_no_rewrite", "maintenance_history_no_rewrite"),
        ("maintenance_events", "maintenance_event_no_truncate", "maintenance_history_no_rewrite"),
        ("equipment_assets", "maintenance_asset_hold_validate", "maintenance_asset_hold_guard"),
        ("equipment_checkouts", "checkout_identifier_snapshots", "checkout_identifier_snapshots"),
    } if revision else set()
    if revision in {C4, HEAD}:
        functions |= {"metadata_audit_protect", "metadata_audit_equipment_change", "metadata_audit_asset_change"}
        triggers |= {("metadata_audit_events", "metadata_audit_no_rewrite", "metadata_audit_protect"),
                     ("metadata_audit_events", "metadata_audit_no_truncate", "metadata_audit_protect"),
                     ("equipment", "metadata_audit_equipment_change", "metadata_audit_equipment_change"),
                     ("equipment_assets", "metadata_audit_asset_change", "metadata_audit_asset_change")}
    if revision == HEAD:
        functions |= {"inventory_audit_parent_guard", "inventory_audit_child_guard", "inventory_audit_no_rewrite"}
        for table in expected_tables(HEAD):
            if table.startswith("inventory_audit"):
                parent = table == "inventory_audits"
                triggers |= {(table, "inventory_audit_parent_validate" if parent else "inventory_audit_child_validate",
                              "inventory_audit_parent_guard" if parent else "inventory_audit_child_guard"),
                             (table, "inventory_audit_immutable", "inventory_audit_no_rewrite"),
                             (table, "inventory_audit_no_truncate", "inventory_audit_no_rewrite")}
    rows = connection.execute(text("""SELECT p.proname, p.pronargs,
        pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema"""), {"schema": schema}).all()
    if {(r[0], r[1]) for r in rows} != {(name, 0) for name in functions}:
        raise DeploymentError("Unexpected or missing schema routines")
    for name, _, owner, definer, options in rows:
        generator = name in {"metadata_audit_equipment_change", "metadata_audit_asset_change"}
        expected_owner = "inventory_metadata_audit_writer" if generator else (
            "inventory_metadata_audit_owner" if name == "metadata_audit_protect" else MIGRATOR)
        path = "search_path=pg_catalog" if name.startswith("maintenance_") else "search_path=pg_catalog, pg_temp"
        if owner != expected_owner or definer != generator or options != [path]:
            raise DeploymentError("Unexpected function ownership, execution rights, or search path")
    rows = connection.execute(text("""SELECT c.relname, t.tgname, p.proname, t.tgenabled,
        pn.nspname, t.tgtype, t.tgnargs, t.tgqual::text,
        t.tgconstraint, t.tgdeferrable, t.tginitdeferred, t.tgparentid,
        t.tgoldtable, t.tgnewtable, encode(t.tgargs, 'hex'),
        ARRAY(SELECT a.attname::text FROM pg_attribute a WHERE a.attrelid=t.tgrelid
              AND a.attnum=ANY(t.tgattr) ORDER BY a.attnum)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=:schema AND NOT t.tgisinternal"""), {"schema": schema}).all()
    if {(r[0], r[1], r[2]) for r in rows} != triggers or any(r[3] != 'O' or r[4] != schema for r in rows):
        raise DeploymentError("Unexpected, disabled, or missing triggers")
    for row in rows:
        table, name, function, _, _, kind, nargs = row[:7]
        expected_kind = 34 if name.endswith("truncate") else (
            17 if name in {"metadata_audit_equipment_change", "metadata_audit_asset_change"} else
            31 if name == "metadata_audit_no_rewrite" else
            27 if name == "maintenance_event_no_rewrite" or (name == "inventory_audit_immutable" and table != "inventory_audits") else
            11 if name.endswith("no_delete") or name == "inventory_audit_immutable" else
            19 if name == "maintenance_asset_hold_validate" else
            7 if name == "inventory_audit_child_validate" else 23)
        if kind != expected_kind or nargs != 0:
            raise DeploymentError("Trigger timing or events mismatch")
        # All approved user triggers are unconditional, ordinary, nondeferred
        # triggers with no transition tables/arguments or partition parent.
        # Only the maintenance asset guard has an UPDATE OF column restriction.
        expected_columns = ["maintenance_hold"] if name == "maintenance_asset_hold_validate" else []
        if tuple(row[7:15]) != (None, 0, False, False, 0, None, None, "") or list(row[15]) != expected_columns:
            raise DeploymentError("Unexpected trigger predicate or security properties")
    if revision in {C4, HEAD}:
        # Reuse read-only validators, never upgrade functions or test exceptions.
        from importlib import import_module
        audit = import_module("backend.alembic.versions.c4e8a17d92f6_metadata_audit")
        quoted = connection.dialect.identifier_preparer.quote_identifier(schema)
        audit._validate_incoming_memberships(connection)
        audit._validate_object_acls(connection, quoted, owner=audit.OWNER, writer=audit.WRITER)
        audit._validate_effective_privileges(connection, quoted, f"{quoted}.metadata_audit_events", f"{quoted}.metadata_audit_protect()")
        unsafe_roles = connection.scalar(text("""
            SELECT count(*) <> 3 OR bool_or(rolsuper OR rolcreatedb OR rolcreaterole
                OR rolreplication OR rolbypassrls OR rolcanlogin <> (rolname='inventory_app')
                OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid))
            FROM pg_roles r WHERE rolname IN
              ('inventory_app','inventory_metadata_audit_owner','inventory_metadata_audit_writer')
        """))
        unsafe_sources = connection.scalar(text("""
            SELECT has_schema_privilege('inventory_app', :schema, 'CREATE')
              OR has_schema_privilege('inventory_metadata_audit_writer', :schema, 'CREATE')
              OR has_parameter_privilege('inventory_app', 'session_replication_role', 'SET')
              OR has_any_column_privilege('inventory_app', :users, 'INSERT,UPDATE')
              OR has_table_privilege('inventory_app', :users, 'DELETE,TRUNCATE')
              OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=:schema AND c.relname IN ('users','equipment','equipment_assets')
                  AND (pg_has_role('inventory_app', c.relowner, 'MEMBER')
                       OR has_table_privilege('inventory_app', c.oid, 'TRIGGER')))
              OR EXISTS (SELECT 1 FROM pg_database WHERE datname=current_database()
                AND pg_get_userbyid(datdba) IN ('inventory_app','inventory_metadata_audit_writer'))
        """), {"schema": schema, "users": f"{quoted}.users"})
        if unsafe_roles or unsafe_sources:
            raise DeploymentError("Unsafe audit roles or source privileges")
    if revision == HEAD:
        for table in expected_tables(HEAD):
            if not table.startswith("inventory_audit"):
                continue
            qualified = f"{connection.dialect.identifier_preparer.quote_identifier(schema)}.{table}"
            if connection.scalar(text("""
                SELECT NOT has_table_privilege('inventory_app', :table, 'SELECT')
                  OR NOT has_table_privilege('inventory_app', :table, 'INSERT')
                  OR has_table_privilege('inventory_app', :table, 'UPDATE') <> :parent
                  OR has_table_privilege('inventory_app', :table, 'DELETE,TRUNCATE,TRIGGER,REFERENCES')
            """), {"table": qualified, "parent": table == "inventory_audits"}):
                raise DeploymentError("Unsafe inventory audit runtime permissions")


def demo_config(connection, settings, user):
    config = Config(str(ROOT / "alembic_nfc_demo.ini"))
    script = ScriptDirectory.from_config(config)
    if Path(script.dir).resolve() != (ROOT / "backend/alembic_nfc_demo").resolve():
        raise DeploymentError("Dedicated demo environment required")
    if [r.revision for r in script.walk_revisions()] != [
        HEAD, C4, B7, "a3c7e91b6d20", "ef14e8f62260", "df453131d3d8", "f9bc7a594a00"
    ]:
        raise DeploymentError("Unreviewed migration history")
    config.attributes.update(connection=connection, nfc_demo_identity=identity(settings, user))
    return config


def run_stage(connection, settings, user, before, after):
    with connection.begin():
        acquire_stage_lock(connection)
        check_connection(connection, settings, user)
        if checkpoint(connection, settings) != before:
            raise DeploymentError("Stage starting revision mismatch")
        config = demo_config(connection, settings, user)
        command.upgrade(config, after)
        check_connection(connection, settings, user)
        if checkpoint(connection, settings) != after:
            raise DeploymentError("Stage final revision mismatch")


def secure_password(message):
    # getpass otherwise falls back to echoing input when terminal control fails.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(message)


def deploy(settings, engine_factory=make_engine, prompt=secure_password, confirm=input):
    settings.validate()
    if confirm("Type INITIALIZE inventory_nfc_demo to permit migration writes: ") != "INITIALIZE inventory_nfc_demo":
        raise DeploymentError("Deployment not confirmed")
    with ExitStack() as resources:
        password = prompt("Migrator password (hidden): ")
        if not password:
            raise DeploymentError("An interactive migrator password is required")
        migrator = engine_factory(settings, MIGRATOR, password)
        password = None
        resources.callback(migrator.dispose)
        revision = read_checkpoint(migrator, settings)
        for before, after, role in STAGES:
            if revision != before:
                continue
            if role == "administrator":
                # No connection/transaction/lock is held while awaiting input.
                # Another runner may advance the checkpoint during this gap.
                # run_stage revalidates it under its own transaction lock and
                # aborts on a stale plan instead of replaying migration work.
                password = prompt("Deployment administrator password (hidden): ")
                if not password:
                    raise DeploymentError("An interactive administrator password is required")
                admin = engine_factory(settings, settings.administrator, password)
                password = None
                try:
                    with admin.connect() as connection:
                        run_stage(connection, settings, settings.administrator, before, after)
                finally:
                    admin.dispose()
            else:
                with migrator.connect() as connection:
                    run_stage(connection, settings, MIGRATOR, before, after)
            revision = after
        if read_checkpoint(migrator, settings) != HEAD:
            raise DeploymentError("Deployment did not reach the approved final revision")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, choices=[DATABASE])
    parser.add_argument("--server-address", required=True, help="Explicit loopback IP")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--administrator", required=True, help="Existing trusted superuser login")
    parser.add_argument("--schema", required=True)
    args = parser.parse_args()
    # Neither Alembic nor driver errors may log credentials or complete URLs.
    logging.disable(logging.CRITICAL)
    try:
        if not sys.stdin.isatty():
            raise DeploymentError("Interactive terminal required")
        deploy(Settings(**vars(args)))
    except (Exception, KeyboardInterrupt):
        print("NFC deployment stopped. Error details withheld; review target state before retrying.", file=sys.stderr)
        return 1
    print("NFC demonstration schema verified at the approved revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
