from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User, UserRole

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

