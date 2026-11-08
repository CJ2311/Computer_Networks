"""Quantity balance behavior on an in-memory database, never PostgreSQL."""
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select, event
from backend.app.models.operations import StockLedger
from sqlalchemy.orm import Session
from backend.app.models.equipment import Equipment, EquipmentStock
from backend.app.models.user import User
from backend.app.schemas.stock import StockAdjustment
from backend.app.services.stock import adjust_stock, get_stock


@pytest.fixture
def stock():
    engine = create_engine("sqlite://")
    event.listen(engine,"connect",lambda connection,record:connection.create_function("set_config",3,lambda key,value,local:value))
    for table in (User.__table__, Equipment.__table__, EquipmentStock.__table__, StockLedger.__table__):
        table.create(engine)
    user = User(id=uuid4(), college_id="STAFF",full_name="Staff",email="staff@example.invalid", role="FACULTY",is_active=True)
    equipment=Equipment(id=uuid4(),name="Resistors",category="Electronics",default_location="Lab",tracking_type="QUANTITY")
    with Session(engine,expire_on_commit=False) as db:
        db.add_all([user,equipment]); db.commit()
    yield engine,user,equipment
    engine.dispose()


def change(stock,expected,delta):
    engine,user,equipment=stock
    with Session(engine) as db:
        return adjust_stock(db,equipment.id,StockAdjustment(expected_quantity=expected,change=delta),user)


def test_first_balance_insert_and_subsequent_update_use_guarded_transactions(stock):
    assert change(stock,0,10).quantity==10
    assert change(stock,10,-3).quantity==7
    with Session(stock[0]) as db:
        assert get_stock(db,stock[2].id).quantity==7


@pytest.mark.parametrize("expected,delta",[(9,-1),(10,-11),(10,2**31-1)])
def test_stale_negative_and_overflow_leave_balance_unchanged(stock,expected,delta):
    change(stock,0,10)
    with pytest.raises(HTTPException) as error:
        change(stock,expected,delta)
    assert error.value.status_code==409
    with Session(stock[0]) as db:
        assert db.get(EquipmentStock,stock[2].id).quantity==10


@pytest.mark.parametrize("role,active",[("STUDENT",True),("FACULTY",False)])
def test_unauthorized_balance_adjustment_is_rolled_back(stock,role,active):
    with Session(stock[0]) as db:
        user=db.get(User,stock[1].id); user.role=role;user.is_active=active; db.commit()
    with pytest.raises(HTTPException) as error:
        change(stock,0,5)
    assert error.value.status_code==403
    with Session(stock[0]) as db:
        assert db.get(EquipmentStock,stock[2].id) is None


def test_individual_equipment_rejected(stock):
    with Session(stock[0]) as db:
        equipment=db.get(Equipment,stock[2].id);equipment.tracking_type="INDIVIDUAL";db.commit()
    with pytest.raises(HTTPException) as error:
        change(stock,0,1)
    assert error.value.status_code==409


@pytest.mark.parametrize("payload",[dict(expected_quantity=True,change=1),dict(expected_quantity=0,change=0),dict(expected_quantity=-1,change=1),dict(expected_quantity=0,change=1.5)])
def test_invalid_adjustment_input(payload):
    with pytest.raises(ValidationError):
        StockAdjustment(**payload)


def test_stock_failed_commit_rolls_back_first_insert(stock):
    from sqlalchemy import event
    engine,user,equipment=stock
    with Session(engine) as db:
        def reject_commit(session):
            raise RuntimeError("commit interrupted")
        event.listen(db,"before_commit",reject_commit)
        with pytest.raises(RuntimeError,match="commit interrupted"):
            adjust_stock(db,equipment.id,StockAdjustment(expected_quantity=0,change=2),user)
    with Session(engine) as db:
        assert db.get(EquipmentStock,equipment.id) is None


def test_missing_equipment_is_404(stock):
    engine,user,equipment=stock
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            adjust_stock(db,uuid4(),StockAdjustment(expected_quantity=0,change=1),user)
        assert error.value.status_code==404


def test_uninitialized_stock_reads_zero_without_insert(stock):
    engine,user,equipment=stock
    with Session(engine) as db:
        assert get_stock(db,equipment.id).quantity==0
        assert db.get(EquipmentStock,equipment.id) is None
