from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.database import get_db
from backend.app.models.equipment import Equipment
from backend.app.schemas.equipment import EquipmentResponse


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