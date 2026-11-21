"""Revision-specific borrower routine, schema and effective privilege verification."""
from importlib import import_module

from sqlalchemy import text


def verify_borrower_creation(c, schema):
    migration = import_module("backend.alembic.application_versions.b2d4e6f810ac_borrower_registration")
    parameters = {"users": f"{schema}.users", "name": f"{schema}.{migration.FUNCTION}{migration.SIGNATURE}",
                  "owner": migration.OWNER, "body": migration.routine_body(schema),
                  "trim_chars": migration.TRIM_CHARACTERS}
    unsafe = c.scalar(text("""SELECT NOT EXISTS (
        SELECT 1 FROM pg_proc p WHERE p.oid=to_regprocedure(:name)
        AND pg_get_userbyid(p.proowner)=:owner AND p.prosecdef AND p.prokind='f'
        AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
        AND p.prosrc=:body AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        AND p.prorettype='record'::regtype AND p.proretset AND p.pronargs=4
        AND p.proargdefaults IS NULL AND p.provariadic=0 AND NOT p.proisstrict
        AND p.proargnames=ARRAY['p_actor','p_new_id','p_name','p_registration','id','college_id','full_name','created']
        AND p.proargmodes=ARRAY['i','i','i','i','t','t','t','t']::"char"[]
        AND NOT p.proleakproof AND p.provolatile='v' AND p.proparallel='u' AND p.prosupport=0
        AND pg_get_function_result(p.oid)='TABLE(id uuid, college_id text, full_name text, created boolean)'
        AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
            WHERE a.grantee<>p.proowner AND NOT(a.grantee='inventory_app'::regrole
                AND a.privilege_type='EXECUTE' AND NOT a.is_grantable AND a.grantor=p.proowner))
        AND has_function_privilege('inventory_app',p.oid,'EXECUTE'))"""), parameters)
    if unsafe is not False:
        raise RuntimeError("Missing or altered borrower registration routine")
    unsafe = c.scalar(text("""SELECT
        has_table_privilege(:owner,:users,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
        OR has_any_column_privilege('inventory_app',:users,'INSERT,UPDATE,REFERENCES')
        OR has_table_privilege('inventory_app',:users,'DELETE,TRUNCATE,TRIGGER')
        OR EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname=:owner AND
            (r.rolcanlogin OR r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls
             OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=r.oid OR member=r.oid)))
        OR EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid=CAST(:users AS regclass)
            AND a.attnum>0 AND NOT a.attisdropped AND (
                has_column_privilege(:owner,:users,a.attname,'SELECT') <>
                    (a.attname IN ('id','college_id','full_name','role','is_active'))
                OR has_column_privilege(:owner,:users,a.attname,'INSERT') <>
                    (a.attname IN ('id','college_id','full_name','email','role','is_active'))
                OR has_column_privilege(:owner,:users,a.attname,'UPDATE,REFERENCES')
                OR EXISTS(SELECT 1 FROM aclexplode(a.attacl) acl
                    WHERE acl.grantee=CAST(:owner AS regrole) AND acl.is_grantable)))"""), parameters)
    if unsafe is not False:
        raise RuntimeError("Unsafe borrower creation privileges")
    unsafe = c.scalar(text("""SELECT
        NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=CAST(:users AS regclass)
            AND attname='email' AND NOT attnotnull AND NOT attisdropped)
        OR NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=CAST(:users AS regclass)
            AND conname='ck_users_staff_email' AND contype='c' AND convalidated AND NOT connoinherit
            AND regexp_replace(replace(pg_get_expr(conbin,conrelid),'::text',''),
                '[[:space:]()"]','','g')=$check$role='STUDENT'ORemailISNOTNULL$check$)
        OR EXISTS(SELECT 1 FROM unnest(ARRAY['email','college_id']) AS fields(field) WHERE NOT EXISTS (
            SELECT 1 FROM pg_constraint con JOIN pg_attribute a ON a.attrelid=con.conrelid
            AND a.attname=fields.field WHERE con.conrelid=CAST(:users AS regclass) AND con.contype='u'
            AND con.conkey=ARRAY[a.attnum] AND NOT con.condeferrable AND con.convalidated))
        OR NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
            WHERE i.indrelid=CAST(:users AS regclass) AND idx.relname='uq_users_registration_normalized'
            AND i.indisunique AND i.indisvalid AND i.indisready AND i.indimmediate
            AND i.indnkeyatts=1 AND i.indnatts=1 AND i.indpred IS NULL AND i.indexprs IS NOT NULL
            AND regexp_replace(replace(replace(pg_get_expr(i.indexprs,i.indrelid),
                quote_literal(CAST(:trim_chars AS text)),'$trim$'),'::text',''),
                '[[:space:]()"]','','g')='upperbtrimcollege_id,$trim$COLLATEC')"""), parameters)
    if unsafe is not False:
        raise RuntimeError("Altered borrower identity or email constraints")
