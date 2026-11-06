"""Shared persistence policy for audited metadata updates."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.asset import AssetUpdate
from backend.app.schemas.equipment import EquipmentUpdate
from backend.app.services.inventory_transactions import flush_stage

UNCERTAIN_METADATA_COMMIT = (
    "Metadata update outcome could not be confirmed. Check existing records before retrying."
)


def persist_metadata(
    db: Session, entity: Equipment | EquipmentAsset, update: EquipmentUpdate | AssetUpdate, actor: User,
) -> None:
    changed = {
        field: value for field, value in update.model_dump(exclude_unset=True).items()
        if getattr(entity, field) != value
    }
    if not changed:
        return
    db.execute(select(func.set_config("inventory.metadata_actor_id", str(actor.id), True)))
    for field, value in changed.items():
        setattr(entity, field, value)
    flush_stage(db, entity)
