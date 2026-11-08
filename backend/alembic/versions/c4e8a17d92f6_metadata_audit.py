"""Add append-only metadata audit history.

Revision ID: c4e8a17d92f6
Revises: b7d2f90c4e61
"""
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c4e8a17d92f6"
down_revision = "b7d2f90c4e61"
branch_labels = None
depends_on = None

OWNER = "inventory_metadata_audit_owner"
WRITER = "inventory_metadata_audit_writer"
RUNTIME = "inventory_app"
SOURCES = (
    ("equipment", "EQUIPMENT", "equipment_id", "metadata_audit_equipment_change",
     ("name", "category", "manufacturer", "model_number", "description", "default_location", "purchase_price")),
    ("equipment_assets", "ASSET", "asset_id", "metadata_audit_asset_change",
     ("asset_code", "nfc_tag_id", "serial_number", "location")),
)


def _schema(connection):
    name = connection.scalar(sa.text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Metadata audit requires a trusted application schema")
    return name, connection.dialect.identifier_preparer.quote_identifier(name)


def _writer(connection, name, schema):
    # The existing isolated login cannot create or assume separate roles.
    if op.get_context().opts.get("metadata_audit_isolated_test"):
        identity = connection.execute(sa.text(
            "SELECT current_database(), current_user, session_user"
        )).one()
        if tuple(identity) != ("inventory_checkout_test", "inventory_checkout_test_login",
                               "inventory_checkout_test_login") or not re.fullmatch(r"checkout_it_[0-9a-f]{32}", name):
            raise RuntimeError("Single-owner metadata audit is restricted to isolated test schemas")
        return "inventory_checkout_test_login", False

    for role in (OWNER, WRITER, RUNTIME):
        flags = connection.execute(sa.text("""
            SELECT rolcanlogin, rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,
                   EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid)
            FROM pg_roles WHERE rolname = :role
        """), {"role": role}).one_or_none()
        if flags is None or flags[1] or flags[2] or flags[0] != (role == RUNTIME):
            raise RuntimeError(f"Unsafe or missing metadata audit role: {role}")
    _validate_incoming_memberships(connection)
    safe = connection.scalar(sa.text(f"""
        SELECT NOT has_schema_privilege('{RUNTIME}', :schema_name, 'CREATE')
           AND NOT has_schema_privilege('{WRITER}', :schema_name, 'CREATE')
           AND NOT has_parameter_privilege('{RUNTIME}', 'session_replication_role', 'SET')
           AND NOT EXISTS (
               SELECT 1 FROM pg_database WHERE datname = current_database()
                 AND pg_get_userbyid(datdba) IN ('{RUNTIME}', '{WRITER}')
           )
           AND NOT EXISTS (
               SELECT 1 FROM pg_class
               WHERE oid IN ('{schema}.users'::regclass, '{schema}.equipment'::regclass,
                             '{schema}.equipment_assets'::regclass)
                 AND (pg_has_role('{RUNTIME}', relowner, 'MEMBER')
                      OR has_table_privilege('{RUNTIME}', oid, 'TRIGGER'))
           )
           AND NOT has_any_column_privilege('{RUNTIME}', '{schema}.users', 'INSERT,UPDATE')
           AND NOT has_table_privilege('{RUNTIME}', '{schema}.users', 'DELETE,TRUNCATE')
    """), {"schema_name": name})
    if not safe:
        raise RuntimeError("Runtime must not control audit sources or staff identity records")
    return WRITER, True


def _validate_incoming_memberships(connection):
    # Every transitive path starts with a direct grant of a dedicated role.
    # Reject even INHERIT-only or ADMIN-only grants.
    if connection.scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_auth_members AS membership
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            WHERE granted.rolname IN (:owner, :writer)
        )
    """), {"owner": OWNER, "writer": WRITER}):
        raise RuntimeError("Incoming membership in a dedicated metadata audit role")


def _create_table(name):
    op.create_table(
        "metadata_audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), sa.ForeignKey(f"{name}.equipment.id", ondelete="RESTRICT")),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey(f"{name}.equipment_assets.id", ondelete="RESTRICT")),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey(f"{name}.users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("previous_values", JSONB(), nullable=False),
        sa.Column("new_values", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("entity_revision", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "(entity_type = 'EQUIPMENT' AND equipment_id IS NOT NULL AND equipment_id = entity_id AND asset_id IS NULL) OR "
            "(entity_type = 'ASSET' AND asset_id IS NOT NULL AND asset_id = entity_id AND equipment_id IS NULL)",
            name="ck_metadata_audit_entity",
        ),
        sa.CheckConstraint("action = 'UPDATE'", name="ck_metadata_audit_action"),
        sa.CheckConstraint("entity_revision >= 1", name="ck_metadata_audit_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(previous_values) = 'object' AND jsonb_typeof(new_values) = 'object' "
            "AND previous_values <> '{}'::jsonb AND previous_values <> new_values "
            "AND jsonb_path_query_array(previous_values, '$.keyvalue().key') = "
            "jsonb_path_query_array(new_values, '$.keyvalue().key')",
            name="ck_metadata_audit_diff",
        ),
        sa.UniqueConstraint("entity_type", "entity_id", "entity_revision", name="uq_metadata_audit_entity_revision"),
        sa.Index("ix_metadata_audit_actor_time", "actor_id", "occurred_at", "id"),
        schema=name,
    )


def _create_generator(schema, source, entity, typed_id, function, fields):
    # Values come directly from the typed OLD/NEW rows. The API owns input
    # normalization. A stored NUMERIC(12,2) cast to text retains exact cents.
    diff = []
    for field in fields:
        cast = "::text" if field == "purchase_price" else ""
        diff.append(f"""
    IF OLD.{field} IS DISTINCT FROM NEW.{field} THEN
        before_values := before_values || jsonb_build_object('{field}', OLD.{field}{cast});
        after_values := after_values || jsonb_build_object('{field}', NEW.{field}{cast});
    END IF;""")
    op.execute(f"""
