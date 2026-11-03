"""Create maintenance database foundation without reclassifying existing assets.

Revision ID: a3c7e91b6d20
Revises: ef14e8f62260
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3c7e91b6d20'
down_revision: Union[str, Sequence[str], None] = 'ef14e8f62260'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('equipment_assets', sa.Column('maintenance_hold', sa.Boolean(), server_default='false', nullable=False))
    op.drop_constraint('ck_asset_availability', 'equipment_assets', type_='check')
    op.create_check_constraint('ck_asset_availability', 'equipment_assets', "availability_status IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')")
    op.create_check_constraint('ck_asset_available_serviceable', 'equipment_assets', "availability_status != 'AVAILABLE' OR (condition_status = 'WORKING' AND NOT maintenance_hold)")
    op.create_check_constraint('ck_asset_maintenance_requires_hold', 'equipment_assets', "availability_status != 'MAINTENANCE' OR maintenance_hold")
    op.create_table('maintenance_requests',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('asset_id', sa.UUID(), nullable=False),
        sa.Column('issue_type', sa.String(length=20), nullable=False),
        sa.Column('issue_description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=30), server_default='OPEN', nullable=False),
        sa.Column('reported_by', sa.UUID(), nullable=False),
        sa.Column('reported_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('assigned_to', sa.UUID(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('repair_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('repair_completed_by', sa.UUID(), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('verified_by', sa.UUID(), nullable=True),
        sa.Column('returned_to_service_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_by', sa.UUID(), nullable=True),
        sa.Column('cancellation_reason', sa.Text(), nullable=True),
        sa.Column('recovery_of_request_id', sa.UUID(), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.CheckConstraint("(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancellation_reason IS NOT NULL AND cancellation_reason ~ '[^[:space:]]' AND char_length(cancellation_reason) <= 4000) OR (status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_reason IS NULL)", name='ck_maintenance_cancellation_fields'),
        sa.CheckConstraint("(status = 'COMPLETED' AND verified_at IS NOT NULL AND verified_by IS NOT NULL AND returned_to_service_at IS NOT NULL) OR (status != 'COMPLETED' AND verified_at IS NULL AND verified_by IS NULL AND returned_to_service_at IS NULL)", name='ck_maintenance_verification_fields'),
        sa.CheckConstraint("issue_description ~ '[^[:space:]]' AND char_length(issue_description) <= 4000", name='ck_maintenance_description'),
        sa.CheckConstraint("issue_type IN ('DAMAGE', 'MALFUNCTION', 'INSPECTION')", name='ck_maintenance_issue_type'),
        sa.CheckConstraint("recovery_of_request_id IS NULL OR issue_type = 'INSPECTION'", name='ck_maintenance_recovery_inspection'),
        sa.CheckConstraint("status != 'OPEN' OR (started_at IS NULL AND repair_completed_at IS NULL)", name='ck_maintenance_open_fields'),
        sa.CheckConstraint("status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name='ck_maintenance_request_status'),
        sa.CheckConstraint("status NOT IN ('AWAITING_VERIFICATION', 'COMPLETED') OR repair_completed_at IS NOT NULL", name='ck_maintenance_repair_required'),
        sa.CheckConstraint("status NOT IN ('IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED') OR (started_at IS NOT NULL AND assigned_to IS NOT NULL)", name='ck_maintenance_work_fields'),
        sa.CheckConstraint('(repair_completed_at IS NULL) = (repair_completed_by IS NULL)', name='ck_maintenance_repair_actor'),
        sa.CheckConstraint('cancelled_at IS NULL OR (cancelled_at >= reported_at AND (started_at IS NULL OR cancelled_at >= started_at) AND (repair_completed_at IS NULL OR cancelled_at >= repair_completed_at))', name='ck_maintenance_cancellation_time'),
        sa.CheckConstraint('recovery_of_request_id IS NULL OR recovery_of_request_id != id', name='ck_maintenance_no_self_recovery'),
        sa.CheckConstraint('repair_completed_at IS NULL OR (started_at IS NOT NULL AND repair_completed_at >= started_at)', name='ck_maintenance_repair_time'),
        sa.CheckConstraint('returned_to_service_at IS NULL OR returned_to_service_at >= verified_at', name='ck_maintenance_service_time'),
        sa.CheckConstraint('started_at IS NULL OR started_at >= reported_at', name='ck_maintenance_start_time'),
        sa.CheckConstraint('verified_at IS NULL OR (repair_completed_at IS NOT NULL AND verified_at >= repair_completed_at AND verified_by != repair_completed_by)', name='ck_maintenance_independent_verification'),
        sa.CheckConstraint('version >= 1', name='ck_maintenance_version'),
        sa.ForeignKeyConstraint(['asset_id'], ['equipment_assets.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['assigned_to'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['cancelled_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['recovery_of_request_id', 'asset_id'], ['maintenance_requests.id', 'maintenance_requests.asset_id'], name='fk_maintenance_recovery_asset', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['repair_completed_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reported_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['verified_by'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'asset_id', name='uq_maintenance_request_id_asset')
    )
    op.create_index('ix_maintenance_asset_reported', 'maintenance_requests', ['asset_id', 'reported_at', 'id'], unique=False)
    op.create_index('ix_maintenance_assignee_status', 'maintenance_requests', ['assigned_to', 'status'], unique=False)
    op.create_index('ix_maintenance_reporter_reported', 'maintenance_requests', ['reported_by', 'reported_at', 'id'], unique=False)
    op.create_index('ix_maintenance_recovery', 'maintenance_requests', ['recovery_of_request_id'], unique=False)
    op.create_index('ix_maintenance_status_reported', 'maintenance_requests', ['status', 'reported_at', 'id'], unique=False)
    op.create_index('uq_active_maintenance_per_asset', 'maintenance_requests', ['asset_id'], unique=True, postgresql_where=sa.text("status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION')"))
    op.create_table('maintenance_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('request_id', sa.UUID(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=30), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('from_status', sa.String(length=30), nullable=True),
        sa.Column('to_status', sa.String(length=30), nullable=True),
        sa.Column('from_assigned_to', sa.UUID(), nullable=True),
        sa.Column('to_assigned_to', sa.UUID(), nullable=True),
        sa.Column('from_condition', sa.String(length=30), nullable=True),
        sa.Column('to_condition', sa.String(length=30), nullable=True),
        sa.Column('from_availability', sa.String(length=30), nullable=True),
        sa.Column('to_availability', sa.String(length=30), nullable=True),
        sa.Column('from_maintenance_hold', sa.Boolean(), nullable=True),
        sa.Column('to_maintenance_hold', sa.Boolean(), nullable=True),
        sa.CheckConstraint("event_type IN ('REPORTED', 'ASSIGNED', 'STARTED', 'NOTE_ADDED', 'REPAIR_COMPLETED', 'VERIFICATION_FAILED', 'VERIFIED', 'CANCELLED', 'RETURN_OBSERVED')", name='ck_maintenance_event_type'),
        sa.CheckConstraint("event_type NOT IN ('NOTE_ADDED', 'REPAIR_COMPLETED', 'VERIFICATION_FAILED', 'CANCELLED') OR note IS NOT NULL", name='ck_maintenance_event_note_required'),
        sa.CheckConstraint("from_availability IS NULL OR from_availability IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')", name='ck_maintenance_event_from_availability'),
        sa.CheckConstraint("from_condition IS NULL OR from_condition IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')", name='ck_maintenance_event_from_condition'),
        sa.CheckConstraint("from_status IS NULL OR from_status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name='ck_maintenance_event_from_status'),
        sa.CheckConstraint("note IS NULL OR (note ~ '[^[:space:]]' AND char_length(note) <= 5000)", name='ck_maintenance_event_note'),
        sa.CheckConstraint("to_availability IS NULL OR to_availability IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')", name='ck_maintenance_event_to_availability'),
        sa.CheckConstraint("to_condition IS NULL OR to_condition IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')", name='ck_maintenance_event_to_condition'),
        sa.CheckConstraint("to_status IS NULL OR to_status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name='ck_maintenance_event_to_status'),
        sa.CheckConstraint('sequence >= 1', name='ck_maintenance_event_sequence'),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['from_assigned_to'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['request_id'], ['maintenance_requests.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['to_assigned_to'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', 'sequence', name='uq_maintenance_event_sequence')
    )
    op.create_index('ix_maintenance_event_occurred', 'maintenance_events', ['request_id', 'occurred_at', 'id'], unique=False)
    op.create_foreign_key(
        'fk_maintenance_current_event', 'maintenance_requests', 'maintenance_events',
        ['id', 'version'], ['request_id', 'sequence'], ondelete='RESTRICT',
        deferrable=True, initially='DEFERRED',
    )
    _install_guards()


def _install_guards() -> None:
    # Invoker rights only; qualify dynamic table references with the trigger's
    # own schema so isolated test schemas and production schemas behave alike.
    op.execute("""
        CREATE FUNCTION maintenance_history_no_rewrite() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Maintenance history cannot be updated, deleted, or truncated'
                USING ERRCODE = '55000';
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION maintenance_request_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        DECLARE
            asset_hold boolean;
            parent_status text;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status IN ('COMPLETED', 'CANCELLED') THEN
                    RAISE EXCEPTION 'Terminal maintenance requests are immutable' USING ERRCODE = '55000';
                END IF;
                IF ROW(NEW.id, NEW.asset_id, NEW.issue_type, NEW.issue_description,
                       NEW.reported_by, NEW.reported_at, NEW.recovery_of_request_id)
                   IS DISTINCT FROM
                   ROW(OLD.id, OLD.asset_id, OLD.issue_type, OLD.issue_description,
                       OLD.reported_by, OLD.reported_at, OLD.recovery_of_request_id) THEN
                    RAISE EXCEPTION 'Original maintenance report fields are immutable' USING ERRCODE = '55000';
                END IF;
                IF NEW.version != OLD.version + 1 THEN
                    RAISE EXCEPTION 'Maintenance version must increase by one' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NEW.version != 1 THEN
                    RAISE EXCEPTION 'Initial maintenance version must be one' USING ERRCODE = '23514';
                END IF;
                IF NEW.recovery_of_request_id IS NOT NULL THEN
                    EXECUTE format('SELECT status FROM %I.maintenance_requests WHERE id = $1', TG_TABLE_SCHEMA)
                        INTO parent_status USING NEW.recovery_of_request_id;
                    IF parent_status IS NOT NULL AND parent_status != 'CANCELLED' THEN
                        RAISE EXCEPTION 'Recovery requires a cancelled request' USING ERRCODE = '23514';
                    END IF;
                END IF;
            END IF;
            IF NEW.status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION') THEN
                -- Future services must already hold the asset lock before a request
                -- UPDATE. This lock also serializes direct concurrent INSERTs.
                EXECUTE format('SELECT maintenance_hold FROM %I.equipment_assets WHERE id = $1 FOR UPDATE', TG_TABLE_SCHEMA)
                    INTO asset_hold USING NEW.asset_id;
                IF asset_hold IS FALSE THEN
                    RAISE EXCEPTION 'Active maintenance requires an asset hold' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION maintenance_asset_hold_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        DECLARE
            active_request boolean;
        BEGIN
            IF OLD.maintenance_hold AND NOT NEW.maintenance_hold THEN
                EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.maintenance_requests WHERE asset_id = $1 AND status IN (''OPEN'', ''IN_PROGRESS'', ''AWAITING_VERIFICATION''))', TG_TABLE_SCHEMA)
                    INTO active_request USING NEW.id;
                IF active_request THEN
                    RAISE EXCEPTION 'Cannot clear hold with active maintenance' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for statement in (
        "CREATE TRIGGER maintenance_request_validate BEFORE INSERT OR UPDATE ON maintenance_requests FOR EACH ROW EXECUTE FUNCTION maintenance_request_guard()",
        "CREATE TRIGGER maintenance_request_no_delete BEFORE DELETE ON maintenance_requests FOR EACH ROW EXECUTE FUNCTION maintenance_history_no_rewrite()",
        "CREATE TRIGGER maintenance_request_no_truncate BEFORE TRUNCATE ON maintenance_requests FOR EACH STATEMENT EXECUTE FUNCTION maintenance_history_no_rewrite()",
        "CREATE TRIGGER maintenance_event_no_rewrite BEFORE UPDATE OR DELETE ON maintenance_events FOR EACH ROW EXECUTE FUNCTION maintenance_history_no_rewrite()",
        "CREATE TRIGGER maintenance_event_no_truncate BEFORE TRUNCATE ON maintenance_events FOR EACH STATEMENT EXECUTE FUNCTION maintenance_history_no_rewrite()",
        "CREATE TRIGGER maintenance_asset_hold_validate BEFORE UPDATE OF maintenance_hold ON equipment_assets FOR EACH ROW EXECUTE FUNCTION maintenance_asset_hold_guard()",
        "REVOKE ALL ON maintenance_requests, maintenance_events FROM PUBLIC",
        "REVOKE ALL ON FUNCTION maintenance_history_no_rewrite(), maintenance_request_guard(), maintenance_asset_hold_guard() FROM PUBLIC",
    ):
        op.execute(statement)


