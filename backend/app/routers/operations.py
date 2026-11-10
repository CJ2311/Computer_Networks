"""Quantity custody, stock history and explicit equipment lending policy."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import UserRole
from backend.app.models.operations import StockLedger, EquipmentLoanPolicy, LoanDestination
from backend.app.models.equipment import Equipment
from backend.app.schemas.operations import QuantityLoanCreate, QuantityReturnCreate, QuantityLoanResponse, QuantityLoanPage, LoanPolicyInput, LedgerResponse
from backend.app.services.quantity_loans import issue_quantity, return_quantity, list_quantity, set_policy
from backend.app.services.borrowing_history import get_borrowing_history
from fastapi import HTTPException

router=APIRouter(tags=["Inventory operations"])
staff=require_roles(UserRole.ADMIN,UserRole.FACULTY,UserRole.LAB_ASSISTANT)
readers=require_roles(*UserRole)


@router.post("/quantity-loans",response_model=QuantityLoanResponse,status_code=201)
def issue(data: QuantityLoanCreate,db=Depends(get_inventory_write_db),actor=Depends(staff)):
    return issue_quantity(db,data,actor)


@router.post("/quantity-loans/{loan_id}/return",response_model=QuantityLoanResponse)
def receive(loan_id: UUID,data: QuantityReturnCreate,db=Depends(get_inventory_write_db),actor=Depends(require_roles(UserRole.FACULTY,UserRole.LAB_ASSISTANT))):
    return return_quantity(db,loan_id,data,actor)


@router.get("/quantity-loans", response_model=QuantityLoanPage)
def history(
    student_id: UUID | None = Query(default=None),
    equipment_id: UUID | None = Query(default=None),
    status: Literal["BORROWED", "RETURNED"] | None = Query(default=None),
    overdue: bool | None = Query(default=None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, lt=2**63),
    db=Depends(get_db),
    actor=Depends(readers),
):
    return list_quantity(
        db,
        actor,
        limit,
        offset,
        overdue=overdue,
        student_id=student_id,
        equipment_id=equipment_id,
        status=status,
    )


@router.get("/quantity-loans/{loan_id}",response_model=QuantityLoanResponse)
def detail(loan_id: UUID,db=Depends(get_db),actor=Depends(readers)):
    return list_quantity(db,actor,loan_id=loan_id)


@router.get("/equipment/{equipment_id}/ledger",response_model=list[LedgerResponse])
def ledger(equipment_id: UUID,db=Depends(get_db),actor=Depends(staff),limit: int=Query(25,ge=1,le=100),offset: int=Query(0,ge=0,lt=2**63)):
    return db.scalars(select(StockLedger).where(StockLedger.equipment_id==equipment_id).order_by(StockLedger.revision).limit(limit).offset(offset)).all()


@router.post("/equipment/{equipment_id}/loan-policy")
def policy(equipment_id: UUID,data: LoanPolicyInput,db=Depends(get_inventory_write_db),actor=Depends(require_roles(UserRole.ADMIN,UserRole.FACULTY))):
    return set_policy(db,equipment_id,data,actor)


@router.get("/equipment/{equipment_id}/loan-policy")
def get_policy(equipment_id: UUID,db=Depends(get_db),actor=Depends(readers)):
    if db.get(Equipment,equipment_id) is None:
        raise HTTPException(404,"Equipment not found")
    record=db.get(EquipmentLoanPolicy,equipment_id)
    return dict(equipment_id=equipment_id,allow_offsite=record.allow_offsite if record else False)


@router.get("/checkouts/{checkout_id}/destination")
def destination(checkout_id: UUID,db=Depends(get_db),actor=Depends(readers)):
    get_borrowing_history(db,actor,checkout_id)  # same non-disclosing student scope
    row=db.get(LoanDestination,checkout_id)
    return dict(checkout_id=checkout_id,destination=row.destination if row else None)


@router.get("/students/lookup")
def student_lookup(college_id: str=Query(min_length=1,max_length=50,pattern=r"^[^\x00]+$"),db=Depends(get_db),actor=Depends(staff)):
    from backend.app.models.user import User
    user=db.scalar(select(User).where(User.college_id==college_id.strip(),User.role=="STUDENT",User.is_active.is_(True)))
    if user is None:
        raise HTTPException(404,"Active student not found")
    return dict(id=user.id,college_id=user.college_id,full_name=user.full_name)
