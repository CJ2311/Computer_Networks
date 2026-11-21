"""Ephemeral scanner staging is separate from inventory custody and audit history."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.base import Base


class EquipmentScanner(Base):
    __tablename__ = "equipment_scanners"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    secret_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    capture_session_id: Mapped[UUID | None] = mapped_column(unique=True)
    capture_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    capture_secret_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    capture_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("secret_sha256 ~ '^[0-9a-f]{64}$'", name="ck_scanner_digest"),
        CheckConstraint("last_sequence >= 0 AND request_count BETWEEN 0 AND 61", name="ck_scanner_counters"),
        CheckConstraint("""(capture_session_id IS NULL AND capture_user_id IS NULL AND capture_secret_sha256 IS NULL
            AND capture_started_at IS NULL AND capture_expires_at IS NULL) OR
            (capture_session_id IS NOT NULL AND capture_user_id IS NOT NULL AND capture_secret_sha256 IS NOT NULL
             AND capture_secret_sha256 ~ '^[0-9a-f]{64}$' AND capture_started_at IS NOT NULL
             AND capture_secret_sha256 <> secret_sha256
             AND capture_expires_at IS NOT NULL AND capture_expires_at > capture_started_at
             AND capture_expires_at <= capture_started_at + interval '15 minutes')""", name="ck_scanner_capture"),
    )


class ScannerScanEvent(Base):
    __tablename__ = "scanner_scan_events"
    scanner_id: Mapped[UUID] = mapped_column(ForeignKey("equipment_scanners.id"), primary_key=True)
    scan_id: Mapped[UUID] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[UUID] = mapped_column(nullable=False)
    staff_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("equipment_assets.id"), nullable=False)
    uid: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("scanner_id", "sequence", name="uq_scanner_event_sequence"),
        CheckConstraint("sequence > 0", name="ck_scanner_event_sequence"),
        CheckConstraint("uid ~ '^([0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})$'", name="ck_scanner_event_uid"),
        CheckConstraint("expires_at = received_at + interval '120 seconds'", name="ck_scanner_event_expiry"),
        Index("ix_scanner_events_expiry", "expires_at"),
    )
