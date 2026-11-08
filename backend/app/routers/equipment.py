from backend.app.schemas.equipment import EquipmentBulkCreate
from backend.app.services.equipment import create_equipment_bulk
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.equipment import Equipment
from backend.app.schemas.equipment import EquipmentResponse

from fastapi import status

from backend.app.auth.dependencies import require_roles
from backend.app.models.user import UserRole
from backend.app.schemas.equipment import EquipmentCreate, EquipmentUpdate
from backend.app.services.equipment import create_equipment, update_equipment
from backend.app.schemas.stock import StockAdjustment, StockResponse
from backend.app.services.stock import get_stock, adjust_stock


router = APIRouter(
    prefix="/equipment",
    tags=["Equipment"],
)


@router.get("", response_model=list[EquipmentResponse])
def list_equipment(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    _authorized_user=Depends(require_roles(*UserRole)),
):
    """Retrieve registered equipment definitions."""

    statement = (
        select(Equipment)
        .order_by(Equipment.name, Equipment.id)
        .limit(limit).offset(offset)
    )

    equipment = db.scalars(statement).all()

    return equipment

@router.get("/{equipment_id}", response_model=EquipmentResponse)
def get_equipment(
    equipment_id: UUID,
    db: Session = Depends(get_db),
    _authorized_user=Depends(require_roles(*UserRole)),
):
    """Retrieve a specific equipment definition by its UUID."""

    equipment = db.get(Equipment, equipment_id)

    if equipment is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    return equipment

@router.post(
    "",
    response_model=EquipmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_equipment(
    equipment_data: EquipmentCreate,
    db: Session = Depends(get_inventory_write_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return create_equipment(db, equipment_data, actor=_authorized_user)

@router.patch("/{equipment_id}", response_model=EquipmentResponse)
def patch_equipment(
    equipment_id: UUID,
    update_data: EquipmentUpdate,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return update_equipment(db, equipment_id, update_data, actor)


@router.get("/{equipment_id}/stock", response_model=StockResponse)
def quantity_stock(
    equipment_id: UUID, db: Session = Depends(get_db),
    actor=Depends(require_roles(*UserRole)),
):
    return get_stock(db, equipment_id)


@router.post("/{equipment_id}/stock/adjustments", response_model=StockResponse)
def change_stock(
    equipment_id: UUID, data: StockAdjustment,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return adjust_stock(db, equipment_id, data, actor)


@router.post("/bulk", response_model=list[EquipmentResponse], status_code=201)
def register_equipment_bulk(
    data: EquipmentBulkCreate, db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN,UserRole.FACULTY,UserRole.LAB_ASSISTANT)),
):
    return create_equipment_bulk(db,data.items,actor)
