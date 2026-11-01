from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db
from backend.app.models.user import UserRole
from backend.app.schemas.asset import AssetCreate, AssetResponse
from backend.app.services.asset import create_asset


router = APIRouter(
    prefix="/assets",
    tags=["Assets"],
)


@router.post(
    "",
    response_model=AssetResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_asset(
    asset_data: AssetCreate,
    db: Session = Depends(get_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return create_asset(db, asset_data)