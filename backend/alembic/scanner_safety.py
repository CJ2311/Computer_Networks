"""Verify private scanner tables and exact executable staging routines."""
from importlib import import_module

from sqlalchemy import CheckConstraint, inspect, text

from backend.app.models.scanners import EquipmentScanner, ScannerScanEvent

TABLES = {"equipment_scanners", "scanner_scan_events"}


def verify_scanner_staging(c, schema):
    migration = import_module("backend.alembic.application_versions.c3a7e9b120df_scanner_ingestion")
    inspector = inspect(c)
    schema_name = c.scalar(text("SELECT current_schema()"))
    for model in (EquipmentScanner, ScannerScanEvent):
        table = model.__table__
        qualified = f"{schema}.{table.name}"
        unsafe = c.scalar(text("""SELECT NOT EXISTS(SELECT 1 FROM pg_class cl
            WHERE cl.oid=to_regclass(:table) AND pg_get_userbyid(cl.relowner)=:owner AND cl.relkind='r'
            AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(cl.relacl,acldefault('r',cl.relowner))) a WHERE a.grantee<>cl.relowner))
            OR has_any_column_privilege('inventory_app',:table,'SELECT,INSERT,UPDATE,REFERENCES')
            OR has_table_privilege('inventory_app',:table,'DELETE,TRUNCATE,TRIGGER')
            OR EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=CAST(:table AS regclass) AND attacl IS NOT NULL)
            OR EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND NOT convalidated)"""),
            {"table": qualified, "owner": migration.OWNER})
        if unsafe is not False:
            raise RuntimeError("Unsafe scanner table ownership or privileges")
        columns = inspector.get_columns(table.name, schema=schema_name)
        if {item["name"] for item in columns} != set(table.columns.keys()):
            raise RuntimeError("Unexpected scanner table columns")
        for item in columns:
            expected = table.c[item["name"]]
            if (item["nullable"] != expected.nullable or str(item["type"].compile(dialect=c.dialect)).upper()
                    != str(expected.type.compile(dialect=c.dialect)).upper()):
                raise RuntimeError("Altered scanner column type or nullability")
            default = str(expected.server_default.arg) if expected.server_default is not None else None
            if item.get("default") != default:
                raise RuntimeError("Altered scanner column default")
        if inspector.get_pk_constraint(table.name, schema=schema_name)["constrained_columns"] != [col.name for col in table.primary_key]:
            raise RuntimeError("Altered scanner primary key")
        expected_checks = {item.name for item in table.constraints if isinstance(item, CheckConstraint)}
        if {item["name"] for item in inspector.get_check_constraints(table.name, schema=schema_name)} != expected_checks:
            raise RuntimeError("Altered scanner check constraints")
        expected_unique = {tuple(col.name for col in item.columns) for item in table.constraints
                           if item.__class__.__name__ == "UniqueConstraint"}
        if {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name, schema=schema_name)} != expected_unique:
            raise RuntimeError("Altered scanner uniqueness constraints")
        foreign = inspector.get_foreign_keys(table.name, schema=schema_name)
        actual = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"])) for item in foreign}
        expected = {(tuple(col.name for col in item.columns), item.elements[0].column.table.name,
                     tuple(element.column.name for element in item.elements)) for item in table.foreign_key_constraints}
        if actual != expected or any(
            item.get("referred_schema") not in {None, schema_name}
            or (item.get("options", {}).get("ondelete") or "NO ACTION") != "NO ACTION"
            or (item.get("options", {}).get("onupdate") or "NO ACTION") != "NO ACTION" for item in foreign
        ):
            raise RuntimeError("Altered scanner foreign keys")
    unsafe = c.scalar(text("""SELECT NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class cl ON cl.oid=i.indexrelid
        WHERE i.indrelid=CAST(:events AS regclass) AND cl.relname='ix_scanner_events_expiry'
        AND i.indisvalid AND i.indisready AND NOT i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL
        AND i.indnkeyatts=1 AND i.indnatts=1
        AND i.indkey::text=(SELECT attnum::text FROM pg_attribute WHERE attrelid=i.indrelid AND attname='expires_at'))
        OR NOT has_column_privilege(:owner,:assets,'id','SELECT')
        OR NOT has_column_privilege(:owner,:assets,'nfc_tag_id','SELECT')
        OR has_any_column_privilege(:owner,:assets,'INSERT,UPDATE,REFERENCES')
        OR has_table_privilege(:owner,:assets,'DELETE,TRUNCATE,TRIGGER')
        OR NOT EXISTS(SELECT 1 FROM pg_roles r WHERE rolname=:owner AND NOT
            (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
            AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid))"""),
        {"events": f"{schema}.scanner_scan_events", "assets": f"{schema}.equipment_assets", "owner": migration.OWNER})
    if unsafe is not False:
        raise RuntimeError("Unsafe scanner source privileges or expiry index")
    for name, body in migration.routine_bodies(schema).items():
        names = [part.split()[0] for part in migration.ARGUMENTS[name].split(",")]
        unsafe = c.scalar(text("""SELECT NOT EXISTS(SELECT 1 FROM pg_proc p WHERE oid=to_regprocedure(:name)
            AND pg_get_userbyid(p.proowner)=:owner AND p.prosecdef AND p.prokind='f'
            AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql') AND p.prosrc=:body
            AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp'] AND p.prorettype='jsonb'::regtype
            AND NOT p.proretset AND p.pronargs=:argument_count AND p.proargnames=CAST(:arguments AS text[])
            AND p.proargmodes IS NULL AND p.proargdefaults IS NULL AND p.provariadic=0
            AND NOT p.proisstrict AND NOT p.proleakproof AND p.provolatile='v' AND p.proparallel='u' AND p.prosupport=0
            AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                WHERE a.grantee<>p.proowner AND NOT(a.grantee='inventory_app'::regrole AND a.privilege_type='EXECUTE'
                    AND NOT a.is_grantable AND a.grantor=p.proowner))
            AND has_function_privilege('inventory_app',p.oid,'EXECUTE'))"""),
            {"name": f"{schema}.{name}{migration.SIGNATURES[name]}", "owner": migration.OWNER,
             "body": body, "arguments": names, "argument_count": len(names)})
        if unsafe is not False:
            raise RuntimeError("Missing or altered scanner staging routine")
