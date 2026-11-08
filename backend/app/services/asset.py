from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.asset import AssetCreate, AssetResponse, AssetUpdate
from backend.app.services.maintenance import ACTIVE, _locked
from backend.app.services.metadata import UNCERTAIN_METADATA_COMMIT, persist_metadata
from backend.app.services.inventory_transactions import flush_stage, transaction
from backend.app.services.registration import authorize_registration, UNCERTAIN_COMMIT_DETAIL


def lookup_asset(db: Session, nfc_tag_id: str) -> EquipmentAsset:
    """Tags identify assets; they never authenticate users or authorize changes."""
    if not isinstance(nfc_tag_id, str) or not nfc_tag_id.strip() or len(nfc_tag_id.strip()) > 100 or "\x00" in nfc_tag_id:
        raise HTTPException(422, "Invalid NFC tag")
    with db.no_autoflush:
        asset = db.scalar(select(EquipmentAsset).where(EquipmentAsset.nfc_tag_id == nfc_tag_id.strip()))
    if asset is None:
        raise HTTPException(404, "Asset not found")
    return asset


def _create_asset(db: Session, asset_data: AssetCreate, actor: User) -> AssetResponse:
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


def _location_conflict(db: Session, asset: EquipmentAsset) -> str | None:
    if asset.availability_status != "AVAILABLE" or asset.condition_status != "WORKING" or asset.maintenance_hold:
        return "Only available, working assets without a hold can be relocated"
    if _locked(db, EquipmentCheckout, EquipmentCheckout.asset_id == asset.id,
               EquipmentCheckout.status == "BORROWED") is not None:
        return "An asset with an active checkout cannot be relocated"
    if _locked(db, MaintenanceRequest, MaintenanceRequest.asset_id == asset.id,
               MaintenanceRequest.status.in_(ACTIVE)) is not None:
        return "An asset with active maintenance cannot be relocated"
    return None


@transaction(uncertain_commit_detail=UNCERTAIN_METADATA_COMMIT)
def update_asset(
    db: Session, asset_id: UUID, update_data: AssetUpdate, actor: User,
) -> AssetResponse:
    authorize_registration(db, actor)
    asset = _locked(db, EquipmentAsset, EquipmentAsset.id == asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    conflict = _location_conflict(db, asset) if "location" in update_data.model_fields_set else None
    staff = authorize_registration(db, actor)
    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    persist_metadata(db, asset, update_data, staff)
    return AssetResponse.model_validate(asset)


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_asset(db, asset_data: AssetCreate, actor: User) -> AssetResponse:
    return _create_asset(db, asset_data, actor)


@transaction(uncertain_commit_detail=UNCERTAIN_COMMIT_DETAIL)
def create_asset_bulk(db, items, actor: User) -> list[AssetResponse]:
    if not 1 <= len(items) <= 100:
        raise HTTPException(422, "A batch must contain between one and 100 records")
    return [_create_asset(db, item, actor) for item in items]
