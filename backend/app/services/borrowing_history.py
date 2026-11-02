from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User, UserRole
from backend.app.schemas.borrowing_history import HistoryDetail, HistoryFilters, HistoryItem, HistoryPage


def _scope(user: User, student_id: UUID | None = None):
    allowed = {
        UserRole.STUDENT.value, UserRole.FACULTY.value,
        UserRole.LAB_ASSISTANT.value, UserRole.ADMIN.value,
    }
    if not user.is_active or user.role not in allowed:
        raise HTTPException(status_code=403, detail="User is not authorized to view borrowing history")
    if user.role == UserRole.STUDENT.value:
        if student_id is not None and student_id != user.id:
            raise HTTPException(status_code=403, detail="Cannot filter another borrower's history")
        return [EquipmentCheckout.student_id == user.id]
    # Temporary laboratory-wide staff access: no assigned-lab scope exists yet.
    return [EquipmentCheckout.student_id == student_id] if student_id is not None else []


def _currently_overdue(as_of: datetime):
    return and_(EquipmentCheckout.status == "BORROWED", EquipmentCheckout.due_at < as_of)


def _history_statement(as_of: datetime):
    return select(
        EquipmentCheckout.id, EquipmentCheckout.asset_id,
        EquipmentAsset.equipment_id, EquipmentAsset.asset_code,
        Equipment.name.label("equipment_name"), EquipmentCheckout.student_id,
        EquipmentCheckout.issued_by, EquipmentCheckout.checked_out_at,
        EquipmentCheckout.due_at, EquipmentCheckout.returned_at,
        EquipmentCheckout.returned_to, EquipmentCheckout.status,
        _currently_overdue(as_of).label("is_currently_overdue"),
        and_(EquipmentCheckout.status == "RETURNED",
             EquipmentCheckout.returned_at > EquipmentCheckout.due_at).label("was_returned_late"),
    ).join(EquipmentAsset, EquipmentAsset.id == EquipmentCheckout.asset_id).join(
        Equipment, Equipment.id == EquipmentAsset.equipment_id
    )


def list_borrowing_history(db: Session, user: User, filters: HistoryFilters) -> HistoryPage:
    predicates = _scope(user, filters.student_id)
    # Even a caller with autoflush enabled must not write while reading history.
    with db.no_autoflush:
        as_of = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
        for field, column in (
            (filters.equipment_id, EquipmentAsset.equipment_id),
            (filters.asset_id, EquipmentCheckout.asset_id),
            (filters.status, EquipmentCheckout.status),
        ):
            if field is not None:
                predicates.append(column == field)
        if filters.overdue is not None:
            predicates.append(_currently_overdue(as_of).is_(filters.overdue))
        if filters.checked_out_from is not None:
            predicates.append(EquipmentCheckout.checked_out_at >= filters.checked_out_from)
        if filters.checked_out_before is not None:
            predicates.append(EquipmentCheckout.checked_out_at < filters.checked_out_before)
        statement = _history_statement(as_of).where(*predicates).order_by(
            EquipmentCheckout.checked_out_at.desc(), EquipmentCheckout.id.desc()
        ).limit(filters.limit + 1).offset(filters.offset)
        rows = db.execute(statement).mappings().all()
        return HistoryPage(
            items=[HistoryItem.model_validate(row) for row in rows[:filters.limit]],
            limit=filters.limit, offset=filters.offset,
            has_more=len(rows) > filters.limit, as_of=as_of,
        )


def get_borrowing_history(db: Session, user: User, checkout_id: UUID) -> HistoryDetail:
    predicates = _scope(user)
    with db.no_autoflush:
        as_of = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
        row = db.execute(_history_statement(as_of).where(
            EquipmentCheckout.id == checkout_id, *predicates
        )).mappings().one_or_none()
        if row is None:
            # Foreign student records and nonexistent IDs are indistinguishable.
            raise HTTPException(status_code=404, detail="Checkout not found")
        return HistoryDetail(**row, as_of=as_of)
