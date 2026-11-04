from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.asset import AssetCreate, AssetResponse
from backend.app.services.inventory_transactions import flush_stage, transaction
from backend.app.services.registration import authorize_registration, UNCERTAIN_COMMIT_DETAIL


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_asset(db: Session, asset_data: AssetCreate, actor: User) -> AssetResponse:
    authorize_registration(db, actor)
    equipment = db.get(Equipment, asset_data.equipment_id, populate_existing=True)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    if equipment.tracking_type != "INDIVIDUAL":
        raise HTTPException(status_code=400, detail="Assets can only be registered for individually tracked equipment")
    # State and IDs are absent from AssetCreate. Initial state remains governed
    # by the existing model/database defaults, without changing uniqueness rules.
    asset = EquipmentAsset(**asset_data.model_dump())
    db.add(asset)
    flush_stage(db, asset)
    return AssetResponse.model_validate(asset)
