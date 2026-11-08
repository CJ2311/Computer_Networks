import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base


class MaintenanceRequest(Base):
    __tablename__ = "maintenance_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_assets.id", ondelete="RESTRICT"), nullable=False,
    )
    issue_type: Mapped[str] = mapped_column(String(20), nullable=False)
    issue_description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", server_default="OPEN", nullable=False)
    reported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False,
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repair_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repair_completed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    returned_to_service_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    recovery_of_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "asset_id", name="uq_maintenance_request_id_asset"),
        ForeignKeyConstraint(
            ["recovery_of_request_id", "asset_id"], ["maintenance_requests.id", "maintenance_requests.asset_id"],
            name="fk_maintenance_recovery_asset", ondelete="RESTRICT",
        ),
        # Each inserted/current request version must have its immutable event by commit.
        ForeignKeyConstraint(
            ["id", "version"], ["maintenance_events.request_id", "maintenance_events.sequence"],
            name="fk_maintenance_current_event", ondelete="RESTRICT",
            deferrable=True, initially="DEFERRED", use_alter=True,
        ),
        CheckConstraint("status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name="ck_maintenance_request_status"),
        CheckConstraint("issue_type IN ('DAMAGE', 'MALFUNCTION', 'INSPECTION')", name="ck_maintenance_issue_type"),
        CheckConstraint("issue_description ~ '[^[:space:]]' AND char_length(issue_description) <= 4000", name="ck_maintenance_description"),
        CheckConstraint("version >= 1", name="ck_maintenance_version"),
        CheckConstraint("recovery_of_request_id IS NULL OR recovery_of_request_id != id", name="ck_maintenance_no_self_recovery"),
        CheckConstraint("recovery_of_request_id IS NULL OR issue_type = 'INSPECTION'", name="ck_maintenance_recovery_inspection"),
        CheckConstraint("started_at IS NULL OR started_at >= reported_at", name="ck_maintenance_start_time"),
        CheckConstraint("(repair_completed_at IS NULL) = (repair_completed_by IS NULL)", name="ck_maintenance_repair_actor"),
        CheckConstraint("repair_completed_at IS NULL OR (started_at IS NOT NULL AND repair_completed_at >= started_at)", name="ck_maintenance_repair_time"),
        CheckConstraint("status != 'OPEN' OR (started_at IS NULL AND repair_completed_at IS NULL)", name="ck_maintenance_open_fields"),
        CheckConstraint("status NOT IN ('IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED') OR (started_at IS NOT NULL AND assigned_to IS NOT NULL)", name="ck_maintenance_work_fields"),
        CheckConstraint("status NOT IN ('AWAITING_VERIFICATION', 'COMPLETED') OR repair_completed_at IS NOT NULL", name="ck_maintenance_repair_required"),
        CheckConstraint(
            "(status = 'COMPLETED' AND verified_at IS NOT NULL AND verified_by IS NOT NULL AND returned_to_service_at IS NOT NULL) OR "
            "(status != 'COMPLETED' AND verified_at IS NULL AND verified_by IS NULL AND returned_to_service_at IS NULL)",
            name="ck_maintenance_verification_fields",
        ),
        CheckConstraint("verified_at IS NULL OR (repair_completed_at IS NOT NULL AND verified_at >= repair_completed_at AND verified_by != repair_completed_by)", name="ck_maintenance_independent_verification"),
        CheckConstraint("returned_to_service_at IS NULL OR returned_to_service_at >= verified_at", name="ck_maintenance_service_time"),
        CheckConstraint(
            "(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancellation_reason IS NOT NULL "
            "AND cancellation_reason ~ '[^[:space:]]' AND char_length(cancellation_reason) <= 4000) OR "
            "(status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_reason IS NULL)",
            name="ck_maintenance_cancellation_fields",
        ),
        CheckConstraint("cancelled_at IS NULL OR (cancelled_at >= reported_at AND (started_at IS NULL OR cancelled_at >= started_at) AND (repair_completed_at IS NULL OR cancelled_at >= repair_completed_at))", name="ck_maintenance_cancellation_time"),
        Index("uq_active_maintenance_per_asset", "asset_id", unique=True, postgresql_where=text("status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION')")),
        Index("ix_maintenance_asset_reported", "asset_id", "reported_at", "id"),
        Index("ix_maintenance_status_reported", "status", "reported_at", "id"),
        Index("ix_maintenance_assignee_status", "assigned_to", "status"),
        Index("ix_maintenance_reporter_reported", "reported_by", "reported_at", "id"),
        Index("ix_maintenance_recovery", "recovery_of_request_id"),
    )


class MaintenanceEvent(Base):
    __tablename__ = "maintenance_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("maintenance_requests.id", ondelete="RESTRICT"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str | None] = mapped_column(String(30))
    from_assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    to_assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    from_condition: Mapped[str | None] = mapped_column(String(30))
    to_condition: Mapped[str | None] = mapped_column(String(30))
    from_availability: Mapped[str | None] = mapped_column(String(30))
    to_availability: Mapped[str | None] = mapped_column(String(30))
    from_maintenance_hold: Mapped[bool | None] = mapped_column(Boolean)
    to_maintenance_hold: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        UniqueConstraint("request_id", "sequence", name="uq_maintenance_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_maintenance_event_sequence"),
        CheckConstraint("event_type IN ('REPORTED', 'ASSIGNED', 'STARTED', 'NOTE_ADDED', 'REPAIR_COMPLETED', 'VERIFICATION_FAILED', 'VERIFIED', 'CANCELLED', 'RETURN_OBSERVED')", name="ck_maintenance_event_type"),
        CheckConstraint("note IS NULL OR (note ~ '[^[:space:]]' AND char_length(note) <= 5000)", name="ck_maintenance_event_note"),
        CheckConstraint("event_type NOT IN ('NOTE_ADDED', 'REPAIR_COMPLETED', 'VERIFICATION_FAILED', 'CANCELLED') OR note IS NOT NULL", name="ck_maintenance_event_note_required"),
        CheckConstraint("from_status IS NULL OR from_status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name="ck_maintenance_event_from_status"),
        CheckConstraint("to_status IS NULL OR to_status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'COMPLETED', 'CANCELLED')", name="ck_maintenance_event_to_status"),
        CheckConstraint("from_condition IS NULL OR from_condition IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')", name="ck_maintenance_event_from_condition"),
        CheckConstraint("to_condition IS NULL OR to_condition IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')", name="ck_maintenance_event_to_condition"),
        CheckConstraint("from_availability IS NULL OR from_availability IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')", name="ck_maintenance_event_from_availability"),
        CheckConstraint("to_availability IS NULL OR to_availability IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')", name="ck_maintenance_event_to_availability"),
        Index("ix_maintenance_event_occurred", "request_id", "occurred_at", "id"),
    )
