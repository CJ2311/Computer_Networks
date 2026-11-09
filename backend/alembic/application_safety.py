"""Application extension validation, retaining the pinned NFC baseline checks."""
from importlib import import_module
from pathlib import Path
from alembic.config import Config
from alembic import command
from sqlalchemy import inspect, text
from backend.app.base import Base
from scripts import deploy_nfc_demo as baseline

HEAD="c7d3e91024ab"
PREVIOUS_REVISIONS={"e8a4c2d190ab"}
TABLES={"external_identities","equipment_loan_policies","loan_destinations","quantity_loans","stock_ledger","overdue_reminders","missing_equipment_incidents","missing_equipment_events"}
FUNCTIONS={"stock_history_protect","stock_change_capture","operations_no_delete","quantity_loan_guard","quantity_ledger_consistency"}
OWNER="inventory_metadata_audit_owner"


def verify_application(c,settings):
    schema=settings.schema
    q=c.dialect.identifier_preparer.quote_schema(schema)
    inspector=inspect(c)
    if set(inspector.get_table_names(schema=schema)) != baseline.expected_tables(baseline.HEAD)|TABLES or inspector.get_view_names(schema=schema):
        raise RuntimeError("Unexpected application schema objects")
    if c.scalar(text(f"SELECT version_num FROM {q}.alembic_version"))!=HEAD:
        raise RuntimeError("Unexpected application revision")
    for table in TABLES|{"equipment_stock"}:
        allowed={"SELECT"} | ({"INSERT","UPDATE"} if table in {"equipment_loan_policies","overdue_reminders","equipment_stock","quantity_loans","missing_equipment_incidents"} else {"INSERT"} if table in {"loan_destinations","missing_equipment_events"} else set())
        row=c.execute(text("""SELECT pg_get_userbyid(relowner),relowner FROM pg_class WHERE oid=CAST(:table AS regclass)"""),dict(table=f"{q}.{table}")).one()
        if row[0]!=OWNER:
            raise RuntimeError("Unexpected application object owner")
        grants=c.execute(text("""SELECT pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
            FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
            WHERE c.oid=CAST(:table AS regclass) AND a.grantee<>c.relowner"""),dict(table=f"{q}.{table}")).all()
        if {(g[0],g[1]) for g in grants}!={("inventory_app",privilege) for privilege in allowed} or any(g[2] for g in grants):
            raise RuntimeError("Unexpected application object privileges")
        if c.scalar(text("""SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=CAST(:table AS regclass) AND attacl IS NOT NULL)"""),dict(table=f"{q}.{table}")):
            raise RuntimeError("Unexpected application column privileges")
        if {col['name'] for col in inspector.get_columns(table,schema=schema)}!=set(Base.metadata.tables[table].columns.keys()):
            raise RuntimeError("Unexpected application columns")
    # The existing security validators remain authoritative for metadata audit ACLs.
    audit=import_module("backend.alembic.versions.c4e8a17d92f6_metadata_audit")
    audit._validate_incoming_memberships(c)
    audit._validate_object_acls(c,q,owner=audit.OWNER,writer=audit.WRITER)
    audit._validate_effective_privileges(c,q,f"{q}.metadata_audit_events",f"{q}.metadata_audit_protect()")
    if c.scalar(text("""SELECT count(*)<>3 OR bool_or(rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        OR rolcanlogin<>(rolname='inventory_app') OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid))
        FROM pg_roles r WHERE rolname IN ('inventory_app','inventory_metadata_audit_owner','inventory_metadata_audit_writer')""")):
        raise RuntimeError("Unsafe application role attributes or memberships")
    for role in ("inventory_app","inventory_metadata_audit_writer"):
        if c.scalar(text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),dict(role=role,schema=schema)):
            raise RuntimeError("Unsafe application schema privileges")
    if c.scalar(text("""SELECT has_parameter_privilege('inventory_app','session_replication_role','SET')
        OR has_any_column_privilege('inventory_app',:users,'INSERT,UPDATE')
        OR has_table_privilege('inventory_app',:users,'DELETE,TRUNCATE,TRIGGER')
        OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relkind='r' AND pg_get_userbyid(c.relowner)<>
                CASE WHEN c.relname IN ('external_identities','equipment_loan_policies','loan_destinations','quantity_loans','stock_ledger','overdue_reminders','equipment_stock','metadata_audit_events','missing_equipment_incidents','missing_equipment_events')
                     THEN 'inventory_metadata_audit_owner' ELSE 'inventory_migrator' END)
        OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database()
            AND pg_get_userbyid(datdba) IN ('inventory_app','inventory_metadata_audit_writer'))
        OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relkind NOT IN ('r','i'))
        OR EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
            WHERE n.nspname=:schema AND t.typrelid=0 AND t.typelem=0)"""),dict(schema=schema,users=f"{q}.users")):
        raise RuntimeError("Unsafe application source or schema privileges")
    routines=c.execute(text("SELECT proname,pronargs,prorettype='trigger'::regtype FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema"),dict(schema=schema)).all()
    known={"maintenance_history_no_rewrite","maintenance_request_guard","maintenance_asset_hold_guard","checkout_identifier_snapshots",
           "metadata_audit_protect","metadata_audit_equipment_change","metadata_audit_asset_change",
           "inventory_audit_parent_guard","inventory_audit_child_guard","inventory_audit_no_rewrite"}|FUNCTIONS
    if {(r[0],r[1],r[2]) for r in routines}!={(name,0,True) for name in known}:
        raise RuntimeError("Unexpected application routine catalog")
    for table in baseline.expected_tables(baseline.HEAD):
        if table.startswith("inventory_audit"):
            qualified=f"{q}.{table}"
            if c.scalar(text("""SELECT NOT has_table_privilege('inventory_app',:table,'SELECT')
                OR NOT has_table_privilege('inventory_app',:table,'INSERT')
                OR has_table_privilege('inventory_app',:table,'UPDATE')<>:parent
                OR has_table_privilege('inventory_app',:table,'DELETE,TRUNCATE,TRIGGER,REFERENCES')"""),dict(table=qualified,parent=table=='inventory_audits')):
                raise RuntimeError("Unsafe inventory audit runtime permissions")
    expected={
        ("maintenance_requests","maintenance_request_validate"):23,
        ("maintenance_requests","maintenance_request_no_delete"):11,
        ("maintenance_requests","maintenance_request_no_truncate"):34,
        ("maintenance_events","maintenance_event_no_rewrite"):27,
        ("maintenance_events","maintenance_event_no_truncate"):34,
        ("equipment_assets","maintenance_asset_hold_validate"):19,
        ("equipment_checkouts","checkout_identifier_snapshots"):23,
        ("metadata_audit_events","metadata_audit_no_rewrite"):31,
        ("metadata_audit_events","metadata_audit_no_truncate"):34,
        ("equipment","metadata_audit_equipment_change"):17,
        ("equipment_assets","metadata_audit_asset_change"):17,
        ("stock_ledger","stock_history_immutable"):31,
        ("stock_ledger","stock_history_no_truncate"):34,
        ("equipment_stock","stock_change_capture"):21,
        ("equipment_stock","stock_no_delete"):11,
        ("equipment_stock","stock_no_truncate"):34,
        ("quantity_loans","quantity_loan_validate"):23,
        ("quantity_loans","quantity_loan_no_delete"):11,
        ("quantity_loans","quantity_loan_no_truncate"):34,
        ("quantity_loans","quantity_ledger_consistent"):21,
        ("missing_equipment_incidents","missing_incident_no_delete"):11,
        ("missing_equipment_incidents","missing_incident_no_truncate"):34,
        ("missing_equipment_events","missing_event_no_delete"):11,
        ("missing_equipment_events","missing_event_no_truncate"):34,
        ("missing_equipment_events","missing_event_no_update"):19,
    }
    for table in ("inventory_audits","inventory_audit_expected_assets","inventory_audit_scans","inventory_audit_discrepancies"):
        parent=table=="inventory_audits"
        expected[table,"inventory_audit_parent_validate" if parent else "inventory_audit_child_validate"]=23 if parent else 7
        expected[table,"inventory_audit_immutable"]=11 if parent else 27
        expected[table,"inventory_audit_no_truncate"]=34
    rows=c.execute(text("""SELECT relname,tgname,tgtype,tgenabled,tgqual::text,tgnargs,
        ARRAY(SELECT a.attname::text FROM pg_attribute a WHERE a.attrelid=t.tgrelid AND a.attnum=ANY(t.tgattr) ORDER BY a.attnum),
        tgdeferrable,tginitdeferred,tgparentid,tgoldtable,tgnewtable,encode(tgargs,'hex'),
        p.proname,pn.nspname,pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig,tgconstraint
        FROM pg_trigger t JOIN pg_class cl ON cl.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE n.nspname=:schema AND NOT tgisinternal"""),dict(schema=schema)).all()
    if {(r[0],r[1]) for r in rows}!=set(expected):
        raise RuntimeError("Unexpected application trigger catalog")
    for r in rows:
        deferred=r[1]=="quantity_ledger_consistent"
        if (r[2]!=expected[r[0],r[1]] or r[3]!='O' or r[4] is not None or r[5]!=0 or
            (list(r[6]) != (["maintenance_hold"] if r[1]=="maintenance_asset_hold_validate" else [])) or r[7]!=deferred or r[8]!=deferred
            or r[9]!=0 or r[10] is not None or r[11] is not None or r[12] or bool(r[18])!=deferred):
            raise RuntimeError("Altered application trigger properties")
        function = {
            "maintenance_request_validate":"maintenance_request_guard", "maintenance_request_no_delete":"maintenance_history_no_rewrite",
            "maintenance_request_no_truncate":"maintenance_history_no_rewrite", "maintenance_event_no_rewrite":"maintenance_history_no_rewrite",
            "maintenance_event_no_truncate":"maintenance_history_no_rewrite", "maintenance_asset_hold_validate":"maintenance_asset_hold_guard",
            "metadata_audit_no_rewrite":"metadata_audit_protect", "metadata_audit_no_truncate":"metadata_audit_protect",
            "stock_history_immutable":"stock_history_protect", "stock_history_no_truncate":"stock_history_protect",
            "stock_no_delete":"operations_no_delete", "stock_no_truncate":"operations_no_delete",
            "quantity_loan_validate":"quantity_loan_guard", "quantity_loan_no_delete":"operations_no_delete",
            "quantity_loan_no_truncate":"operations_no_delete", "quantity_ledger_consistent":"quantity_ledger_consistency",
            "missing_incident_no_delete":"operations_no_delete", "missing_incident_no_truncate":"operations_no_delete",
            "missing_event_no_delete":"operations_no_delete", "missing_event_no_truncate":"operations_no_delete",
            "missing_event_no_update":"operations_no_delete",
            "inventory_audit_parent_validate":"inventory_audit_parent_guard", "inventory_audit_child_validate":"inventory_audit_child_guard",
            "inventory_audit_immutable":"inventory_audit_no_rewrite", "inventory_audit_no_truncate":"inventory_audit_no_rewrite",
        }.get(r[1],r[1])
        writer_function=function in {"metadata_audit_equipment_change","metadata_audit_asset_change"}
        expected_owner="inventory_metadata_audit_writer" if writer_function else OWNER if function in FUNCTIONS|{"metadata_audit_protect"} else baseline.MIGRATOR
        path="search_path=pg_catalog" if function.startswith("maintenance_") else "search_path=pg_catalog, pg_temp"
        if r[13:18]!=(function,schema,expected_owner,writer_function or function=="stock_change_capture",[path]):
            raise RuntimeError("Altered application trigger function")
    for name in FUNCTIONS:
        unsafe=c.scalar(text("""SELECT pg_get_userbyid(p.proowner)<>:owner OR p.prosecdef<>:definer
            OR p.proconfig<>ARRAY['search_path=pg_catalog, pg_temp']
            OR EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee<>p.proowner)
            FROM pg_proc p WHERE oid=CAST(:name AS regprocedure)"""),dict(owner=OWNER,definer=name=='stock_change_capture',name=f"{q}.{name}()"))
        if unsafe is not False:
            raise RuntimeError("Unsafe application function")


def apply_application(c,settings):
    settings.validate()
    with c.begin():
        baseline.acquire_stage_lock(c)
        baseline.check_connection(c,settings,settings.administrator)
        q=c.dialect.identifier_preparer.quote_schema(settings.schema)
        revisions=c.execute(text(f"SELECT version_num FROM {q}.alembic_version")).scalars().all()
        if revisions==[baseline.HEAD]:
            baseline.checkpoint(c,settings)
            config=Config(str(Path(__file__).resolve().parents[2]/"alembic_nfc_application.ini"))
            config.attributes.update(connection=c,nfc_demo_identity=baseline.identity(settings,settings.administrator))
            command.upgrade(config,HEAD)
        elif len(revisions)==1 and revisions[0] in PREVIOUS_REVISIONS:
            config=Config(str(Path(__file__).resolve().parents[2]/"alembic_nfc_application.ini"))
            config.attributes.update(connection=c,nfc_demo_identity=baseline.identity(settings,settings.administrator))
            command.upgrade(config,HEAD)
        elif revisions!=[HEAD]:
            raise RuntimeError("Application extension requires the completed NFC baseline or approved predecessor revision")
        baseline.check_connection(c,settings,settings.administrator)
        verify_application(c,settings)
