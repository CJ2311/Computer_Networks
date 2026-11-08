"""Regression coverage for authenticated identity and NFC lookup."""
import hashlib
import json
from contextlib import nullcontext
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.tokens import token_identity, token_mode
from backend.app.models.user import User
from backend.app.schemas.asset import AssetCreate
from backend.app.schemas.equipment import EquipmentCreate
from backend.app.schemas.maintenance import MaintenanceNoteCreate
from backend.app.services.asset import lookup_asset

TOKEN = "unit_test_token_" + "x" * 40


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("AUTH_TOKEN_SHA256_MAP", raising=False)


def configure(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "opaque_token")
    monkeypatch.setenv("AUTH_TOKEN_SHA256_MAP", json.dumps({hashlib.sha256(TOKEN.encode()).hexdigest(): "STAFF-1"}))


def request():
    return Request({"type": "http", "client": ("198.51.100.1", 1234), "headers": []})


def test_bearer_identity_uses_persisted_role_and_activation(monkeypatch):
    configure(monkeypatch)
    user = User(id=uuid4(), college_id="STAFF-1", role="FACULTY", is_active=True)
    db = Mock()
    db.scalar.return_value = user
    assert get_current_user(request(), db=db, authorization="Bearer " + TOKEN) is user
    statement = db.scalar.call_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
    assert statement.compile().params["college_id_1"] == "STAFF-1"


@pytest.mark.parametrize("header", [None, "", "Basic xyz", "Bearer short", "Bearer " + "y" * 43, "Bearer  " + TOKEN])
def test_invalid_bearer_rejected_before_database(monkeypatch, header):
    configure(monkeypatch)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        get_current_user(request(), db=db, authorization=header)
    assert error.value.status_code == 401
    db.scalar.assert_not_called()


@pytest.mark.parametrize("mapping", [None, "not-json", "{}", "[]", '{"secret":"ADMIN"}', '{"' + "a"*64 + '":" "}'])
def test_bad_auth_configuration_fails_closed(monkeypatch, mapping):
    monkeypatch.setenv("AUTH_MODE", "opaque_token")
    if mapping is not None:
        monkeypatch.setenv("AUTH_TOKEN_SHA256_MAP", mapping)
    with pytest.raises(HTTPException) as error:
        token_identity("Bearer " + TOKEN)
    assert error.value.status_code == 503
    assert "secret" not in error.value.detail


@pytest.mark.parametrize("user,code", [(None,401), (User(is_active=False),403)])
def test_unknown_or_inactive_identity_denied(monkeypatch,user,code):
    configure(monkeypatch)
    db=Mock()
    db.scalar.return_value=user
    with pytest.raises(HTTPException) as error:
        get_current_user(request(),db=db,authorization="Bearer " + TOKEN)
    assert error.value.status_code == code


def test_bearer_never_falls_back_to_dev_identity(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as error:
        get_current_user(request(),x_dev_user="DEV-ADMIN",db=Mock(),authorization="Bearer " + TOKEN)
    assert error.value.status_code == 503
    configure(monkeypatch)
    with pytest.raises(HTTPException) as error:
        get_current_user(request(),x_dev_user="DEV-ADMIN",db=Mock(),authorization="Bearer " + TOKEN)
    assert error.value.status_code == 401


def test_unknown_auth_mode_is_configuration_failure(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "typo")
    with pytest.raises(HTTPException) as error:
        token_mode()
    assert error.value.status_code == 503


def test_nfc_lookup_binds_normalized_tag_without_writes():
    db=Mock(no_autoflush=nullcontext())
    asset=object()
    db.scalar.return_value=asset
    assert lookup_asset(db,"  TAG-1  ") is asset
    assert db.scalar.call_args.args[0].compile().params["nfc_tag_id_1"] == "TAG-1"
    db.commit.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.parametrize("tag", ["", " ", "tag\x00", "x"*101, None])
def test_invalid_nfc_lookup_does_not_query(tag):
    db=Mock()
    with pytest.raises(HTTPException) as error:
        lookup_asset(db,tag)
    assert error.value.status_code == 422
    db.scalar.assert_not_called()


def test_unknown_nfc_is_404():
    db=Mock(no_autoflush=nullcontext())
    db.scalar.return_value=None
    with pytest.raises(HTTPException) as error:
        lookup_asset(db,"unknown")
    assert error.value.status_code == 404


@pytest.mark.parametrize("schema,payload", [
    (EquipmentCreate, dict(name="Scope\x00",category="Lab",default_location="Lab",tracking_type="INDIVIDUAL")),
    (AssetCreate, dict(asset_code="A",location="Lab",equipment_id=uuid4(),nfc_tag_id="tag\x00")),
    (MaintenanceNoteCreate,dict(expected_version=1,note=" \n ")),
    (MaintenanceNoteCreate,dict(expected_version=1,note="bad\x00")),
    (MaintenanceNoteCreate,dict(expected_version=True,note="note")),
    (MaintenanceNoteCreate,dict(expected_version=1,note="note",status="COMPLETED")),
])
def test_inputs_reject_invalid_or_injected_fields(schema,payload):
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_checkout_notes_reject_database_invalid_nul():
    from backend.app.schemas.checkout import CheckoutCreate,ReturnCreate
    from datetime import datetime,timezone,timedelta
    for schema,payload in (
        (CheckoutCreate,dict(asset_id=uuid4(),student_id=uuid4(),due_at=datetime.now(timezone.utc)+timedelta(days=1),notes="bad\x00")),
        (ReturnCreate,dict(condition_status="WORKING",notes="bad\x00")),
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(payload)


@pytest.mark.parametrize("first",["equipment","checkout","maintenance","inventory_audit"])
def test_metadata_import_order_is_independent_of_application_credentials(first):
    import subprocess
    import sys
    import os
    code='''
import importlib
import sys
import dotenv
from unittest.mock import Mock
dotenv.load_dotenv = Mock(side_effect=AssertionError("No dotenv for models"))
importlib.import_module("backend.app.models." + sys.argv[1])
from backend.app.base import Base
from sqlalchemy.orm import configure_mappers
configure_mappers()
expected={"users","equipment","equipment_assets","equipment_stock","equipment_checkouts", "maintenance_requests","maintenance_events","metadata_audit_events","inventory_audits", "inventory_audit_expected_assets","inventory_audit_scans","inventory_audit_discrepancies"}
expected|={"external_identities","equipment_loan_policies","loan_destinations","quantity_loans","stock_ledger","overdue_reminders"}
assert set(Base.metadata.tables)==expected
assert "backend.app.database" not in sys.modules
'''
    env=dict(os.environ)
    env.pop("INVENTORY_APP_PASSWORD",None)
    env.pop("INVENTORY_MIGRATOR_PASSWORD",None)
    result=subprocess.run([sys.executable,"-c",code,first],env=env,capture_output=True,text=True,timeout=30)
    # Child only imports metadata; report the fixed exit status, never ambient values.
    assert result.returncode==0
