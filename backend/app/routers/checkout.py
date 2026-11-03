from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from uuid import UUID
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import User, UserRole
from backend.app.schemas.checkout import CheckoutCreate, CheckoutResponse, ReturnCreate, ReturnResponse
from backend.app.services.checkout import create_checkout, record_return
from backend.app.schemas.borrowing_history import HistoryDetail, HistoryFilters, HistoryPage
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history


router = APIRouter(prefix="/checkouts", tags=["Checkouts"])


@router.post(
    "",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_checkout(
    checkout_data: CheckoutCreate,
    db: Session = Depends(get_inventory_write_db),
    authorized_user: User = Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return create_checkout(
        db=db,
        issued_by=authorized_user,
        **checkout_data.model_dump(),
    )


@router.post("/{checkout_id}/return", response_model=ReturnResponse)
def return_equipment(
    checkout_id: UUID,
    return_data: ReturnCreate,
    db: Session = Depends(get_inventory_write_db),
    authorized_user: User = Depends(require_roles(UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return record_return(db, checkout_id, authorized_user, return_data)


@router.get("", response_model=HistoryPage)
def borrowing_history(
    filters: Annotated[HistoryFilters, Query()],
    db: Session = Depends(get_db),
    authorized_user: User = Depends(require_roles(
        UserRole.STUDENT, UserRole.FACULTY, UserRole.LAB_ASSISTANT, UserRole.ADMIN,
    )),
):
    return list_borrowing_history(db, authorized_user, filters)


@router.get("/{checkout_id}", response_model=HistoryDetail)
def borrowing_history_detail(
    checkout_id: UUID,
    db: Session = Depends(get_db),
    authorized_user: User = Depends(require_roles(
        UserRole.STUDENT, UserRole.FACULTY, UserRole.LAB_ASSISTANT, UserRole.ADMIN,
    )),
):
    return get_borrowing_history(db, authorized_user, checkout_id)
