"""Freeze checkout custody and missing-incident report fields after insertion.

Preserve the existing return/reconciliation services and all earlier triggers.
"""
import re

from alembic import op
from sqlalchemy import text


revision = "f1a6b8c920de"
down_revision = "c7d3e91024ab"
branch_labels = None
depends_on = None

OWNER = "inventory_metadata_audit_owner"


def guard_bodies(schema):
    """Canonical PL/pgSQL bodies, also checked by deployment verification."""
    return {
        "checkout_history_guard": f"""
DECLARE note_prefix text;
BEGIN
    IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF (to_jsonb(NEW) - ARRAY['status','returned_at','returned_to','notes']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','returned_at','returned_to','notes']) THEN
        RAISE EXCEPTION 'Original checkout fields are immutable' USING ERRCODE='42501';
    END IF;
    IF OLD.status <> 'BORROWED' OR OLD.returned_at IS NOT NULL OR OLD.returned_to IS NOT NULL
       OR NEW.status <> 'RETURNED' OR NEW.returned_at IS NULL OR NEW.returned_to IS NULL
       OR NEW.returned_at < OLD.checked_out_at
       OR NOT EXISTS(SELECT 1 FROM {schema}.users WHERE id=NEW.returned_to
                     AND is_active AND role IN ('FACULTY','LAB_ASSISTANT')) THEN
        RAISE EXCEPTION 'Checkout permits only one authorized return' USING ERRCODE='42501';
    END IF;
    note_prefix := CASE WHEN OLD.notes IS NULL THEN '[RETURN] '
                        ELSE OLD.notes || E'\\n\\n[RETURN] ' END;
    IF NEW.notes IS NULL OR char_length(NEW.notes) <= char_length(note_prefix)
       OR left(NEW.notes, char_length(note_prefix)) IS DISTINCT FROM note_prefix THEN
        RAISE EXCEPTION 'Original checkout notes must be preserved' USING ERRCODE='42501';
    END IF;
    RETURN NEW;
END;
""",
        "missing_incident_history_guard": f"""
BEGIN
    IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF (to_jsonb(NEW) - ARRAY['status','version','reconciled_by','reconciled_at',
            'reconcile_reason','recovered_condition','resolved_availability_status',
            'resolved_condition_status','reconcile_key','reconcile_fingerprint']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','version','reconciled_by','reconciled_at',
            'reconcile_reason','recovered_condition','resolved_availability_status',
            'resolved_condition_status','reconcile_key','reconcile_fingerprint']) THEN
        RAISE EXCEPTION 'Original missing report fields are immutable' USING ERRCODE='42501';
    END IF;
    IF OLD.status <> 'OPEN' OR NEW.status <> 'RECONCILED'
       OR NEW.version::bigint <> OLD.version::bigint + 1
       OR NEW.reconciled_at IS NULL OR NEW.reconciled_at < OLD.reported_at
       OR NEW.reconcile_key IS NULL OR NEW.reconcile_fingerprint IS NULL
       OR NEW.reconcile_reason IS NULL OR btrim(NEW.reconcile_reason) = ''
       OR NEW.recovered_condition IS NULL OR NEW.resolved_availability_status IS NULL
       OR NEW.resolved_condition_status IS NULL
       OR NOT EXISTS(SELECT 1 FROM {schema}.users WHERE id=NEW.reconciled_by
                     AND is_active AND role IN ('ADMIN','FACULTY','LAB_ASSISTANT')) THEN
        RAISE EXCEPTION 'Missing incident permits only one authorized reconciliation' USING ERRCODE='42501';
    END IF;
    RETURN NEW;
END;
""",
    }


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql" or connection.scalar(text(
        "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
    )) is not True:
        raise RuntimeError("History protection migration requires a trusted deployment administrator")
    name = connection.scalar(text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    schema = connection.dialect.identifier_preparer.quote_schema(name)
    for role in (OWNER, "inventory_metadata_audit_writer"):
        if connection.scalar(text("""SELECT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb
            OR rolreplication OR rolbypassrls
            OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=r.oid)
            FROM pg_roles r WHERE rolname=:role"""), {"role": role}) is not False:
            raise RuntimeError("Protected roles must remain isolated NOLOGIN roles")
    if connection.scalar(text(
        "SELECT has_schema_privilege('inventory_app', :schema, 'CREATE')"
    ), {"schema": name}) is not False:
        raise RuntimeError("Runtime schema CREATE is forbidden")

    # Serialize installation with writers. No existing rows are rewritten.
    op.execute(f"LOCK TABLE {schema}.equipment_checkouts, {schema}.missing_equipment_incidents IN SHARE ROW EXCLUSIVE MODE")
    tables = {
        "checkout_history_guard": "equipment_checkouts",
        "missing_incident_history_guard": "missing_equipment_incidents",
    }
    for function, body in guard_bodies(schema).items():
        op.execute(f"""CREATE FUNCTION {schema}.{function}() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $guard${body}$guard$""")
        op.execute(f"REVOKE ALL ON FUNCTION {schema}.{function}() FROM PUBLIC, inventory_app, inventory_migrator, inventory_metadata_audit_writer")
        op.execute(f"ALTER FUNCTION {schema}.{function}() OWNER TO {OWNER}")
        op.execute(f"""CREATE TRIGGER {function} BEFORE UPDATE ON {schema}.{tables[function]}
FOR EACH ROW EXECUTE FUNCTION {schema}.{function}()""")


def downgrade():
    raise RuntimeError("Historical inventory protection requires a separately reviewed forward migration")
