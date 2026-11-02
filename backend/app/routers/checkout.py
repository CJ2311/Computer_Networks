from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db
from backend.app.models.user import User, UserRole
from backend.app.schemas.checkout import CheckoutCreate, CheckoutResponse
from backend.app.services.checkout import create_checkout


router = APIRouter(prefix="/checkouts", tags=["Checkouts"])


@router.post(
    "",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_checkout(
    checkout_data: CheckoutCreate,
    db: Session = Depends(get_db),
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