CREATE FUNCTION {schema}.{function}() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    before_values jsonb := '{{}}'::jsonb;
    after_values jsonb := '{{}}'::jsonb;
    actor uuid;
    next_revision bigint;
BEGIN
    IF TG_OP <> 'UPDATE' OR TG_WHEN <> 'AFTER' OR TG_LEVEL <> 'ROW'
       OR TG_RELID <> '{schema}.{source}'::regclass THEN
        RAISE EXCEPTION 'Invalid metadata audit source' USING ERRCODE = '42501';
    END IF;
    {''.join(diff)}
    IF before_values = '{{}}'::jsonb THEN
        RETURN NULL;
    END IF;

    BEGIN
        actor := nullif(current_setting('inventory.metadata_actor_id', true), '')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'Invalid metadata audit actor' USING ERRCODE = '42501';
    END;
    IF NOT EXISTS (
        SELECT 1 FROM {schema}.users
        WHERE id = actor AND is_active AND role IN ('ADMIN', 'FACULTY', 'LAB_ASSISTANT')
    ) THEN
        RAISE EXCEPTION 'Invalid metadata audit actor' USING ERRCODE = '42501';
    END IF;

    -- The source UPDATE holds the entity row lock until transaction end.
    SELECT coalesce(max(entity_revision), 0) + 1 INTO next_revision
    FROM {schema}.metadata_audit_events WHERE entity_type = '{entity}' AND entity_id = NEW.id;
    INSERT INTO {schema}.metadata_audit_events
        (entity_type, entity_id, {typed_id}, actor_id, action, previous_values, new_values, entity_revision)
    VALUES ('{entity}', NEW.id, NEW.id, actor, 'UPDATE', before_values, after_values, next_revision);
    RETURN NULL;
END; $$;
""")
    op.execute(f"CREATE TRIGGER {function} AFTER UPDATE ON {schema}.{source} "
               f"FOR EACH ROW EXECUTE FUNCTION {schema}.{function}()")
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.{function}() FROM PUBLIC")


def upgrade():
    connection = op.get_bind()
    name, schema = _schema(connection)
    writer, production = _writer(connection, name, schema)
    table = f"{schema}.metadata_audit_events"
    guard = f"{schema}.metadata_audit_protect()"
    _create_table(name)
    op.execute(f"""
CREATE FUNCTION {guard} RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Defense in depth only: nesting does not identify the caller.
        -- Production trust comes from the validated ownership and ACLs.
        IF current_user <> '{writer}' OR pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION 'Direct metadata audit insertion is forbidden' USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Metadata audit history is immutable' USING ERRCODE = '55000';
