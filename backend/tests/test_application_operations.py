"""New operation contracts and safety entry points; no PostgreSQL connections."""
from unittest.mock import Mock
from uuid import uuid4
from datetime import datetime,timezone,timedelta
import pytest
from pydantic import ValidationError
from backend.app.schemas.asset import AssetBulkCreate
from backend.app.schemas.equipment import EquipmentBulkCreate
from backend.app.schemas.operations import QuantityLoanCreate,LoanPolicyInput
from backend.app.schemas.stock import StockAdjustment
from scripts import deploy_nfc_demo as baseline
from scripts import deploy_nfc_application as extension


@pytest.mark.parametrize("schema",[AssetBulkCreate,EquipmentBulkCreate])
def test_empty_bulk_is_invalid(schema):
    with pytest.raises(ValidationError):
        schema(items=[])


@pytest.mark.parametrize("quantity",[0,-1,True,1.5,2**31])
def test_quantity_loan_requires_positive_bounded_integer(quantity):
    with pytest.raises(ValidationError):
        QuantityLoanCreate(idempotency_key=uuid4(),equipment_id=uuid4(),student_id=uuid4(),quantity=quantity,due_at=datetime.now(timezone.utc)+timedelta(days=1))


def test_loan_policy_rejects_coerced_boolean():
    with pytest.raises(ValidationError):
        LoanPolicyInput(allow_offsite="true")


@pytest.mark.parametrize("reason",[" ","bad\x00"])
def test_stock_reason_is_required_valid_text(reason):
    with pytest.raises(ValidationError):
        StockAdjustment(expected_quantity=0,change=1,reason=reason)


def test_pinned_demo_does_not_include_extension_tables():
    assert not baseline.expected_tables(baseline.HEAD)&{"stock_ledger","quantity_loans","external_identities","equipment_loan_policies","loan_destinations","overdue_reminders"}


@pytest.mark.parametrize("target",["inventory_db","inventory_checkout_test"])
def test_extension_cli_rejects_protected_database_before_connection(monkeypatch,target):
    monkeypatch.setattr("sys.argv",["extension","--database",target])
    engine=Mock(side_effect=AssertionError("No database"))
    monkeypatch.setattr(baseline,"make_engine",engine)
    with pytest.raises(SystemExit) as error:
        extension.main()
    assert error.value.code==2
    engine.assert_not_called()


def test_extension_requires_explicit_typed_confirmation(monkeypatch):
    monkeypatch.setattr("sys.argv",["extension","--database","inventory_nfc_demo","--server-address","127.0.0.1","--port","5432","--administrator","postgres","--schema","public"])
    monkeypatch.setattr(extension.sys.stdin,"isatty",lambda:True)
    monkeypatch.setattr("builtins.input",lambda prompt:"wrong")
    engine=Mock(side_effect=AssertionError("No database"))
    monkeypatch.setattr(baseline,"make_engine",engine)
    prior=extension.logging.root.manager.disable
    try:
        assert extension.main()==1
    finally:
        extension.logging.disable(prior)
    engine.assert_not_called()


@pytest.mark.parametrize("condition",["DAMAGED","UNDER_REPAIR",None])
def test_bulk_returns_cannot_silently_restock_unserviceable_units(condition):
    from backend.app.schemas.operations import QuantityReturnCreate
    with pytest.raises(ValidationError):
        QuantityReturnCreate(idempotency_key=uuid4(),condition_status=condition)
