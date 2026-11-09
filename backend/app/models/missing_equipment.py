"""Missing equipment incidents and immutable audit events."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base


class MissingEquipmentIncident(Base):
    __tablename__ = "missing_equipment_incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("equipment_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    reconcile_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        unique=True,
    )
    report_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    reconcile_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="OPEN",
        server_default="OPEN",
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    reported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )
    report_reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    police_report_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    reconciled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reconcile_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    recovered_condition: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    previous_availability_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    previous_condition_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    resolved_availability_status: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    resolved_condition_status: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "uq_active_missing_incident_per_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        CheckConstraint(
            "status IN ('OPEN', 'RECONCILED')",
            name="ck_missing_incident_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_missing_incident_version",
        ),
        CheckConstraint(
            "recovered_condition IS NULL OR recovered_condition IN ('WORKING', 'DAMAGED')",
            name="ck_missing_recovered_condition",
        ),
        CheckConstraint(
            "report_reason ~ '[^[:space:]]' AND char_length(report_reason) <= 4000",
            name="ck_missing_report_reason",
        ),
        CheckConstraint(
            "reconcile_reason IS NULL OR (reconcile_reason ~ '[^[:space:]]' AND char_length(reconcile_reason) <= 4000)",
            name="ck_missing_reconcile_reason",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND reconciled_by IS NULL AND reconciled_at IS NULL "
            "AND reconcile_reason IS NULL AND recovered_condition IS NULL "
            "AND resolved_availability_status IS NULL AND resolved_condition_status IS NULL "
            "AND reconcile_key IS NULL AND reconcile_fingerprint IS NULL) OR "
            "(status = 'RECONCILED' AND reconciled_by IS NOT NULL AND reconciled_at IS NOT NULL "
            "AND reconcile_reason IS NOT NULL AND recovered_condition IS NOT NULL "
            "AND resolved_availability_status IS NOT NULL AND resolved_condition_status IS NOT NULL "
            "AND reconcile_key IS NOT NULL AND reconcile_fingerprint IS NOT NULL "
            "AND reconciled_at >= reported_at)",
            name="ck_missing_incident_resolution",
        ),
    )


class MissingEquipmentEvent(Base):
    __tablename__ = "missing_equipment_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("missing_equipment_incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("equipment_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    police_report_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    from_availability_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    to_availability_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    from_condition_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    to_condition_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    from_maintenance_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    to_maintenance_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("incident_id", "sequence", name="uq_missing_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_missing_event_sequence"),
        CheckConstraint(
            "event_type IN ('REPORTED_MISSING', 'RECONCILED')",
            name="ck_missing_event_type",
        ),
        CheckConstraint(
            "reason ~ '[^[:space:]]' AND char_length(reason) <= 4000",
            name="ck_missing_event_reason",
        ),
    )

