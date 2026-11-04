"""Shared registration authorization and commit-outcome policy."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.models.user import User, UserRole


UNCERTAIN_COMMIT_DETAIL = (
    "Registration outcome could not be confirmed. Check existing records before retrying."
)


def authorize_registration(db: Session, actor: User) -> User:
    # Authentication may have read a different Session before this transaction.
    # Trust only the ID reference; re-read activation and role here.
    if actor is None or not isinstance(actor.id, UUID):
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    staff = db.get(User, actor.id, populate_existing=True)
    if staff is None or not staff.is_active or staff.role not in {
        UserRole.ADMIN.value, UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value,
    }:
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    return staff
