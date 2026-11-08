import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    FetchedValue,
    Index,
    String,
    Text,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base

class EquipmentCheckout(Base):
    __tablename__ = "equipment_checkouts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("equipment_assets.id"),
        nullable=False,
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )

    issued_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )

    checked_out_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    returned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    returned_to: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="BORROWED",
        server_default="BORROWED",
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    equipment_name_at_checkout: Mapped[str | None] = mapped_column(
        String(150), nullable=True, server_default=FetchedValue(),
    )
    asset_code_at_checkout: Mapped[str | None] = mapped_column(
        String(50), nullable=True, server_default=FetchedValue(),
    )
    identifier_snapshot_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "(identifier_snapshot_status = 'CAPTURED' AND equipment_name_at_checkout IS NOT NULL "
            "AND asset_code_at_checkout IS NOT NULL) OR "
            "(identifier_snapshot_status = 'LEGACY_UNKNOWN' AND equipment_name_at_checkout IS NULL "
            "AND asset_code_at_checkout IS NULL)",
            name="ck_checkout_identifier_snapshots",
        ),
        CheckConstraint(
            "status IN ('BORROWED', 'RETURNED')",
            name="ck_checkout_status",
        ),
        CheckConstraint(
            "(status = 'BORROWED' AND returned_at IS NULL "
            "AND returned_to IS NULL) OR "
            "(status = 'RETURNED' AND returned_at IS NOT NULL "
            "AND returned_to IS NOT NULL)",
            name="ck_checkout_return_consistency",
        ),
        CheckConstraint(
            "due_at > checked_out_at",
            name="ck_checkout_due_after_issue",
        ),
        CheckConstraint(
            "returned_at IS NULL OR returned_at >= checked_out_at",
            name="ck_checkout_return_after_issue",
        ),
        Index(
            "uq_active_checkout_per_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'BORROWED'"),
        ),
    )
