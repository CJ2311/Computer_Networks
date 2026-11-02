from datetime import datetime, timezone
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User, UserRole
from backend.app.schemas.checkout import CheckoutResponse, ReturnCreate, ReturnResponse

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


def record_return(
    db: Session,
    checkout_id: UUID,
    received_by: User,
    return_data: ReturnCreate,
) -> ReturnResponse:
    """Record a return and the asset state atomically, preserving loan history."""
    try:
        if not received_by.is_active or received_by.role not in {
            UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value,
        }:
            raise HTTPException(status_code=403, detail="User is not authorized to record returns")

        # Discover the asset without locking the checkout first. Checkout creation
        # locks the asset, so all return paths take the asset lock before the loan.
        asset_id = db.scalar(
            select(EquipmentCheckout.asset_id).where(EquipmentCheckout.id == checkout_id)
        )
        if asset_id is None:
            raise HTTPException(status_code=404, detail="Checkout not found")

        asset = db.scalar(
            select(EquipmentAsset).where(EquipmentAsset.id == asset_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        checkout = db.scalar(
            select(EquipmentCheckout).where(EquipmentCheckout.id == checkout_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if checkout is None:
            raise HTTPException(status_code=404, detail="Checkout not found")
        if asset is None or checkout.asset_id != asset_id:
            raise HTTPException(status_code=409, detail="Checkout asset is inconsistent")
        if (
            checkout.status != "BORROWED"
            or checkout.returned_at is not None
            or checkout.returned_to is not None
        ):
            raise HTTPException(status_code=409, detail="Checkout is not active or already returned")
        if asset.availability_status != "BORROWED":
            raise HTTPException(status_code=409, detail="Asset is not in the expected borrowed state")

        # clock_timestamp(), unlike now(), reflects the time AFTER any lock wait.
        returned_at = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
        if returned_at < checkout.checked_out_at:
            raise HTTPException(status_code=409, detail="Return timestamp precedes checkout")

        checkout.status = "RETURNED"
        checkout.returned_at = returned_at
        checkout.returned_to = received_by.id
        # Temporary history representation: replace with structured condition
        # history for future maintenance/auditing. Never overwrite original notes.
        entry = "[RETURN] " + json.dumps({
            "returned_at": returned_at.isoformat(),
            "returned_to": str(received_by.id),
            "condition_status": return_data.condition_status,
            "notes": return_data.notes,
        }, ensure_ascii=False)
        checkout.notes = entry if checkout.notes is None else checkout.notes + "\n\n" + entry
        asset.condition_status = return_data.condition_status
        # RESERVED is only a temporary maintenance hold. A dedicated maintenance
        # availability state must replace this in a future schema change.
        asset.availability_status = (
            "AVAILABLE" if return_data.condition_status == "WORKING" else "RESERVED"
        )

        db.flush()
        # Capture the committed operation's response while holding the locks; a
        # later checkout must not change the availability reported for this return.
        response = ReturnResponse(
            **CheckoutResponse.model_validate(checkout).model_dump(),
            condition_status=asset.condition_status,
            availability_status=asset.availability_status,
            is_overdue=returned_at > checkout.due_at,
        )
        db.commit()
        return response
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Return conflicts with an existing record")
    except Exception:
        db.rollback()
        raise

