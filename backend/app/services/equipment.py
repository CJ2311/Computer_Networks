from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment
from backend.app.models.user import User
from backend.app.schemas.equipment import EquipmentCreate, EquipmentResponse
from backend.app.services.inventory_transactions import flush_stage, transaction
from backend.app.services.registration import authorize_registration, UNCERTAIN_COMMIT_DETAIL


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_equipment(db: Session, equipment_data: EquipmentCreate, actor: User) -> EquipmentResponse:
    """Register one definition in an owned transaction; snapshot before commit."""
    authorize_registration(db, actor)
    equipment = Equipment(**equipment_data.model_dump())
    db.add(equipment)
    flush_stage(db, equipment)
    return EquipmentResponse.model_validate(equipment)
