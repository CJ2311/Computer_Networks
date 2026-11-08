"""Full-quantity custody with atomic stock/ledger changes and retry keys."""
from fastapi import HTTPException
import hashlib
import json
from datetime import timezone
from backend.app.services.lending_policy import allowed_destination
from sqlalchemy import select
from backend.app.models.equipment import Equipment, EquipmentStock
from backend.app.models.operations import QuantityLoan, EquipmentLoanPolicy
from backend.app.services.inventory_transactions import transaction, flush_stage
from backend.app.services.registration import authorize_registration
from backend.app.services.maintenance import _locked, _authorize, _now
from backend.app.services.checkout import validate_borrowing_student, validate_checkout_deadline
from backend.app.services.stock import _quantity_equipment, set_stock_context
from backend.app.schemas.operations import QuantityLoanResponse


def _balance(db,equipment_id,delta,actor,key,kind,loan_id):
    stock = _locked(db,EquipmentStock,EquipmentStock.equipment_id==equipment_id)
    current = stock.quantity if stock else 0
    quantity = current + delta
    if not 0 <= quantity <= 2**31-1:
        raise HTTPException(409,"Insufficient stock or quantity overflow")
    set_stock_context(db,actor.id,key,kind,"Quantity loan " + kind.lower() + (" (WORKING)" if kind=="RETURN" else ""),loan_id)
    if stock is None:
        stock=EquipmentStock(equipment_id=equipment_id,quantity=quantity)
        db.add(stock)
    else:
        stock.quantity=quantity
    flush_stage(db,stock)


@transaction(uncertain_commit_detail="Quantity loan outcome is uncertain. Retry only with the same operation key.")
def issue_quantity(db,data,actor):
    authorize_registration(db,actor)
    equipment=_locked(db,Equipment,Equipment.id==data.equipment_id)
    _quantity_equipment(equipment)
    staff=authorize_registration(db,actor)
    if data.due_at.tzinfo is None or data.due_at.utcoffset() is None:
        raise HTTPException(422,"Return deadline must include a timezone")
    fingerprint=hashlib.sha256(json.dumps([str(data.equipment_id),str(data.student_id),data.quantity,
        str(staff.id),data.due_at.astimezone(timezone.utc).isoformat(),data.destination],separators=(",",":")).encode()).hexdigest()
    existing=db.get(QuantityLoan,data.idempotency_key,populate_existing=True)
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409,"Loan operation key was already used")
        return loan_view(db,existing)
    validate_borrowing_student(db,data.student_id)
    now=_now(db)
    deadline=validate_checkout_deadline(data.due_at,now)
    destination=allowed_destination(db,equipment,equipment.default_location,data.destination)
    loan=QuantityLoan(id=data.idempotency_key,equipment_id=equipment.id,student_id=data.student_id,quantity=data.quantity,
        issued_by=staff.id,request_fingerprint=fingerprint,equipment_name=equipment.name,destination=destination,due_at=deadline,checked_out_at=now)
    db.add(loan)
    flush_stage(db,loan)
    _balance(db,equipment.id,-data.quantity,staff,loan.id,"ISSUE",loan.id)
    validate_checkout_deadline(deadline,_now(db))
    return loan_view(db,loan)


@transaction(uncertain_commit_detail="Quantity return outcome is uncertain. Retry only with the same operation key.")
def return_quantity(db,loan_id,data,actor):
    if data.condition_status!="WORKING":
        raise HTTPException(422,"Only inspected working quantity returns can be restocked")
    roles={"FACULTY","LAB_ASSISTANT"}
    _authorize(db,actor,roles)
    equipment_id=db.scalar(select(QuantityLoan.equipment_id).where(QuantityLoan.id==loan_id))
    if equipment_id is None:
        raise HTTPException(404,"Quantity loan not found")
    equipment=_locked(db,Equipment,Equipment.id==equipment_id)
    _quantity_equipment(equipment)
    loan=_locked(db,QuantityLoan,QuantityLoan.id==loan_id)
    staff=_authorize(db,actor,roles)
    if loan.returned_at:
        if loan.return_key!=data.idempotency_key or loan.returned_to!=staff.id:
            raise HTTPException(409,"Quantity loan was already returned")
        return loan_view(db,loan)
    now=_now(db)
    if now<loan.checked_out_at:
        raise HTTPException(409,"Return precedes checkout")
    loan.returned_at,loan.returned_to,loan.return_key=now,staff.id,data.idempotency_key
    flush_stage(db,loan)
    _balance(db,equipment_id,loan.quantity,staff,data.idempotency_key,"RETURN",loan.id)
    return loan_view(db,loan)


def loan_view(db,loan,now=None):
    now=now or _now(db)
    return QuantityLoanResponse.model_validate(loan).model_copy(update={
        "is_currently_overdue":loan.returned_at is None and loan.due_at<now,
        "was_returned_late":loan.returned_at is not None and loan.returned_at>loan.due_at,
    })


def list_quantity(db,actor,limit=25,offset=0,loan_id=None,overdue=None):
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<2**63:
        raise HTTPException(422,"Invalid quantity history pagination")
    with db.no_autoflush:
        user=_authorize(db,actor,{"ADMIN","FACULTY","LAB_ASSISTANT","STUDENT"})
        now=_now(db)
        stmt=select(QuantityLoan)
        if user.role=="STUDENT":
            stmt=stmt.where(QuantityLoan.student_id==user.id)
        if overdue is not None:
            predicate=(QuantityLoan.returned_at.is_(None)) & (QuantityLoan.due_at<now)
            stmt=stmt.where(predicate.is_(overdue))
        if loan_id is not None:
            result=db.scalar(stmt.where(QuantityLoan.id==loan_id))
            if result is None:
                raise HTTPException(404,"Quantity loan not found")
            return loan_view(db,result,now)
        rows=db.scalars(stmt.order_by(QuantityLoan.checked_out_at.desc(),QuantityLoan.id).limit(limit+1).offset(offset)).all()
        return dict(items=[loan_view(db,row,now) for row in rows[:limit]],limit=limit,offset=offset,has_more=len(rows)>limit)


@transaction
def set_policy(db,equipment_id,data,actor):
    _authorize(db,actor,{"ADMIN","FACULTY"})
    equipment=_locked(db,Equipment,Equipment.id==equipment_id)
    if equipment is None:
        raise HTTPException(404,"Equipment not found")
    _authorize(db,actor,{"ADMIN","FACULTY"})
    policy=db.get(EquipmentLoanPolicy,equipment_id,populate_existing=True)
    if policy is None:
        policy=EquipmentLoanPolicy(equipment_id=equipment_id,allow_offsite=data.allow_offsite)
        db.add(policy)
    else:
        policy.allow_offsite=data.allow_offsite
    flush_stage(db,policy)
    return dict(equipment_id=equipment_id,allow_offsite=policy.allow_offsite)