def downgrade() -> None:
    # ACCESS EXCLUSIVE locks must be retained by the caller's migration transaction
    # until all DDL finishes; no data is deleted or reclassified to enable rollback.
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE equipment_assets, maintenance_requests, maintenance_events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM maintenance_requests)
               OR EXISTS (SELECT 1 FROM maintenance_events)
               OR EXISTS (SELECT 1 FROM equipment_assets WHERE maintenance_hold OR availability_status = 'MAINTENANCE') THEN
                RAISE EXCEPTION 'Refusing downgrade: maintenance history or asset holds exist'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $$
    """)
    op.execute("DROP TRIGGER maintenance_asset_hold_validate ON equipment_assets")
    op.drop_constraint('fk_maintenance_current_event', 'maintenance_requests', type_='foreignkey')
    op.drop_table('maintenance_events')
    op.drop_table('maintenance_requests')
    op.execute("DROP FUNCTION maintenance_asset_hold_guard()")
    op.execute("DROP FUNCTION maintenance_request_guard()")
    op.execute("DROP FUNCTION maintenance_history_no_rewrite()")
    op.drop_constraint('ck_asset_available_serviceable', 'equipment_assets', type_='check')
    op.drop_constraint('ck_asset_maintenance_requires_hold', 'equipment_assets', type_='check')
    op.drop_constraint('ck_asset_availability', 'equipment_assets', type_='check')
    op.create_check_constraint('ck_asset_availability', 'equipment_assets', "availability_status IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'RETIRED')")
    op.drop_column('equipment_assets', 'maintenance_hold')
