from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.app.models.equipment import EquipmentAsset
from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import UserRole
from backend.app.schemas.asset import AssetCreate, AssetResponse, AssetUpdate
from backend.app.services.asset import create_asset, update_asset

from uuid import UUID
from fastapi import HTTPException

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
    db: Session = Depends(get_inventory_write_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return create_asset(db, asset_data, actor=_authorized_user)

@router.get(
    "",
    response_model=list[AssetResponse],
)
def list_assets(
    db: Session = Depends(get_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return db.scalars(
        select(EquipmentAsset)
        .order_by(EquipmentAsset.asset_code)
        .limit(100)
    ).all()

@router.get(
    "/{asset_id}",
    response_model=AssetResponse,
)
def get_asset(
    asset_id: UUID,
    db: Session = Depends(get_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    asset = db.get(EquipmentAsset, asset_id)

    if asset is None:
        raise HTTPException(
            status_code=404,
            detail="Asset not found",
        )

    return asset

@router.patch("/{asset_id}", response_model=AssetResponse)
def patch_asset(
    asset_id: UUID,
    update_data: AssetUpdate,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return update_asset(db, asset_id, update_data, actor)
