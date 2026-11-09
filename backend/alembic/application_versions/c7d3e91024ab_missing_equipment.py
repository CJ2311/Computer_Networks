"""Missing equipment incident lifecycle and append-only audit events.

Requires an already trusted deployment administrator; preserves all existing
role memberships, constraints, and immutability invariants.
"""

import re
from alembic import op
from sqlalchemy import text

revision = "c7d3e91024ab"
down_revision = "e8a4c2d190ab"
branch_labels = None
depends_on = None


def upgrade():
    c = op.get_bind()
    if c.dialect.name != "postgresql" or not c.scalar(
        text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
    ):
        raise RuntimeError("Application ownership migration requires a trusted deployment administrator")
    name = c.scalar(text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Trusted application schema required")
    q = c.dialect.identifier_preparer.quote_schema(name)
    owner = "inventory_metadata_audit_owner"
    for role in (owner, "inventory_metadata_audit_writer"):
        if (
            c.scalar(
                text(
                    """SELECT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
            OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=r.oid)
            FROM pg_roles r WHERE rolname=:role"""
                ),
                {"role": role},
            )
            is not False
        ):
            raise RuntimeError("Protected roles must remain isolated NOLOGIN roles")
    if c.scalar(text("SELECT has_schema_privilege('inventory_app', :schema, 'CREATE')"), {"schema": name}):
        raise RuntimeError("Runtime schema CREATE is forbidden")

    statements = [
        """CREATE TABLE {q}.missing_equipment_incidents (
            id uuid PRIMARY KEY,
            asset_id uuid NOT NULL REFERENCES {q}.equipment_assets(id) ON DELETE RESTRICT,
            operation_key uuid NOT NULL UNIQUE,
            reconcile_key uuid UNIQUE,
            report_fingerprint varchar(64) NOT NULL,
            reconcile_fingerprint varchar(64),
            status varchar(20) NOT NULL DEFAULT 'OPEN',
            version integer NOT NULL DEFAULT 1,
            reported_by uuid NOT NULL REFERENCES {q}.users(id) ON DELETE RESTRICT,
            reported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            report_reason text NOT NULL,
            police_report_number varchar(100),
            reconciled_by uuid REFERENCES {q}.users(id) ON DELETE RESTRICT,
            reconciled_at timestamptz,
            reconcile_reason text,
            recovered_condition varchar(30),
            previous_availability_status varchar(30) NOT NULL,
            previous_condition_status varchar(30) NOT NULL,
            resolved_availability_status varchar(30),
            resolved_condition_status varchar(30),
            CONSTRAINT ck_missing_incident_status CHECK (status IN ('OPEN', 'RECONCILED')),
            CONSTRAINT ck_missing_incident_version CHECK (version >= 1),
            CONSTRAINT ck_missing_recovered_condition CHECK (recovered_condition IS NULL OR recovered_condition IN ('WORKING', 'DAMAGED')),
            CONSTRAINT ck_missing_report_reason CHECK (report_reason ~ '[^[:space:]]' AND char_length(report_reason) <= 4000),
            CONSTRAINT ck_missing_reconcile_reason CHECK (reconcile_reason IS NULL OR (reconcile_reason ~ '[^[:space:]]' AND char_length(reconcile_reason) <= 4000)),
            CONSTRAINT ck_missing_incident_resolution CHECK (
                (status = 'OPEN' AND reconciled_by IS NULL AND reconciled_at IS NULL AND reconcile_reason IS NULL AND recovered_condition IS NULL AND resolved_availability_status IS NULL AND resolved_condition_status IS NULL AND reconcile_key IS NULL AND reconcile_fingerprint IS NULL) OR
                (status = 'RECONCILED' AND reconciled_by IS NOT NULL AND reconciled_at IS NOT NULL AND reconcile_reason IS NOT NULL AND recovered_condition IS NOT NULL AND resolved_availability_status IS NOT NULL AND resolved_condition_status IS NOT NULL AND reconcile_key IS NOT NULL AND reconcile_fingerprint IS NOT NULL AND reconciled_at >= reported_at)
            )
        )""",
        """CREATE UNIQUE INDEX uq_active_missing_incident_per_asset ON {q}.missing_equipment_incidents (asset_id) WHERE status = 'OPEN'""",
        """CREATE TABLE {q}.missing_equipment_events (
            id uuid PRIMARY KEY,
            incident_id uuid NOT NULL REFERENCES {q}.missing_equipment_incidents(id) ON DELETE RESTRICT,
            asset_id uuid NOT NULL REFERENCES {q}.equipment_assets(id) ON DELETE RESTRICT,
            sequence integer NOT NULL,
            event_type varchar(30) NOT NULL,
            actor_id uuid NOT NULL REFERENCES {q}.users(id) ON DELETE RESTRICT,
            occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            reason text NOT NULL,
            police_report_number varchar(100),
            from_availability_status varchar(30) NOT NULL,
            to_availability_status varchar(30) NOT NULL,
            from_condition_status varchar(30) NOT NULL,
            to_condition_status varchar(30) NOT NULL,
            from_maintenance_hold boolean NOT NULL,
            to_maintenance_hold boolean NOT NULL,
            CONSTRAINT uq_missing_event_sequence UNIQUE (incident_id, sequence),
            CONSTRAINT ck_missing_event_sequence CHECK (sequence >= 1),
            CONSTRAINT ck_missing_event_type CHECK (event_type IN ('REPORTED_MISSING', 'RECONCILED')),
            CONSTRAINT ck_missing_event_reason CHECK (reason ~ '[^[:space:]]' AND char_length(reason) <= 4000)
        )""",
        """CREATE TRIGGER missing_incident_no_delete BEFORE DELETE ON {q}.missing_equipment_incidents FOR EACH ROW EXECUTE FUNCTION {q}.operations_no_delete()""",
        """CREATE TRIGGER missing_incident_no_truncate BEFORE TRUNCATE ON {q}.missing_equipment_incidents FOR EACH STATEMENT EXECUTE FUNCTION {q}.operations_no_delete()""",
        """CREATE TRIGGER missing_event_no_delete BEFORE DELETE ON {q}.missing_equipment_events FOR EACH ROW EXECUTE FUNCTION {q}.operations_no_delete()""",
        """CREATE TRIGGER missing_event_no_truncate BEFORE TRUNCATE ON {q}.missing_equipment_events FOR EACH STATEMENT EXECUTE FUNCTION {q}.operations_no_delete()""",
        """CREATE TRIGGER missing_event_no_update BEFORE UPDATE ON {q}.missing_equipment_events FOR EACH ROW EXECUTE FUNCTION {q}.operations_no_delete()""",
    ]
    for statement in statements:
        op.execute(statement.format(q=q))

    tables = ("missing_equipment_incidents", "missing_equipment_events")
    for table in tables:
        op.execute(f"REVOKE ALL ON {q}.{table} FROM PUBLIC, inventory_app, inventory_migrator, inventory_metadata_audit_writer")
        op.execute(f"ALTER TABLE {q}.{table} OWNER TO {owner}")
        op.execute(f"GRANT SELECT ON {q}.{table} TO inventory_app")

    op.execute(f"GRANT INSERT, UPDATE ON {q}.missing_equipment_incidents TO inventory_app")
    op.execute(f"GRANT INSERT ON {q}.missing_equipment_events TO inventory_app")


def downgrade():
    raise RuntimeError("Immutable missing equipment history requires a separately reviewed retention migration")

