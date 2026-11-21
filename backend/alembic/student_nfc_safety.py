"""Fail-closed verification of private student NFC storage and exact routine."""
from importlib import import_module
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from backend.app.models.student_nfc import StudentNFCCard


def verify_student_nfc(c, q, *, routine_migration=None):
    # Only the forward migration explicitly requests predecessor validation.
    # Normal deployment verification always requires the corrected routine.
    migration = routine_migration or import_module('backend.alembic.application_versions.f6c1a8d420be_student_nfc_lock_time')
    parameters = {'table': f'{q}.student_nfc_cards', 'routine': f'{q}.student_nfc_manage{migration.SIGNATURE}',
                  'owner': migration.OWNER, 'body': migration.routine_body(q)}
    unsafe = c.scalar(text('''SELECT NOT EXISTS(SELECT 1 FROM pg_class cl WHERE cl.oid=to_regclass(:table)
        AND pg_get_userbyid(cl.relowner)=:owner AND cl.relkind='r'
        AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(cl.relacl,acldefault('r',cl.relowner))) a WHERE a.grantee<>cl.relowner))
        OR has_any_column_privilege('inventory_app',:table,'SELECT,INSERT,UPDATE,REFERENCES')
        OR has_table_privilege('inventory_app',:table,'DELETE,TRUNCATE,TRIGGER')
        OR EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=CAST(:table AS regclass) AND attacl IS NOT NULL)
        OR EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND NOT convalidated)
        OR EXISTS(SELECT 1 FROM pg_roles r WHERE rolname=:owner AND
            (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
             OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid)))'''), parameters)
    if unsafe is not False:
        raise RuntimeError('Unsafe student NFC table privileges or ownership')
    unsafe = c.scalar(text('''SELECT NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid=to_regprocedure(:routine)
        AND pg_get_userbyid(p.proowner)=:owner AND p.prosecdef AND p.prokind='f'
        AND p.prosrc=:body AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
        AND p.prorettype='jsonb'::regtype AND NOT p.proretset AND p.pronargs=4
        AND p.proargdefaults IS NULL AND p.provariadic=0 AND NOT p.proisstrict
        AND NOT p.proleakproof AND p.provolatile='v' AND p.proparallel='u' AND p.prosupport=0
        AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
            WHERE a.grantee<>p.proowner AND NOT(a.grantee='inventory_app'::regrole
                AND a.privilege_type='EXECUTE' AND NOT a.is_grantable AND a.grantor=p.proowner))
        AND has_function_privilege('inventory_app',p.oid,'EXECUTE'))'''), parameters)
    if unsafe is not False:
        raise RuntimeError('Missing or altered student NFC routine')
    schema = c.scalar(text('SELECT current_schema()'))
    inspector = inspect(c)
    table = StudentNFCCard.__table__
    columns = inspector.get_columns(table.name, schema=schema)
    if {item['name'] for item in columns} != set(table.columns.keys()):
        raise RuntimeError('Altered student NFC columns')
    for item in columns:
        column = table.c[item['name']]
        if item['nullable'] != column.nullable or str(item['type'].compile(dialect=c.dialect)).upper() != str(column.type.compile(dialect=c.dialect)).upper():
            raise RuntimeError('Altered student NFC column type')
    if inspector.get_pk_constraint(table.name, schema=schema)['constrained_columns'] != ['id']:
        raise RuntimeError('Altered student NFC primary key')
    expected_checks = {item.name for item in table.constraints if isinstance(item, CheckConstraint)}
    if {item['name'] for item in inspector.get_check_constraints(table.name, schema=schema)} != expected_checks:
        raise RuntimeError('Altered student NFC checks')
    expected_unique = {tuple(col.name for col in item.columns) for item in table.constraints if isinstance(item, UniqueConstraint)}
    if {tuple(item['column_names']) for item in inspector.get_unique_constraints(table.name, schema=schema)} != expected_unique:
        raise RuntimeError('Altered student NFC uniqueness')
    foreign = inspector.get_foreign_keys(table.name, schema=schema)
    if {(tuple(item['constrained_columns']), item['referred_table'], tuple(item['referred_columns'])) for item in foreign} != {
        (('student_id',), 'users', ('id',)), (('initiated_by',), 'users', ('id',))} or any(
            item.get('referred_schema') not in {None, schema}
            or (item.get('options', {}).get('ondelete') or 'NO ACTION') != 'NO ACTION'
            or (item.get('options', {}).get('onupdate') or 'NO ACTION') != 'NO ACTION' for item in foreign):
        raise RuntimeError('Altered student NFC foreign keys')
