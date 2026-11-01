from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.schemas.asset import AssetCreate


def create_asset(db: Session, asset_data: AssetCreate) -> EquipmentAsset:
    equipment = db.get(Equipment, asset_data.equipment_id)

    if equipment is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    if equipment.tracking_type != "INDIVIDUAL":
        raise HTTPException(
            status_code=400,
            detail="Assets can only be registered for individually tracked equipment",
        )

    asset = EquipmentAsset(**asset_data.model_dump())

    try:
        db.add(asset)
        db.commit()
        db.refresh(asset)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Asset code, NFC tag, or serial number already exists",
        )

    return asset