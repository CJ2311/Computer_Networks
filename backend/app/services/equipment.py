from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment
from backend.app.models.user import User
from backend.app.schemas.equipment import EquipmentCreate, EquipmentResponse, EquipmentUpdate
from backend.app.services.maintenance import _locked
from backend.app.services.metadata import UNCERTAIN_METADATA_COMMIT, persist_metadata
from backend.app.services.inventory_transactions import flush_stage, transaction
from backend.app.services.registration import authorize_registration, UNCERTAIN_COMMIT_DETAIL


def _create_equipment(db: Session, equipment_data: EquipmentCreate, actor: User) -> EquipmentResponse:
    """Register one definition in an owned transaction; snapshot before commit."""
    authorize_registration(db, actor)
    equipment = Equipment(**equipment_data.model_dump())
    db.add(equipment)
    flush_stage(db, equipment)
    return EquipmentResponse.model_validate(equipment)


@transaction(uncertain_commit_detail=UNCERTAIN_METADATA_COMMIT)
def update_equipment(
    db: Session, equipment_id: UUID, update_data: EquipmentUpdate, actor: User,
) -> EquipmentResponse:
    authorize_registration(db, actor)
    equipment = _locked(db, Equipment, Equipment.id == equipment_id)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    staff = authorize_registration(db, actor)
    persist_metadata(db, equipment, update_data, staff)
    return EquipmentResponse.model_validate(equipment)


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_equipment(db, equipment_data: EquipmentCreate, actor: User) -> EquipmentResponse:
    return _create_equipment(db, equipment_data, actor)


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_equipment_bulk(db, items, actor: User) -> list[EquipmentResponse]:
    if not 1 <= len(items) <= 100:
        raise HTTPException(422, "A batch must contain between one and 100 records")
    return [_create_equipment(db, item, actor) for item in items]
