"""Parent-row locking serializes quantity adjustments, including the first insert."""
from uuid import UUID, uuid4
import json
from sqlalchemy import select, func
from backend.app.models.operations import StockLedger
from fastapi import HTTPException
from sqlalchemy.orm import Session
from backend.app.models.user import User
from backend.app.models.equipment import Equipment, EquipmentStock
from backend.app.schemas.stock import StockAdjustment, StockResponse
from backend.app.services.inventory_transactions import transaction, flush_stage
from backend.app.services.maintenance import _locked
from backend.app.services.registration import authorize_registration


def _quantity_equipment(equipment: Equipment | None) -> None:
    if equipment is None:
        raise HTTPException(404, "Equipment not found")
    if equipment.tracking_type != "QUANTITY":
        raise HTTPException(409, "Quantity stock is only available for quantity-tracked equipment")


def get_stock(db: Session, equipment_id: UUID) -> StockResponse:
    with db.no_autoflush:
        _quantity_equipment(db.get(Equipment, equipment_id, populate_existing=True))
        stock = db.get(EquipmentStock, equipment_id, populate_existing=True)
        return StockResponse(equipment_id=equipment_id, quantity=stock.quantity if stock else 0)


@transaction(uncertain_commit_detail="Stock adjustment outcome is uncertain. Reuse the original operation key, or reconcile manually if none was supplied.")
def adjust_stock(db: Session, equipment_id: UUID, data: StockAdjustment, actor: User) -> StockResponse:
    authorize_registration(db, actor)
    equipment = _locked(db, Equipment, Equipment.id == equipment_id)
    _quantity_equipment(equipment)
    stock = _locked(db, EquipmentStock, EquipmentStock.equipment_id == equipment_id)
    staff = authorize_registration(db, actor)
    key = data.idempotency_key or uuid4()
    existing = db.scalar(select(StockLedger).where(StockLedger.operation_key == key))
    if existing is not None:
        if (existing.equipment_id, existing.actor_id, existing.kind, existing.previous_quantity, existing.change, existing.reason) != (
                equipment_id, staff.id, "ADJUSTMENT", data.expected_quantity, data.change, data.reason):
            raise HTTPException(409, "Operation key was already used for a different request")
        return StockResponse(equipment_id=equipment_id, quantity=existing.quantity, operation_key=key)
    current = stock.quantity if stock else 0
    if data.expected_quantity != current:
        raise HTTPException(409, "Stock changed; reload the balance")
    quantity = current + data.change
    if not 0 <= quantity <= 2**31-1:
        raise HTTPException(409, "Stock would be outside the supported range")
    set_stock_context(db, staff.id, key, "ADJUSTMENT", data.reason)
    if stock is None:
        stock = EquipmentStock(equipment_id=equipment_id, quantity=quantity)
        db.add(stock)
    else:
        stock.quantity = quantity
    flush_stage(db, stock)
    return StockResponse(equipment_id=equipment_id, quantity=quantity, operation_key=key)


def set_stock_context(db, actor_id, key, kind, reason, loan_id=None):
    context = dict(actor_id=str(actor_id),operation_key=str(key),kind=kind,reason=reason,
                   loan_id=str(loan_id) if loan_id else None)
    db.execute(select(func.set_config("inventory.stock_context",json.dumps(context),True)))
