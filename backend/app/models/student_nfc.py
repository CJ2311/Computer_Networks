"""Student NFC card associations.

Student cards are separate from equipment scanner events.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base


class StudentNFCCard(Base):
    __tablename__ = "student_nfc_cards"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )

    card_uid: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING_WRITE",
        server_default="PENDING_WRITE",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    attempt_nonce: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    scanner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    write_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_student_nfc_digest"),
        CheckConstraint("attempt_started_at >= created_at AND expires_at > attempt_started_at AND expires_at <= attempt_started_at + interval '5 minutes'", name="ck_student_nfc_expiry"),
        UniqueConstraint(
            "student_id",
            name="uq_student_nfc_student",
        ),
        UniqueConstraint(
            "card_uid",
            name="uq_student_nfc_uid",
        ),
        CheckConstraint(
            "card_uid ~ '^([0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})$'",
            name="ck_student_nfc_uid",
        ),
        CheckConstraint(
            "status IN ('PENDING_WRITE', 'ACTIVE', 'FAILED')",
            name="ck_student_nfc_status",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND verified_at IS NOT NULL) "
            "OR (status <> 'ACTIVE' AND verified_at IS NULL)",
            name="ck_student_nfc_verification",
        ),
    )
