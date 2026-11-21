"""Add borrower-only creation without runtime users INSERT/UPDATE privileges."""
import re

from alembic import op
from sqlalchemy import text

revision = "b2d4e6f810ac"
down_revision = "f1a6b8c920de"
branch_labels = None
depends_on = None
OWNER = "inventory_metadata_audit_owner"
FUNCTION = "borrower_resolve_or_create"
SIGNATURE = "(uuid,uuid,text,text)"
TRIM_CHARACTERS = "".join(chr(code) for code in (
    *range(9, 14), *range(28, 33), 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
))
SQL_TRIM = "U&'" + "".join(f"\\{ord(character):04X}" for character in TRIM_CHARACTERS) + "'"


def normalized_registration(column):
    # Column names are fixed migration literals, never request input.
    return f'upper(btrim({column}, {SQL_TRIM}) COLLATE "C")'


INDEX_EXPRESSION = normalized_registration("college_id")


def routine_body(schema):
    return f"""
DECLARE existing record; inserted_id uuid;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM {schema}.users u WHERE u.id=p_actor AND u.is_active
                   AND u.role IN ('FACULTY','LAB_ASSISTANT')) THEN
        RAISE EXCEPTION 'Active authorized staff required' USING ERRCODE='42501';
    END IF;
    IF p_new_id IS NULL OR p_name IS NULL OR p_registration IS NULL
       OR char_length(p_name) NOT BETWEEN 1 AND 150 OR p_name<>btrim(p_name,{SQL_TRIM})
       OR p_name ~ '[[:cntrl:]]' OR p_name !~ '[[:alpha:]]'
       OR char_length(p_registration) NOT BETWEEN 1 AND 50
       OR p_registration<>{normalized_registration('p_registration')}
       OR p_registration !~ '^[A-Z0-9][A-Z0-9._/-]{{0,49}}$' THEN
        RAISE EXCEPTION 'Invalid borrower details' USING ERRCODE='22023';
    END IF;
    FOR attempt IN 1..3 LOOP
        SELECT u.id, u.college_id, u.full_name, u.role, u.is_active INTO existing
        FROM {schema}.users u WHERE {normalized_registration('u.college_id')}=p_registration;
        IF FOUND THEN
            IF existing.role<>'STUDENT' OR NOT existing.is_active OR btrim(existing.full_name,{SQL_TRIM})<>p_name THEN
                RAISE EXCEPTION 'Registration identity conflict' USING ERRCODE='P2001';
            END IF;
            RETURN QUERY SELECT existing.id, existing.college_id::text, existing.full_name::text, false;
            RETURN;
        END IF;
        INSERT INTO {schema}.users AS u (id,college_id,full_name,email,role,is_active)
        VALUES (p_new_id,p_registration,p_name,NULL,'STUDENT',true)
        ON CONFLICT DO NOTHING RETURNING u.id INTO inserted_id;
        IF FOUND THEN
            RETURN QUERY SELECT inserted_id, p_registration, p_name, true;
            RETURN;
        END IF;
        -- A competing INSERT committed. At READ COMMITTED the next statement sees
        -- it. Higher isolation must retry the entire transaction, never duplicate.
        IF EXISTS (SELECT 1 FROM {schema}.users u WHERE u.id=p_new_id) THEN
            RAISE EXCEPTION 'Borrower identifier conflict' USING ERRCODE='P2001';
        END IF;
    END LOOP;
    RAISE EXCEPTION 'Concurrent borrower registration; retry transaction' USING ERRCODE='40001';
END;
"""


def upgrade():
    c = op.get_bind()
    if c.dialect.name != "postgresql" or c.scalar(text(
        "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
    )) is not True:
        raise RuntimeError("Borrower migration requires a trusted deployment administrator")
    name = c.scalar(text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    q = c.dialect.identifier_preparer.quote_schema(name)
    if c.scalar(text("""SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
        OR rolreplication OR rolbypassrls OR EXISTS(SELECT 1 FROM pg_auth_members
        WHERE roleid=r.oid OR member=r.oid) FROM pg_roles r WHERE rolname=:owner"""),
        {"owner": OWNER}) is not False:
        raise RuntimeError("Borrower routine owner must remain an isolated NOLOGIN role")
    op.execute(f"LOCK TABLE {q}.users IN ACCESS EXCLUSIVE MODE")
    if c.scalar(text(f"""SELECT EXISTS(SELECT 1 FROM {q}.users
        GROUP BY {INDEX_EXPRESSION} HAVING count(*)>1)""")):
        raise RuntimeError("Legacy registration numbers collide after normalization; manual reconciliation required")
    op.alter_column("users", "email", nullable=True, schema=name)
    op.create_check_constraint("ck_users_staff_email", "users", "role = 'STUDENT' OR email IS NOT NULL", schema=name)
    op.execute(f"CREATE UNIQUE INDEX uq_users_registration_normalized ON {q}.users ({INDEX_EXPRESSION})")
    op.execute(f"GRANT SELECT(college_id,full_name) ON {q}.users TO {OWNER}")
    op.execute(f"GRANT INSERT(id,college_id,full_name,email,role,is_active) ON {q}.users TO {OWNER}")
    signature = f"{q}.{FUNCTION}{SIGNATURE}"
    op.execute(f"""CREATE FUNCTION {q}.{FUNCTION}(p_actor uuid,p_new_id uuid,p_name text,p_registration text)
        RETURNS TABLE(id uuid,college_id text,full_name text,created boolean)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $borrower${routine_body(q)}$borrower$""")
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, inventory_app, inventory_migrator, inventory_metadata_audit_writer")
    op.execute(f"ALTER FUNCTION {signature} OWNER TO {OWNER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO inventory_app")
    from backend.alembic.borrower_safety import verify_borrower_creation
    verify_borrower_creation(c, q)


def downgrade():
    raise RuntimeError("Borrower registration requires a separately reviewed forward migration")
