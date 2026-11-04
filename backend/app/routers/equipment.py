from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.equipment import Equipment
from backend.app.schemas.equipment import EquipmentResponse

from fastapi import status

from backend.app.auth.dependencies import require_roles
from backend.app.models.user import UserRole
from backend.app.schemas.equipment import EquipmentCreate
from backend.app.services.equipment import create_equipment


router = APIRouter(
    prefix="/equipment",
    tags=["Equipment"],
)


@router.get("", response_model=list[EquipmentResponse])
def list_equipment(
    db: Session = Depends(get_db),
):
    """Retrieve registered equipment definitions."""

    statement = (
        select(Equipment)
        .order_by(Equipment.name, Equipment.id)
        .limit(100)
    )

    equipment = db.scalars(statement).all()

    return equipment

@router.get("/{equipment_id}", response_model=EquipmentResponse)
def get_equipment(
    equipment_id: UUID,
    db: Session = Depends(get_db),
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