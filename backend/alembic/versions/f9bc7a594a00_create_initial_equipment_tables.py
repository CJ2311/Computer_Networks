"""create initial equipment tables

Revision ID: f9bc7a594a00
Revises:
Create Date: 2026-10-31 09:29:31.325933
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Revision identifiers used by Alembic.
revision: str = "f9bc7a594a00"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the initial equipment inventory tables."""

    # Equipment definitions
    op.create_table(
        "equipment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("tracking_type", sa.String(length=20), nullable=False),
        sa.Column("manufacturer", sa.String(length=100), nullable=True),
        sa.Column("model_number", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_location", sa.String(length=150), nullable=False),
        sa.Column(
            "purchase_price",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tracking_type IN ('INDIVIDUAL', 'QUANTITY')",
            name="ck_equipment_tracking_type",
        ),
        sa.CheckConstraint(
            "purchase_price IS NULL OR purchase_price >= 0",
            name="ck_equipment_purchase_price",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Individually tracked physical equipment
    op.create_table(
        "equipment_assets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("equipment_id", sa.UUID(), nullable=False),
        sa.Column("asset_code", sa.String(length=50), nullable=False),
        sa.Column("nfc_tag_id", sa.String(length=100), nullable=True),
        sa.Column("serial_number", sa.String(length=100), nullable=True),
        sa.Column("location", sa.String(length=150), nullable=False),
        sa.Column(
            "availability_status",
            sa.String(length=30),
            server_default="AVAILABLE",
            nullable=False,
        ),
        sa.Column(
            "condition_status",
            sa.String(length=30),
            server_default="WORKING",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "availability_status IN "
            "('AVAILABLE', 'BORROWED', 'RESERVED', 'RETIRED')",
            name="ck_asset_availability",
        ),
        sa.CheckConstraint(
            "condition_status IN "
            "('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')",
            name="ck_asset_condition",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"],
            ["equipment.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_code"),
        sa.UniqueConstraint(
            "equipment_id",
            "serial_number",
            name="uq_equipment_asset_serial",
        ),
        sa.UniqueConstraint("nfc_tag_id"),
    )

    # Quantity-managed equipment stock
    op.create_table(
        "equipment_stock",
        sa.Column("equipment_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 0",
            name="ck_equipment_stock_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"],
            ["equipment.id"],
        ),
        sa.PrimaryKeyConstraint("equipment_id"),
    )


def downgrade() -> None:
    """Remove the initial equipment inventory tables."""

    op.drop_table("equipment_stock")
    op.drop_table("equipment_assets")
    op.drop_table("equipment")
