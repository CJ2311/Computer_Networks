from backend.tests.inventory_session_stub import configure_session
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.user import User
from backend.app.routers.checkout import register_checkout
from backend.app.schemas.checkout import CheckoutCreate, CheckoutResponse
from backend.app.services.checkout import validate_checkout_deadline


@pytest.mark.parametrize(
    "deadline, expected_status",
    [
        (datetime(2020, 1, 1), 422),
        (datetime(2020, 1, 1, tzinfo=timezone.utc), 400),
    ],
)
def test_checkout_rejects_invalid_deadline(deadline, expected_status):
    with pytest.raises(HTTPException) as error:
        validate_checkout_deadline(deadline)
    assert error.value.status_code == expected_status


@pytest.mark.parametrize("conflict", [False, True])
def test_checkout_route_transaction_and_response(conflict, monkeypatch):
    now = datetime.now(timezone.utc)
    payload = CheckoutCreate(
        asset_id=uuid4(), student_id=uuid4(), due_at=now + timedelta(days=1)
    )
    staff = User(id=uuid4(), role="LAB_ASSISTANT", is_active=True)
    db = Mock()
    asset = EquipmentAsset(id=payload.asset_id, availability_status="AVAILABLE", condition_status="WORKING", maintenance_hold=False)
    student = User(id=payload.student_id, role="STUDENT", is_active=True)
    db.new, db.dirty, db.deleted, db.info = set(), set(), set(), {}
    db.no_autoflush = nullcontext()
    db.in_nested_transaction.return_value = False
    configure_session(db, monkeypatch)
    db.get.side_effect = lambda model, identifier, **kw: staff if identifier == staff.id else student
    def scalar(statement):
        if "clock_timestamp()" in str(statement):
            return now
        if statement.column_descriptions[0].get("entity") is Equipment:
            return Equipment(id=asset.equipment_id, name="Sensor")
        if statement.column_descriptions[0].get("entity") is EquipmentAsset:
            return asset
        return None
    db.scalar.side_effect = scalar
    def add(checkout):
        checkout.id = uuid4()
    db.add.side_effect = add
    if conflict:
        db.commit.side_effect = IntegrityError("checkout", {}, Exception())
        with pytest.raises(HTTPException) as error:
            register_checkout(payload, db=db, authorized_user=staff)
        assert error.value.status_code == 409
        db.rollback.assert_called_once()
        db.refresh.assert_not_called()
        return

    checkout = register_checkout(payload, db=db, authorized_user=staff)
    assert isinstance(checkout, CheckoutResponse)
    response = CheckoutResponse.model_validate(checkout)
    assert response.asset_id == payload.asset_id
    assert response.student_id == payload.student_id
    assert response.issued_by == staff.id
    assert response.due_at == payload.due_at
    assert response.status == "BORROWED"
    assert response.returned_at is None
    assert response.returned_to is None
    assert asset.availability_status == "BORROWED"
    db.add.assert_called_once()
    assert db.flush.call_count == 2
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_checkout_request_rejects_invalid_fields():
    with pytest.raises(ValidationError):
        CheckoutCreate(asset_id="invalid", student_id=uuid4(), due_at="invalid")
    with pytest.raises(ValidationError):
        CheckoutCreate(
            asset_id=uuid4(),
            student_id=uuid4(),
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
            notes="x" * 1001,
        )
