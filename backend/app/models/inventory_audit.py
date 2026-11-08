"""Immutable inventory audit snapshots, observations, and reconciliation findings."""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base


class InventoryAudit(Base):
    __tablename__ = "inventory_audits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="IN_PROGRESS", server_default="IN_PROGRESS")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    snapshot_sealed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    final_notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED', 'CANCELLED')", name="ck_inventory_audit_status"),
        CheckConstraint("version >= 1", name="ck_inventory_audit_version"),
        CheckConstraint("name ~ '[^[:space:]]' AND location ~ '[^[:space:]]'", name="ck_inventory_audit_text"),
        CheckConstraint(
            "(status = 'COMPLETED' AND snapshot_sealed AND completed_at IS NOT NULL AND completed_by IS NOT NULL "
            "AND reconciled_at IS NOT NULL AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_reason IS NULL) OR "
            "(status = 'CANCELLED' AND snapshot_sealed AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancellation_reason IS NOT NULL AND cancellation_reason ~ '[^[:space:]]' "
            "AND completed_at IS NULL AND completed_by IS NULL AND reconciled_at IS NULL AND final_notes IS NULL) OR "
            "(status = 'IN_PROGRESS' AND completed_at IS NULL AND completed_by IS NULL AND cancelled_at IS NULL "
            "AND cancelled_by IS NULL AND cancellation_reason IS NULL AND reconciled_at IS NULL AND final_notes IS NULL)",
            name="ck_inventory_audit_terminal_fields",
        ),
        CheckConstraint(
            "(completed_at IS NULL OR completed_at >= reconciled_at AND reconciled_at >= started_at) "
            "AND (cancelled_at IS NULL OR cancelled_at >= started_at)",
            name="ck_inventory_audit_times",
        ),
        CheckConstraint(
            "(notes IS NULL OR char_length(notes) <= 4000) AND "
            "(final_notes IS NULL OR char_length(final_notes) <= 4000) AND "
            "(cancellation_reason IS NULL OR char_length(cancellation_reason) <= 4000)",
            name="ck_inventory_audit_notes",
        ),
        Index("ix_inventory_audit_status_started", "status", "started_at", "id"),
    )


class InventoryAuditExpectedAsset(Base):
    __tablename__ = "inventory_audit_expected_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_audits.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("equipment_assets.id", ondelete="RESTRICT"))
    in_scope: Mapped[bool] = mapped_column(Boolean)
    equipment_name_at_start: Mapped[str] = mapped_column(String(150))
    expected_location: Mapped[str] = mapped_column(String(150))
    availability_at_start: Mapped[str] = mapped_column(String(30))
    condition_at_start: Mapped[str] = mapped_column(String(30))
    maintenance_hold_at_start: Mapped[bool] = mapped_column(Boolean)
    asset_code_at_start: Mapped[str] = mapped_column(String(50))
    nfc_tag_id_at_start: Mapped[str | None] = mapped_column(String(100))
    checkout_id_at_start: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_checkouts.id", ondelete="RESTRICT"),
    )
    maintenance_id_at_start: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maintenance_requests.id", ondelete="RESTRICT"),
    )
    maintenance_version_at_start: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("audit_id", "asset_id", name="uq_inventory_audit_expected_asset"),
        UniqueConstraint("id", "audit_id", "asset_id", name="uq_inventory_expected_identity"),
        UniqueConstraint("audit_id", "nfc_tag_id_at_start", name="uq_inventory_expected_tag"),
        CheckConstraint(
            "availability_at_start IN ('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')",
            name="ck_inventory_audit_expected_availability",
        ),
        CheckConstraint(
            "condition_at_start IN ('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')",
            name="ck_inventory_audit_expected_condition",
        ),
        CheckConstraint(
            "(maintenance_id_at_start IS NULL AND maintenance_version_at_start IS NULL) OR "
            "(maintenance_id_at_start IS NOT NULL AND maintenance_version_at_start IS NOT NULL AND maintenance_version_at_start >= 1)",
            name="ck_inventory_expected_maintenance",
        ),
    )


