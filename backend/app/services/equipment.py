from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment
from backend.app.schemas.equipment import EquipmentCreate


def create_equipment(
    db: Session,
    equipment_data: EquipmentCreate,
) -> Equipment:
    """Register a new equipment definition in the database."""

    equipment = Equipment(
        **equipment_data.model_dump()
    )

    try:
        db.add(equipment)
        db.commit()
        db.refresh(equipment)

    except Exception:
        db.rollback()
        raise

    return equipment