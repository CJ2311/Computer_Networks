import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User, UserRole

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base

def validate_borrowing_student(
    db: Session,
    student_id: UUID,
) -> User:

    student = db.get(User, student_id)

    if student is None:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    if student.role != UserRole.STUDENT.value:
        raise HTTPException(
            status_code=400,
            detail="Borrower must have the STUDENT role",
        )

    if not student.is_active:
        raise HTTPException(
            status_code=403,
            detail="Student account is inactive",
        )

    return student

def validate_checkout_asset(
    db: Session,
    asset_id: UUID,
) -> EquipmentAsset:

    asset = db.scalar(
        select(EquipmentAsset)
        .where(EquipmentAsset.id == asset_id)
        .with_for_update()
    )

    if asset is None:
        raise HTTPException(
            status_code=404,
            detail="Asset not found",
        )

    if asset.availability_status != "AVAILABLE":
        raise HTTPException(
            status_code=409,
            detail="Asset is not available for borrowing",
        )

    if asset.condition_status != "WORKING":
        raise HTTPException(
            status_code=409,
            detail="Asset is not in working condition",
        )

    return asset

def validate_checkout_deadline(
    due_at: datetime,
) -> datetime:

    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise HTTPException(
            status_code=422,
            detail="Return deadline must include a timezone",
        )

    now = datetime.now(timezone.utc)

    if due_at <= now:
        raise HTTPException(
            status_code=400,
            detail="Return deadline must be in the future",
        )

    return due_at

def create_checkout(
    db: Session,
    asset_id: UUID,
    student_id: UUID,
    issued_by: User,
    due_at: datetime,
    notes: str | None = None,
) -> EquipmentCheckout:

    # Validate the deadline before acquiring database locks.
    due_at = validate_checkout_deadline(due_at)

    try:
        # Lock the asset row to prevent simultaneous checkouts.
        asset = validate_checkout_asset(db, asset_id)

        # Verify the borrowing student.
        validate_borrowing_student(db, student_id)

        # Verify the issuing staff member.
        if (
            not issued_by.is_active
            or issued_by.role not in {
                UserRole.ADMIN.value,
                UserRole.FACULTY.value,
                UserRole.LAB_ASSISTANT.value,
            }
        ):
            raise HTTPException(
                status_code=403,
                detail="User is not authorized to issue equipment",
            )

        # Create the borrowing record.
        checkout = EquipmentCheckout(
            asset_id=asset_id,
            student_id=student_id,
            issued_by=issued_by.id,
            due_at=due_at,
            status="BORROWED",
            notes=notes,
        )

        # Update the physical asset's availability.
        asset.availability_status = "BORROWED"

        db.add(checkout)

        # Save both changes in one transaction.
        db.commit()
        db.refresh(checkout)

        return checkout

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Checkout conflicts with an existing record",
        )

    except Exception:
        db.rollback()
        raise

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

    __table_args__ = (
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