from datetime import datetime, timezone
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.user import User, UserRole
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.services import maintenance
from backend.app.services.inventory_transactions import transaction, flush_stage
from backend.app.schemas.checkout import CheckoutResponse, ReturnCreate, ReturnResponse

def validate_borrowing_student(
    db: Session,
    student_id: UUID,
) -> User:

    student = db.get(User, student_id, populate_existing=True)

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
        .with_for_update().execution_options(populate_existing=True)
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

    if asset.maintenance_hold:
        raise HTTPException(status_code=409, detail="Asset has a maintenance hold")

    return asset

def validate_checkout_deadline(
    due_at: datetime,
    now: datetime | None = None,
) -> datetime:

    if not isinstance(due_at, datetime) or due_at.tzinfo is None or due_at.utcoffset() is None:
        raise HTTPException(
            status_code=422,
            detail="Return deadline must include a timezone",
        )

    now = now if now is not None else datetime.now(timezone.utc)

    if due_at <= now:
        raise HTTPException(
            status_code=400,
            detail="Return deadline must be in the future",
        )

    return due_at

@transaction
def create_checkout(
    db: Session,
    asset_id: UUID,
    student_id: UUID,
    issued_by: User,
    due_at: datetime,
    notes: str | None = None,
) -> CheckoutResponse:
    asset_id = maintenance._id(asset_id, "asset ID")
    student_id = maintenance._id(student_id, "student ID")
    maintenance._authorize(db, issued_by)
    if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
        raise HTTPException(status_code=422, detail="Checkout notes must be at most 1000 characters")
    asset = validate_checkout_asset(db, asset_id)
    active = maintenance._locked(db, EquipmentCheckout, EquipmentCheckout.asset_id == asset_id,
                                 EquipmentCheckout.status == "BORROWED")
    if active is not None:
        raise HTTPException(status_code=409, detail="Asset already has an active checkout")
    if maintenance._locked(db, MaintenanceRequest, MaintenanceRequest.asset_id == asset_id,
                           MaintenanceRequest.status.in_(maintenance.ACTIVE)) is not None:
        raise HTTPException(status_code=409, detail="Asset has active maintenance")
    validate_borrowing_student(db, student_id)
    issuer = maintenance._authorize(db, issued_by)
    now = maintenance._now(db)  # Actual database time after every lock wait.
    due_at = validate_checkout_deadline(due_at, now)
    checkout = EquipmentCheckout(
        asset_id=asset_id, student_id=student_id, issued_by=issuer.id,
        checked_out_at=now, due_at=due_at, status="BORROWED", notes=notes,
    )
    asset.availability_status = "BORROWED"
    flush_stage(db, asset)
    db.add(checkout)
    flush_stage(db, checkout)
    response = CheckoutResponse.model_validate(checkout)
    # Detect a deadline expiring during persistence/response validation too.
    validate_checkout_deadline(due_at, maintenance._now(db))
    return response


@transaction
def record_return(
    db: Session,
    checkout_id: UUID,
    received_by: User,
    return_data: ReturnCreate,
) -> ReturnResponse:
    """Close the loan and record maintenance observations in one owned transaction."""
    roles = {UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value}
    maintenance._authorize(db, received_by, roles)
    checkout_id = maintenance._id(checkout_id, "checkout ID")
    return_data = ReturnCreate.model_validate(return_data)
    asset_id = db.scalar(select(EquipmentCheckout.asset_id).where(EquipmentCheckout.id == checkout_id))
    if asset_id is None:
        raise HTTPException(status_code=404, detail="Checkout not found")
    asset = maintenance._locked(db, EquipmentAsset, EquipmentAsset.id == asset_id)
    checkout = maintenance._locked(db, EquipmentCheckout, EquipmentCheckout.id == checkout_id)
    if checkout is None:
        raise HTTPException(status_code=404, detail="Checkout not found")
    if asset is None or checkout.asset_id != asset_id:
        raise HTTPException(status_code=409, detail="Checkout asset is inconsistent")
    if checkout.status != "BORROWED" or checkout.returned_at is not None or checkout.returned_to is not None:
        raise HTTPException(status_code=409, detail="Checkout is not active or already returned")
    # RESERVED with an active loan has no trustworthy reservation provenance.
    # Reject for reconciliation; never reinterpret or release it.
    if asset.availability_status != "BORROWED":
        raise HTTPException(status_code=409, detail="Asset is not in the expected borrowed state; reconciliation required")
    plan, clock_record = maintenance._plan_return_maintenance(
        db, asset, checkout, received_by, return_data.condition_status, return_data.notes,
        return_data.recovery_of_request_id,
    )
    receiver = maintenance._authorize(db, received_by, roles)
    returned_at = maintenance._now(db, clock_record)
    if returned_at < checkout.checked_out_at:
        raise HTTPException(status_code=409, detail="Return timestamp precedes checkout")
    checkout.status, checkout.returned_at, checkout.returned_to = "RETURNED", returned_at, receiver.id
    entry = "[RETURN] " + json.dumps({
        "returned_at": returned_at.isoformat(), "returned_to": str(receiver.id),
        "condition_status": return_data.condition_status, "notes": return_data.notes,
    }, ensure_ascii=False)
    checkout.notes = entry if checkout.notes is None else checkout.notes + "\n\n" + entry
    flush_stage(db, checkout)
    if plan is not None:
        maintenance._persist_return_maintenance(db, plan, asset, receiver.id, returned_at)
    else:
        asset.condition_status, asset.availability_status = "WORKING", "AVAILABLE"
        flush_stage(db, asset)
    # Snapshot and validate under locks, before the transaction owner commits.
    return ReturnResponse(
        **CheckoutResponse.model_validate(checkout).model_dump(),
        condition_status=asset.condition_status, availability_status=asset.availability_status,
        maintenance_hold=asset.maintenance_hold,
        maintenance_request_id=plan.request.id if plan else None,
        is_overdue=returned_at > checkout.due_at,
    )