END; $$;
""")
    op.execute(f"CREATE TRIGGER metadata_audit_no_rewrite BEFORE INSERT OR UPDATE OR DELETE ON {table} "
               f"FOR EACH ROW EXECUTE FUNCTION {guard}")
    op.execute(f"CREATE TRIGGER metadata_audit_no_truncate BEFORE TRUNCATE ON {table} "
               f"FOR EACH STATEMENT EXECUTE FUNCTION {guard}")
    op.execute(f"REVOKE ALL ON FUNCTION {guard} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
    for source in SOURCES:
        _create_generator(schema, *source)
    if production:
        _separate_privileges(connection, schema, table, guard)


def _separate_privileges(connection, schema, table, guard):
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {OWNER}, {WRITER}")
    op.execute(f"REVOKE ALL ON TABLE {table} FROM {WRITER}, {RUNTIME}")
    op.execute(f"GRANT SELECT ON TABLE {table} TO {RUNTIME}")
    op.execute(f"GRANT INSERT, SELECT (entity_type, entity_id, entity_revision) ON TABLE {table} TO {WRITER}")
    op.execute(f"GRANT SELECT (id, role, is_active) ON {schema}.users TO {WRITER}")
    op.execute(f"ALTER TABLE {table} OWNER TO {OWNER}")
    op.execute(f"REVOKE ALL ON FUNCTION {guard} FROM {RUNTIME}")
    op.execute(f"ALTER FUNCTION {guard} OWNER TO {OWNER}")
    for _, _, _, function, _ in SOURCES:
        qualified = f"{schema}.{function}()"
        op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM {RUNTIME}")
        op.execute(f"ALTER FUNCTION {qualified} OWNER TO {WRITER}")
    _validate_object_acls(connection, schema, owner=OWNER, writer=WRITER)
    _validate_effective_privileges(connection, schema, table, guard)


def _validate_object_acls(connection, schema, *, owner, writer):
    table = f"{schema}.metadata_audit_events"
    params = {"table": table, "owner": owner, "writer": writer, "runtime": RUNTIME}
    if connection.scalar(sa.text("""
        SELECT pg_get_userbyid(c.relowner) <> :owner OR EXISTS (
            SELECT 1 FROM aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) AS acl
            WHERE acl.grantee <> c.relowner AND NOT (
                acl.grantee <> 0 AND acl.grantor = c.relowner AND NOT acl.is_grantable AND (
                    (pg_get_userbyid(acl.grantee) = :writer AND acl.privilege_type = 'INSERT')
                    OR (pg_get_userbyid(acl.grantee) = :runtime AND acl.privilege_type = 'SELECT')
                )
            )
        ) OR EXISTS (
            SELECT 1 FROM pg_attribute AS a
            CROSS JOIN LATERAL aclexplode(a.attacl) AS acl
            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
              AND acl.grantee <> c.relowner AND NOT (
                  acl.grantee <> 0 AND acl.grantor = c.relowner AND NOT acl.is_grantable
                  AND pg_get_userbyid(acl.grantee) = :writer AND acl.privilege_type = 'SELECT'
                  AND a.attname IN ('entity_type', 'entity_id', 'entity_revision')
              )
        )
        FROM pg_class AS c WHERE c.oid = CAST(:table AS regclass)
    """), params):
        raise RuntimeError("Unexpected metadata audit table or column privileges")

    functions = [(f"{schema}.{source[3]}()", writer, True) for source in SOURCES]
    functions.append((f"{schema}.metadata_audit_protect()", owner, False))
    for function, expected_owner, definer in functions:
        if connection.scalar(sa.text("""
            SELECT pg_get_userbyid(p.proowner) <> :owner OR p.prosecdef <> :definer OR EXISTS (
                SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS acl
                WHERE acl.grantee <> p.proowner
            )
            FROM pg_proc AS p WHERE p.oid = CAST(:function AS regprocedure)
        """), {"function": function, "owner": expected_owner, "definer": definer}):
            raise RuntimeError("Unexpected metadata audit function privileges")


def _validate_effective_privileges(connection, schema, table, guard):
    for function in [guard, *(f"{schema}.{source[3]}()" for source in SOURCES)]:
        if connection.scalar(sa.text(
            "SELECT has_function_privilege(:runtime, :function, 'EXECUTE')"
        ), {"runtime": RUNTIME, "function": function}):
            raise RuntimeError("Runtime can execute a metadata audit function")
    if connection.scalar(sa.text(f"""
        SELECT NOT has_table_privilege('{RUNTIME}', '{table}', 'SELECT')
            OR has_any_column_privilege('{RUNTIME}', '{table}', 'INSERT,UPDATE,REFERENCES')
            OR has_table_privilege('{RUNTIME}', '{table}', 'DELETE,TRUNCATE,TRIGGER')
            OR NOT has_table_privilege('{WRITER}', '{table}', 'INSERT')
            OR has_any_column_privilege('{WRITER}', '{table}', 'UPDATE,REFERENCES')
            OR has_table_privilege('{WRITER}', '{table}', 'DELETE,TRUNCATE,TRIGGER')
    """)):
        raise RuntimeError("Unsafe effective metadata audit privileges")
    if connection.scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_proc
            WHERE proowner = CAST(:writer AS regrole) AND prosecdef
              AND oid NOT IN (CAST(:equipment AS regprocedure), CAST(:asset AS regprocedure))
        )
    """), {"writer": WRITER, "equipment": f"{schema}.{SOURCES[0][3]}()",
           "asset": f"{schema}.{SOURCES[1][3]}()"}):
        raise RuntimeError("Unexpected SECURITY DEFINER routine owned by the metadata audit writer")


def downgrade():
    connection = op.get_bind()
    name, schema = _schema(connection)
    table = f"{schema}.metadata_audit_events"
    op.execute(f"LOCK TABLE {schema}.equipment_assets, {schema}.equipment IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM {table}) THEN
        RAISE EXCEPTION 'Cannot discard populated metadata audit history' USING ERRCODE = '55000';
    END IF;
END; $$;
""")
    for source, _, _, function, _ in SOURCES:
        op.execute(f"DROP TRIGGER {function} ON {schema}.{source}")
        op.execute(f"DROP FUNCTION {schema}.{function}()")
    op.drop_table("metadata_audit_events", schema=name)
    op.execute(f"DROP FUNCTION {schema}.metadata_audit_protect()")
