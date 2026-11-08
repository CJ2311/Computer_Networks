"""One declared-destination rule shared by individual and quantity checkout."""
from fastapi import HTTPException
from backend.app.models.operations import EquipmentLoanPolicy


def allowed_destination(db,equipment,origin,destination):
    if destination is not None:
        if not isinstance(destination,str) or not destination.strip() or len(destination)>150 or "\x00" in destination:
            raise HTTPException(422,"Invalid borrowing destination")
        destination=destination.strip()
    destination=destination or origin
    if destination!=origin:
        policy=db.get(EquipmentLoanPolicy,equipment.id,populate_existing=True)
        if policy is None or not policy.allow_offsite:
            raise HTTPException(409,"Equipment must remain at its recorded laboratory location")
    return destination