class InventoryAuditScan(Base):
    __tablename__ = "inventory_audit_scans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_audits.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("equipment_assets.id", ondelete="RESTRICT"))
    expected_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    duplicate_of_scan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    nfc_tag_id: Mapped[str] = mapped_column(String(100))
    scanned_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    observed_location: Mapped[str] = mapped_column(String(150))
    result: Mapped[str] = mapped_column(String(30))

    __table_args__ = (
        UniqueConstraint("id", "audit_id", name="uq_inventory_audit_scan_id_audit"),
        UniqueConstraint("id", "audit_id", "asset_id", name="uq_inventory_scan_identity"),
        UniqueConstraint("audit_id", "idempotency_key", name="uq_inventory_scan_idempotency"),
        ForeignKeyConstraint(
            ["expected_asset_id", "audit_id", "asset_id"],
            ["inventory_audit_expected_assets.id", "inventory_audit_expected_assets.audit_id", "inventory_audit_expected_assets.asset_id"],
            name="fk_inventory_scan_expected", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["duplicate_of_scan_id", "audit_id"], ["inventory_audit_scans.id", "inventory_audit_scans.audit_id"],
            name="fk_inventory_scan_duplicate_audit", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "result IN ('MATCHED', 'MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG', 'DUPLICATE')",
            name="ck_inventory_audit_scan_result",
        ),
        CheckConstraint(
            "(result = 'UNKNOWN_TAG' AND asset_id IS NULL AND expected_asset_id IS NULL) OR "
            "(result = 'DUPLICATE' AND ((asset_id IS NULL AND expected_asset_id IS NULL) OR "
            "(asset_id IS NOT NULL AND expected_asset_id IS NOT NULL))) OR "
            "(result IN ('MATCHED', 'MISPLACED', 'UNEXPECTED') AND asset_id IS NOT NULL AND expected_asset_id IS NOT NULL)",
            name="ck_inventory_audit_scan_asset_resolution",
        ),
        CheckConstraint("(result = 'DUPLICATE') = (duplicate_of_scan_id IS NOT NULL)", name="ck_inventory_scan_duplicate"),
        CheckConstraint("duplicate_of_scan_id IS NULL OR duplicate_of_scan_id <> id", name="ck_inventory_scan_no_self_duplicate"),
        CheckConstraint("nfc_tag_id ~ '[^[:space:]]' AND observed_location ~ '[^[:space:]]'", name="ck_inventory_scan_text"),
        Index("uq_inventory_scan_first_tag", "audit_id", "nfc_tag_id", unique=True,
              postgresql_where=text("duplicate_of_scan_id IS NULL")),
        Index("ix_inventory_scan_audit_time", "audit_id", "scanned_at", "id"),
    )


class InventoryAuditDiscrepancy(Base):
    __tablename__ = "inventory_audit_discrepancies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_audits.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("equipment_assets.id", ondelete="RESTRICT"))
    expected_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    discrepancy_type: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())

    __table_args__ = (
        ForeignKeyConstraint(
            ["scan_id", "audit_id"], ["inventory_audit_scans.id", "inventory_audit_scans.audit_id"],
            name="fk_inventory_discrepancy_scan_audit", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scan_id", "audit_id", "asset_id"],
            ["inventory_audit_scans.id", "inventory_audit_scans.audit_id", "inventory_audit_scans.asset_id"],
            name="fk_inventory_discrepancy_scan_asset", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["expected_asset_id", "audit_id", "asset_id"],
            ["inventory_audit_expected_assets.id", "inventory_audit_expected_assets.audit_id", "inventory_audit_expected_assets.asset_id"],
            name="fk_inventory_discrepancy_expected", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "discrepancy_type IN ('NOT_VERIFIED', 'EXPECTED_ON_LOAN', 'EXPECTED_MAINTENANCE', "
            "'MISPLACED', 'UNEXPECTED', 'UNKNOWN_TAG', 'STATE_CHANGED', 'CUSTODY_REVIEW')",
            name="ck_inventory_audit_discrepancy_type",
        ),
        CheckConstraint(
            "(asset_id IS NULL AND expected_asset_id IS NULL AND scan_id IS NOT NULL AND discrepancy_type = 'UNKNOWN_TAG') OR "
            "(asset_id IS NOT NULL AND expected_asset_id IS NOT NULL)",
            name="ck_inventory_audit_discrepancy_reference",
        ),
        CheckConstraint("description ~ '[^[:space:]]' AND char_length(description) <= 4000",
                        name="ck_inventory_audit_discrepancy_description"),
        UniqueConstraint("audit_id", "expected_asset_id", "discrepancy_type", name="uq_inventory_finding_expected"),
        UniqueConstraint("audit_id", "scan_id", "discrepancy_type", name="uq_inventory_finding_scan"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_inventory_discrepancy_details"),
        Index("ix_inventory_discrepancy_audit", "audit_id", "discrepancy_type", "id"),
    )
