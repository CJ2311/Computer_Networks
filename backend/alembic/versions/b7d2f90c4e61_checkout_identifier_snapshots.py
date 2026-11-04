"""Capture immutable checkout-time identifiers without inventing legacy history.

Revision ID: b7d2f90c4e61
Revises: a3c7e91b6d20
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d2f90c4e61"
down_revision = "a3c7e91b6d20"
branch_labels = None
depends_on = None

SNAPSHOT_CHECK = (
    "(identifier_snapshot_status = 'CAPTURED' AND equipment_name_at_checkout IS NOT NULL "
    "AND asset_code_at_checkout IS NOT NULL) OR "
    "(identifier_snapshot_status = 'LEGACY_UNKNOWN' AND equipment_name_at_checkout IS NULL "
    "AND asset_code_at_checkout IS NULL)"
)


def _schema():
    connection = op.get_bind()
    return connection.dialect.identifier_preparer.quote_schema(
        connection.scalar(sa.text("SELECT current_schema()"))
    )


def upgrade():
    schema = _schema()
    op.add_column("equipment_checkouts", sa.Column("equipment_name_at_checkout", sa.String(150), nullable=True))
    op.add_column("equipment_checkouts", sa.Column("asset_code_at_checkout", sa.String(50), nullable=True))
    # Temporary default classifies only preexisting rows. Remove before future inserts.
    op.add_column("equipment_checkouts", sa.Column(
        "identifier_snapshot_status", sa.String(20), nullable=False,
        server_default="LEGACY_UNKNOWN"))
    op.alter_column("equipment_checkouts", "identifier_snapshot_status", server_default=None)
    op.create_check_constraint("ck_checkout_identifier_snapshots", "equipment_checkouts", SNAPSHOT_CHECK)
    op.execute(sa.text(f"""
CREATE FUNCTION {schema}.checkout_identifier_snapshots() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
DECLARE parent_id uuid; captured_code varchar(50); captured_name varchar(150);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.equipment_name_at_checkout IS DISTINCT FROM OLD.equipment_name_at_checkout
           OR NEW.asset_code_at_checkout IS DISTINCT FROM OLD.asset_code_at_checkout
           OR NEW.identifier_snapshot_status IS DISTINCT FROM OLD.identifier_snapshot_status THEN
            RAISE EXCEPTION 'Checkout identifiers are immutable' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.equipment_name_at_checkout IS NOT NULL OR NEW.asset_code_at_checkout IS NOT NULL
       OR NEW.identifier_snapshot_status IS NOT NULL THEN
        RAISE EXCEPTION 'Checkout identifiers must be captured by the database' USING ERRCODE = '42501';
    END IF;
    SELECT a.equipment_id, a.asset_code INTO parent_id, captured_code
      FROM {schema}.equipment_assets AS a WHERE a.id = NEW.asset_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Checkout asset does not exist' USING ERRCODE = '23503';
    END IF;
    SELECT e.name INTO captured_name FROM {schema}.equipment AS e
      WHERE e.id = parent_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Checkout equipment does not exist' USING ERRCODE = '23503';
    END IF;
    NEW.equipment_name_at_checkout := captured_name;
    NEW.asset_code_at_checkout := captured_code;
    NEW.identifier_snapshot_status := 'CAPTURED';
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}.checkout_identifier_snapshots() FROM PUBLIC;
CREATE TRIGGER checkout_identifier_snapshots
BEFORE INSERT OR UPDATE ON {schema}.equipment_checkouts
FOR EACH ROW EXECUTE FUNCTION {schema}.checkout_identifier_snapshots();
"""))


def downgrade():
    schema = _schema()
    op.execute(sa.text(f"LOCK TABLE {schema}.equipment_checkouts IN ACCESS EXCLUSIVE MODE"))
    # Never silently discard captured OR explicitly unknown borrowing provenance.
    op.execute(sa.text(f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM {schema}.equipment_checkouts) THEN
        RAISE EXCEPTION 'Cannot discard populated checkout identifier history'
            USING ERRCODE = '55000';
    END IF;
END $$;
"""))
    op.execute(sa.text(f"DROP TRIGGER checkout_identifier_snapshots ON {schema}.equipment_checkouts"))
    op.execute(sa.text(f"DROP FUNCTION {schema}.checkout_identifier_snapshots()"))
    op.drop_constraint("ck_checkout_identifier_snapshots", "equipment_checkouts", type_="check")
    for column in ("identifier_snapshot_status", "asset_code_at_checkout", "equipment_name_at_checkout"):
        op.drop_column("equipment_checkouts", column)
