"""Immutable NFC inventory audits.

Revision ID: d6f2b8a931c0
Revises: c4e8a17d92f6
"""
import re
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d6f2b8a931c0"
down_revision = "c4e8a17d92f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('inventory_audits',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('location', sa.String(length=150), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='IN_PROGRESS', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('snapshot_sealed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_by', sa.UUID(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by', sa.UUID(), nullable=True),
    sa.Column('cancellation_reason', sa.Text(), nullable=True),
    sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('final_notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("(status = 'COMPLETED' AND snapshot_sealed AND completed_at IS NOT NULL AND completed_by IS NOT NULL AND reconciled_at IS NOT NULL AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_reason IS NULL) OR (status = 'CANCELLED' AND snapshot_sealed AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancellation_reason IS NOT NULL AND cancellation_reason ~ '[^[:space:]]' AND completed_at IS NULL AND completed_by IS NULL AND reconciled_at IS NULL AND final_notes IS NULL) OR (status = 'IN_PROGRESS' AND completed_at IS NULL AND completed_by IS NULL AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_reason IS NULL AND reconciled_at IS NULL AND final_notes IS NULL)", name='ck_inventory_audit_terminal_fields'),
    sa.CheckConstraint("name ~ '[^[:space:]]' AND location ~ '[^[:space:]]'", name='ck_inventory_audit_text'),
    sa.CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED', 'CANCELLED')", name='ck_inventory_audit_status'),
    sa.CheckConstraint('(completed_at IS NULL OR completed_at >= reconciled_at AND reconciled_at >= started_at) AND (cancelled_at IS NULL OR cancelled_at >= started_at)', name='ck_inventory_audit_times'),
    sa.CheckConstraint('(notes IS NULL OR char_length(notes) <= 4000) AND (final_notes IS NULL OR char_length(final_notes) <= 4000) AND (cancellation_reason IS NULL OR char_length(cancellation_reason) <= 4000)', name='ck_inventory_audit_notes'),
    sa.CheckConstraint('version >= 1', name='ck_inventory_audit_version'),
    sa.ForeignKeyConstraint(['cancelled_by'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['completed_by'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_inventory_audit_status_started', 'inventory_audits', ['status', 'started_at', 'id'], unique=False)
    op.create_table('inventory_audit_expected_assets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('audit_id', sa.UUID(), nullable=False),
    sa.Column('asset_id', sa.UUID(), nullable=False),
    sa.Column('in_scope', sa.Boolean(), nullable=False),
    sa.Column('equipment_name_at_start', sa.String(length=150), nullable=False),
    sa.Column('expected_location', sa.String(length=150), nullable=False),
    sa.Column('availability_at_start', sa.String(length=30), nullable=False),
    sa.Column('condition_at_start', sa.String(length=30), nullable=False),
    sa.Column('maintenance_hold_at_start', sa.Boolean(), nullable=False),
    sa.Column('asset_code_at_start', sa.String(length=50), nullable=False),
    sa.Column('nfc_tag_id_at_start', sa.String(length=100), nullable=True),
    sa.Column('checkout_id_at_start', sa.UUID(), nullable=True),
    sa.Column('maintenance_id_at_start', sa.UUID(), nullable=True),
    sa.Column('maintenance_version_at_start', sa.Integer(), nullable=True),
    sa.CheckConstraint("availability_at_start IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')", name='ck_inventory_audit_expected_availability'),
    sa.CheckConstraint("condition_at_start IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')", name='ck_inventory_audit_expected_condition'),
    sa.CheckConstraint('(maintenance_id_at_start IS NULL AND maintenance_version_at_start IS NULL) OR (maintenance_id_at_start IS NOT NULL AND maintenance_version_at_start IS NOT NULL AND maintenance_version_at_start >= 1)', name='ck_inventory_expected_maintenance'),
    sa.ForeignKeyConstraint(['asset_id'], ['equipment_assets.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['audit_id'], ['inventory_audits.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['checkout_id_at_start'], ['equipment_checkouts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['maintenance_id_at_start'], ['maintenance_requests.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('audit_id', 'asset_id', name='uq_inventory_audit_expected_asset'),
    sa.UniqueConstraint('audit_id', 'nfc_tag_id_at_start', name='uq_inventory_expected_tag'),
    sa.UniqueConstraint('id', 'audit_id', 'asset_id', name='uq_inventory_expected_identity')
    )
    op.create_table('inventory_audit_scans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('audit_id', sa.UUID(), nullable=False),
    sa.Column('idempotency_key', sa.UUID(), nullable=False),
    sa.Column('asset_id', sa.UUID(), nullable=True),
    sa.Column('expected_asset_id', sa.UUID(), nullable=True),
    sa.Column('duplicate_of_scan_id', sa.UUID(), nullable=True),
    sa.Column('nfc_tag_id', sa.String(length=100), nullable=False),
    sa.Column('scanned_by', sa.UUID(), nullable=False),
    sa.Column('scanned_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('observed_location', sa.String(length=150), nullable=False),
    sa.Column('result', sa.String(length=30), nullable=False),
    sa.CheckConstraint("(result = 'DUPLICATE') = (duplicate_of_scan_id IS NOT NULL)", name='ck_inventory_scan_duplicate'),
    sa.CheckConstraint("(result = 'UNKNOWN_TAG' AND asset_id IS NULL AND expected_asset_id IS NULL) OR (result = 'DUPLICATE' AND ((asset_id IS NULL AND expected_asset_id IS NULL) OR (asset_id IS NOT NULL AND expected_asset_id IS NOT NULL))) OR (result IN ('MATCHED', 'MISPLACED', 'UNEXPECTED') AND asset_id IS NOT NULL AND expected_asset_id IS NOT NULL)", name='ck_inventory_audit_scan_asset_resolution'),
    sa.CheckConstraint("nfc_tag_id ~ '[^[:space:]]' AND observed_location ~ '[^[:space:]]'", name='ck_inventory_scan_text'),
    sa.CheckConstraint("result IN ('MATCHED', 'MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG', 'DUPLICATE')", name='ck_inventory_audit_scan_result'),
    sa.CheckConstraint('duplicate_of_scan_id IS NULL OR duplicate_of_scan_id <> id', name='ck_inventory_scan_no_self_duplicate'),
    sa.ForeignKeyConstraint(['asset_id'], ['equipment_assets.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['audit_id'], ['inventory_audits.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['duplicate_of_scan_id', 'audit_id'], ['inventory_audit_scans.id', 'inventory_audit_scans.audit_id'], name='fk_inventory_scan_duplicate_audit', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['expected_asset_id', 'audit_id', 'asset_id'], ['inventory_audit_expected_assets.id', 'inventory_audit_expected_assets.audit_id', 'inventory_audit_expected_assets.asset_id'], name='fk_inventory_scan_expected', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scanned_by'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('audit_id', 'idempotency_key', name='uq_inventory_scan_idempotency'),
    sa.UniqueConstraint('id', 'audit_id', 'asset_id', name='uq_inventory_scan_identity'),
    sa.UniqueConstraint('id', 'audit_id', name='uq_inventory_audit_scan_id_audit')
    )
    op.create_index('ix_inventory_scan_audit_time', 'inventory_audit_scans', ['audit_id', 'scanned_at', 'id'], unique=False)
    op.create_index('uq_inventory_scan_first_tag', 'inventory_audit_scans', ['audit_id', 'nfc_tag_id'], unique=True, postgresql_where=sa.text('duplicate_of_scan_id IS NULL'))
    op.create_table('inventory_audit_discrepancies',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('audit_id', sa.UUID(), nullable=False),
    sa.Column('asset_id', sa.UUID(), nullable=True),
    sa.Column('expected_asset_id', sa.UUID(), nullable=True),
    sa.Column('scan_id', sa.UUID(), nullable=True),
    sa.Column('discrepancy_type', sa.String(length=30), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("(asset_id IS NULL AND expected_asset_id IS NULL AND scan_id IS NOT NULL AND discrepancy_type = 'UNKNOWN_TAG') OR (asset_id IS NOT NULL AND expected_asset_id IS NOT NULL)", name='ck_inventory_audit_discrepancy_reference'),
    sa.CheckConstraint("description ~ '[^[:space:]]' AND char_length(description) <= 4000", name='ck_inventory_audit_discrepancy_description'),
    sa.CheckConstraint("discrepancy_type IN ('NOT_VERIFIED', 'EXPECTED_ON_LOAN', 'EXPECTED_MAINTENANCE', 'MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG', 'STATE_CHANGED', 'CUSTODY_REVIEW')", name='ck_inventory_audit_discrepancy_type'),
    sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_inventory_discrepancy_details'),
    sa.ForeignKeyConstraint(['asset_id'], ['equipment_assets.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['audit_id'], ['inventory_audits.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['expected_asset_id', 'audit_id', 'asset_id'], ['inventory_audit_expected_assets.id', 'inventory_audit_expected_assets.audit_id', 'inventory_audit_expected_assets.asset_id'], name='fk_inventory_discrepancy_expected', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scan_id', 'audit_id', 'asset_id'], ['inventory_audit_scans.id', 'inventory_audit_scans.audit_id', 'inventory_audit_scans.asset_id'], name='fk_inventory_discrepancy_scan_asset', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scan_id', 'audit_id'], ['inventory_audit_scans.id', 'inventory_audit_scans.audit_id'], name='fk_inventory_discrepancy_scan_audit', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_inventory_discrepancy_audit', 'inventory_audit_discrepancies', ['audit_id', 'discrepancy_type', 'id'], unique=False)
    op.create_unique_constraint("uq_inventory_finding_expected", "inventory_audit_discrepancies",
                                ["audit_id", "expected_asset_id", "discrepancy_type"])
    op.create_unique_constraint("uq_inventory_finding_scan", "inventory_audit_discrepancies",
                                ["audit_id", "scan_id", "discrepancy_type"])
    _install_guards()
    _configure_runtime_privileges()


def _schema():
    connection = op.get_bind()
    name = connection.scalar(sa.text("SELECT current_schema()"))
    if not name or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) or name.startswith("pg_"):
        raise RuntimeError("Inventory audit requires a trusted application schema")
    return connection.dialect.identifier_preparer.quote_identifier(name)


def _configure_runtime_privileges():
    connection = op.get_bind()
    schema = _schema()
    tables = ("inventory_audits", "inventory_audit_expected_assets", "inventory_audit_scans", "inventory_audit_discrepancies")
    for table in tables:
        op.execute(f"REVOKE ALL ON TABLE {schema}.{table} FROM PUBLIC")
    if op.get_context().opts.get("metadata_audit_isolated_test"):
        identity = connection.execute(sa.text("SELECT current_database(), current_user, session_user")).one()
        name = connection.scalar(sa.text("SELECT current_schema()"))
        if tuple(identity) != ("inventory_checkout_test", "inventory_checkout_test_login", "inventory_checkout_test_login") or not re.fullmatch(r"checkout_it_[0-9a-f]{32}", name):
            raise RuntimeError("Single-owner audit tests require the verified dedicated schema")
        return
    # Add rights only on the new tables. Runtime must never own or attach
    # triggers to them. Unexpected default grants abort the migration.
    for table in tables:
        qualified = f"{schema}.{table}"
        op.execute(f"REVOKE ALL ON TABLE {qualified} FROM inventory_app")
        privileges = "SELECT, INSERT, UPDATE" if table == "inventory_audits" else "SELECT, INSERT"
        op.execute(f"GRANT {privileges} ON TABLE {qualified} TO inventory_app")
        unsafe = connection.scalar(sa.text("""
            SELECT pg_has_role('inventory_app', c.relowner, 'MEMBER') OR EXISTS (
                SELECT 1 FROM aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) acl
                WHERE acl.grantee <> c.relowner AND NOT (
                    acl.grantee <> 0 AND pg_get_userbyid(acl.grantee) = 'inventory_app'
                    AND NOT acl.is_grantable AND acl.privilege_type = ANY(CAST(:allowed AS text[]))
                )
            ) OR EXISTS (
                SELECT 1 FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) acl
                WHERE a.attrelid = c.oid AND a.attnum > 0 AND acl.grantee <> c.relowner
            )
            FROM pg_class c WHERE c.oid = CAST(:table AS regclass)
        """), {"table": qualified, "allowed": privileges.split(", ")})
        if unsafe:
            raise RuntimeError("Unsafe inventory audit ownership or default privileges")
        if connection.scalar(sa.text(
            "SELECT has_table_privilege('inventory_app', :table, 'DELETE,TRUNCATE,TRIGGER,REFERENCES')"
        ), {"table": qualified}):
            raise RuntimeError("Unsafe effective inventory audit privileges")


def _install_guards():
    schema = _schema()
    op.execute(f"""
CREATE FUNCTION {schema}.inventory_audit_no_rewrite() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'Inventory audit history is immutable' USING ERRCODE = '55000';
END; $$;
CREATE FUNCTION {schema}.inventory_audit_parent_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
DECLARE actor uuid; allowed_roles text[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'IN_PROGRESS' OR NEW.snapshot_sealed OR NEW.version <> 1 THEN
            RAISE EXCEPTION 'Audit must start with an unsealed snapshot' USING ERRCODE = '23514';
        END IF;
        actor := NEW.created_by;
        allowed_roles := ARRAY['ADMIN', 'FACULTY', 'LAB_ASSISTANT'];
    ELSE
        IF OLD.status <> 'IN_PROGRESS' THEN
            RAISE EXCEPTION 'Closed inventory audits are immutable' USING ERRCODE = '55000';
        END IF;
        IF (NEW.id, NEW.name, NEW.location, NEW.created_by, NEW.started_at, NEW.notes)
           IS DISTINCT FROM (OLD.id, OLD.name, OLD.location, OLD.created_by, OLD.started_at, OLD.notes)
           OR NEW.version <> OLD.version + 1 OR (OLD.snapshot_sealed AND NOT NEW.snapshot_sealed) THEN
            RAISE EXCEPTION 'Invalid inventory audit mutation' USING ERRCODE = '23514';
        END IF;
        IF NOT OLD.snapshot_sealed AND (NOT NEW.snapshot_sealed OR NEW.status <> 'IN_PROGRESS') THEN
            RAISE EXCEPTION 'Snapshot must be sealed before use' USING ERRCODE = '23514';
        END IF;
        IF NEW.status = 'COMPLETED' THEN
            actor := NEW.completed_by;
            allowed_roles := ARRAY['FACULTY'];
            IF EXISTS (
                SELECT 1 FROM {schema}.inventory_audit_expected_assets e
                WHERE e.audit_id = NEW.id AND e.in_scope
                  AND NOT EXISTS (SELECT 1 FROM {schema}.inventory_audit_scans s
                                  WHERE s.audit_id = NEW.id AND s.expected_asset_id = e.id)
                  AND NOT EXISTS (SELECT 1 FROM {schema}.inventory_audit_discrepancies d
                                  WHERE d.audit_id = NEW.id AND d.expected_asset_id = e.id
                                    AND d.discrepancy_type IN ('NOT_VERIFIED', 'EXPECTED_ON_LOAN', 'EXPECTED_MAINTENANCE'))
            ) OR EXISTS (
                SELECT 1 FROM {schema}.inventory_audit_scans s
                WHERE s.audit_id = NEW.id AND s.result IN ('MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG')
                  AND NOT EXISTS (SELECT 1 FROM {schema}.inventory_audit_discrepancies d
                                  WHERE d.audit_id = NEW.id AND d.scan_id = s.id
                                    AND d.discrepancy_type = s.result)
            ) THEN
                RAISE EXCEPTION 'Audit reconciliation is incomplete' USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.status = 'CANCELLED' THEN
            actor := NEW.cancelled_by;
            allowed_roles := ARRAY['ADMIN', 'FACULTY'];
        ELSE
            RETURN NEW;
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM {schema}.users WHERE id = actor AND is_active AND role = ANY(allowed_roles)) THEN
        RAISE EXCEPTION 'Active authorized audit staff required' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END; $$;
CREATE FUNCTION {schema}.inventory_audit_child_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    parent {schema}.inventory_audits%ROWTYPE;
    expected {schema}.inventory_audit_expected_assets%ROWTYPE;
    first_scan {schema}.inventory_audit_scans%ROWTYPE;
    predicted text;
BEGIN
    SELECT * INTO parent FROM {schema}.inventory_audits WHERE id = NEW.audit_id FOR UPDATE;
    IF NOT FOUND OR parent.status <> 'IN_PROGRESS' THEN
        RAISE EXCEPTION 'Audit is not open' USING ERRCODE = '55000';
    END IF;
    IF TG_TABLE_NAME = 'inventory_audit_expected_assets' THEN
        IF parent.snapshot_sealed OR NEW.in_scope IS DISTINCT FROM (NEW.expected_location = parent.location) THEN
            RAISE EXCEPTION 'Audit snapshot is sealed or scope is inconsistent' USING ERRCODE = '23514';
        END IF;
        IF (NEW.checkout_id_at_start IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM {schema}.equipment_checkouts WHERE id = NEW.checkout_id_at_start AND asset_id = NEW.asset_id
            )) OR (NEW.maintenance_id_at_start IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM {schema}.maintenance_requests WHERE id = NEW.maintenance_id_at_start AND asset_id = NEW.asset_id
            )) THEN
            RAISE EXCEPTION 'Snapshot custody references another asset' USING ERRCODE = '23503';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT parent.snapshot_sealed THEN
        RAISE EXCEPTION 'Audit snapshot is not sealed' USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'inventory_audit_scans' THEN
        IF NOT EXISTS (SELECT 1 FROM {schema}.users
                       WHERE id = NEW.scanned_by AND is_active AND role IN ('ADMIN', 'FACULTY', 'LAB_ASSISTANT')) THEN
            RAISE EXCEPTION 'Active authorized scan staff required' USING ERRCODE = '42501';
        END IF;
        IF NEW.scanned_at < parent.started_at THEN
            RAISE EXCEPTION 'Scan precedes audit' USING ERRCODE = '23514';
        END IF;
        SELECT * INTO expected FROM {schema}.inventory_audit_expected_assets
            WHERE audit_id = NEW.audit_id AND nfc_tag_id_at_start = NEW.nfc_tag_id;
        IF NEW.asset_id IS DISTINCT FROM expected.asset_id OR NEW.expected_asset_id IS DISTINCT FROM expected.id THEN
            RAISE EXCEPTION 'Scan asset resolution differs from snapshot' USING ERRCODE = '23503';
        END IF;
        SELECT * INTO first_scan FROM {schema}.inventory_audit_scans
            WHERE audit_id = NEW.audit_id AND nfc_tag_id = NEW.nfc_tag_id AND duplicate_of_scan_id IS NULL;
        IF FOUND THEN
            predicted := 'DUPLICATE';
            IF NEW.duplicate_of_scan_id IS DISTINCT FROM first_scan.id THEN
                RAISE EXCEPTION 'Invalid duplicate scan reference' USING ERRCODE = '23503';
            END IF;
        ELSIF expected.id IS NULL THEN predicted := 'UNKNOWN_TAG';
        ELSIF NOT expected.in_scope THEN predicted := 'UNEXPECTED';
        ELSIF NEW.observed_location <> expected.expected_location THEN predicted := 'MISPLACED';
        ELSE predicted := 'MATCHED';
        END IF;
        IF NEW.result <> predicted THEN
            RAISE EXCEPTION 'Invalid scan classification' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.scan_id IS NOT NULL THEN
        SELECT * INTO first_scan FROM {schema}.inventory_audit_scans WHERE id = NEW.scan_id AND audit_id = NEW.audit_id;
        IF NOT FOUND OR first_scan.asset_id IS DISTINCT FROM NEW.asset_id
           OR first_scan.expected_asset_id IS DISTINCT FROM NEW.expected_asset_id
           OR first_scan.result <> NEW.discrepancy_type
           OR first_scan.result NOT IN ('MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG') THEN
            RAISE EXCEPTION 'Discrepancy scan reference is inconsistent' USING ERRCODE = '23503';
        END IF;
    ELSE
        SELECT * INTO expected FROM {schema}.inventory_audit_expected_assets
            WHERE id = NEW.expected_asset_id AND audit_id = NEW.audit_id AND asset_id = NEW.asset_id;
        IF NOT FOUND OR (NEW.discrepancy_type NOT IN ('STATE_CHANGED', 'CUSTODY_REVIEW') AND NOT expected.in_scope) THEN
            RAISE EXCEPTION 'Discrepancy snapshot reference is inconsistent' USING ERRCODE = '23503';
        END IF;
        IF NEW.discrepancy_type NOT IN ('STATE_CHANGED', 'CUSTODY_REVIEW') THEN
            IF EXISTS (SELECT 1 FROM {schema}.inventory_audit_scans
                       WHERE audit_id = NEW.audit_id AND expected_asset_id = expected.id) THEN
                RAISE EXCEPTION 'Observed asset cannot be unverified' USING ERRCODE = '23514';
            END IF;
            IF expected.checkout_id_at_start IS NOT NULL THEN predicted := 'EXPECTED_ON_LOAN';
            ELSIF expected.maintenance_id_at_start IS NOT NULL OR expected.maintenance_hold_at_start THEN
                predicted := 'EXPECTED_MAINTENANCE';
            ELSE predicted := 'NOT_VERIFIED';
            END IF;
            IF NEW.discrepancy_type <> predicted THEN
                RAISE EXCEPTION 'Invalid unverified asset classification' USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END; $$;
""")
    for table in ("inventory_audits", "inventory_audit_expected_assets", "inventory_audit_scans", "inventory_audit_discrepancies"):
        if table == "inventory_audits":
            op.execute(f"CREATE TRIGGER inventory_audit_parent_validate BEFORE INSERT OR UPDATE ON {schema}.{table} "
                       f"FOR EACH ROW EXECUTE FUNCTION {schema}.inventory_audit_parent_guard()")
            operations = "DELETE"
        else:
            op.execute(f"CREATE TRIGGER inventory_audit_child_validate BEFORE INSERT ON {schema}.{table} "
                       f"FOR EACH ROW EXECUTE FUNCTION {schema}.inventory_audit_child_guard()")
            operations = "UPDATE OR DELETE"
        op.execute(f"CREATE TRIGGER inventory_audit_immutable BEFORE {operations} ON {schema}.{table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {schema}.inventory_audit_no_rewrite()")
        op.execute(f"CREATE TRIGGER inventory_audit_no_truncate BEFORE TRUNCATE ON {schema}.{table} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.inventory_audit_no_rewrite()")
    for function in ("inventory_audit_parent_guard", "inventory_audit_child_guard", "inventory_audit_no_rewrite"):
        op.execute(f"REVOKE ALL ON FUNCTION {schema}.{function}() FROM PUBLIC")


def downgrade():
    schema = _schema()
    # Lock the parent first, as all audit writers do.
    op.execute(f"LOCK TABLE {schema}.inventory_audits, {schema}.inventory_audit_expected_assets, "
               f"{schema}.inventory_audit_scans, {schema}.inventory_audit_discrepancies IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM {schema}.inventory_audits)
       OR EXISTS (SELECT 1 FROM {schema}.inventory_audit_expected_assets)
       OR EXISTS (SELECT 1 FROM {schema}.inventory_audit_scans)
       OR EXISTS (SELECT 1 FROM {schema}.inventory_audit_discrepancies) THEN
        RAISE EXCEPTION 'Cannot discard inventory audit history' USING ERRCODE = '55000';
    END IF;
END; $$;
""")
    for table in ("inventory_audit_discrepancies", "inventory_audit_scans", "inventory_audit_expected_assets", "inventory_audits"):
        op.drop_table(table)
    for function in ("inventory_audit_child_guard", "inventory_audit_parent_guard", "inventory_audit_no_rewrite"):
        op.execute(f"DROP FUNCTION {schema}.{function}()")
