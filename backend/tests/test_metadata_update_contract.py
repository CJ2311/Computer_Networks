"""Update request contract. Missing future schemas/services are explicitly pending."""
import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database import engine
from backend.app.models.user import User
from backend.app.models.checkout import EquipmentCheckout
from backend.app.schemas.checkout import CheckoutCreate, ReturnCreate
from backend.app.schemas.borrowing_history import HistoryItem
from backend.app.services.borrowing_history import _history_statement
from backend.tests.registration_helpers import registration_app
from backend.tests.test_inventory_transaction_ownership import recording_engine, Parent
from backend.tests.update_contract_helpers import (
    CONTRACTS, schema_for, runtime_for, patch_json, CHECKOUT_SNAPSHOT_FIELDS, checkout_snapshots_ready,
)


@pytest.fixture(autouse=True)
def block_application_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Application database forbidden in update contract tests")
    monkeypatch.setattr(engine, "connect", forbidden)
    monkeypatch.setattr(engine, "raw_connection", forbidden)


@pytest.fixture(params=["equipment", "asset"])
def contract(request):
    return request.param, CONTRACTS[request.param]


@pytest.fixture
def update_schema(contract):
    kind, _ = contract
    return schema_for(kind)


def test_editable_fields_are_real_metadata_columns_and_exclude_workflow_state(contract):
    _, contract = contract
    columns = contract["model"].__table__.c
    assert contract["editable"] <= set(columns.keys())
    assert contract["editable"].isdisjoint(contract["immutable"])
    assert "quantity" not in contract["editable"]


def test_nullable_contract_matches_existing_database_columns(contract):
    _, contract = contract
    columns = contract["model"].__table__.c
    assert {name for name in contract["editable"] if columns[name].nullable} == contract["nullable"]


@pytest.mark.parametrize("kind, field, maximum", [
    (kind, field, maximum) for kind, contract in CONTRACTS.items() for field, maximum in contract["lengths"].items()
])
def test_contract_limits_match_current_model_columns(kind, field, maximum):
    assert CONTRACTS[kind]["model"].__table__.c[field].type.length == maximum


def test_update_schema_exposes_only_editable_fields(contract, update_schema):
    _, contract = contract
    assert set(update_schema.model_fields) == contract["editable"]


def test_empty_update_rejected(update_schema):
    with pytest.raises(ValidationError):
        update_schema.model_validate({})


def test_partial_update_preserves_omission(contract, update_schema):
    kind, _ = contract
    field = "name" if kind == "equipment" else "asset_code"
    value = update_schema.model_validate({field: " Updated "})
    assert value.model_fields_set == {field}
    assert value.model_dump(exclude_unset=True) == {field: "Updated"}


@pytest.mark.parametrize("kind, field", [(kind, field) for kind, c in CONTRACTS.items() for field in sorted(c["nullable"])])
def test_explicit_null_is_present_and_clears_only_nullable_field(kind, field):
    schema = schema_for(kind)
    value = schema.model_validate({field: None})
    assert value.model_fields_set == {field}
    assert value.model_dump(exclude_unset=True) == {field: None}


@pytest.mark.parametrize("value", [None, "", "  ", "\t\n", 123])
def test_required_fields_cannot_be_null_blank_or_nonstrings(contract, update_schema, value):
    _, contract = contract
    for field in contract["required"]:
        with pytest.raises(ValidationError):
            update_schema.model_validate({field: value})


def test_unknown_and_immutable_fields_rejected(contract, update_schema):
    _, contract = contract
    for field in contract["immutable"] | {"unknown", "quantity"}:
        with pytest.raises(ValidationError) as error:
            update_schema.model_validate({field: str(uuid4())})
        assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


def test_text_length_boundaries_preserve_existing_rules(contract, update_schema):
    _, contract = contract
    for field, maximum in contract["lengths"].items():
        value = update_schema.model_validate({field: " " + "X" * maximum + " "})
        assert value.model_dump(exclude_unset=True)[field] == "X" * maximum
        with pytest.raises(ValidationError):
            update_schema.model_validate({field: "X" * (maximum + 1)})


@pytest.mark.parametrize("kind, field", [("equipment", "manufacturer"), ("equipment", "model_number"),
                                         ("asset", "nfc_tag_id"), ("asset", "serial_number")])
def test_blank_optional_identifiers_and_labels_normalize_to_null(kind, field):
    value = schema_for(kind).model_validate({field: "  "})
    assert value.model_dump(exclude_unset=True) == {field: None}


@pytest.mark.parametrize("value, valid", [("0", True), ("9999999999.99", True), ("-1", False),
                                         ("1.001", False), ("10000000000", False), ("NaN", False), ("Infinity", False)])
def test_equipment_update_price_rules(value, valid):
    schema = schema_for("equipment")
    if valid:
        assert schema.model_validate({"purchase_price": value}).purchase_price == Decimal(value)
    else:
        with pytest.raises(ValidationError):
            schema.model_validate({"purchase_price": value})


@pytest.mark.parametrize("work", ["root", "pending", "flushed", "sql", "savepoint"])
def test_future_update_entry_preserves_caller_transaction(contract, recording_engine, work):
    kind, _ = contract
    schema, operation, _ = runtime_for(kind)
    recording, state = recording_engine
    with Session(recording) as db:
        if work == "root":
            db.begin()
        elif work == "savepoint":
            db.begin_nested()
        elif work == "sql":
            db.execute(insert(Parent).values(id=1))
        else:
            db.add(Parent(id=1))
            if work == "flushed":
                db.flush()
        root, nested = db.get_transaction(), db.get_nested_transaction()
        before = list(state.sql), state.commits, state.rollbacks
        data = schema.model_validate({"name" if kind == "equipment" else "asset_code": "Updated"})
        with pytest.raises(HTTPException) as error:
            operation(db, **{f"{kind}_id": uuid4()}, update_data=data, actor=User(id=uuid4()))
        assert error.value.status_code == 409
        assert db.get_transaction() is root and root.is_active
        assert db.get_nested_transaction() is nested
        assert before == (state.sql, state.commits, state.rollbacks)


def test_future_update_rejects_external_connection_without_disturbing_it(contract, recording_engine):
    kind, _ = contract
    schema, operation, _ = runtime_for(kind)
    recording, state = recording_engine
    with recording.connect() as connection, connection.begin() as external:
        with Session(connection) as db:
            before = state.commits, state.rollbacks
            with pytest.raises(HTTPException) as error:
                operation(db, **{f"{kind}_id": uuid4()}, update_data=schema.model_validate(
                    {"name" if kind == "equipment" else "asset_code": "Updated"}), actor=User(id=uuid4()))
            assert error.value.status_code == 409 and external.is_active
            assert not db.in_transaction() and before == (state.commits, state.rollbacks)


def test_future_update_rejects_failed_session_without_rolling_it_back(contract, recording_engine):
    kind, _ = contract
    schema, operation, _ = runtime_for(kind)
    recording, state = recording_engine
    def fail(*args):
        raise RuntimeError("Injected SQL failure")
    with Session(recording) as db:
        db.add(Parent(id=1))
        event.listen(recording, "before_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError):
                db.flush()
        finally:
            event.remove(recording, "before_cursor_execute", fail)
        assert not db.is_active
        root, rollbacks = db.get_transaction(), state.rollbacks
        with pytest.raises(HTTPException) as error:
            operation(db, **{f"{kind}_id": uuid4()}, update_data=schema.model_validate(
                {"name" if kind == "equipment" else "asset_code": "Updated"}), actor=User(id=uuid4()))
        assert error.value.status_code == 409 and db.get_transaction() is root
        assert not db.is_active and state.rollbacks == rollbacks


@pytest.mark.parametrize("field", CHECKOUT_SNAPSHOT_FIELDS)
def test_returns_already_reject_client_supplied_checkout_snapshot_fields(field):
    # Executable today: ReturnCreate already forbids all extra fields.
    with pytest.raises(ValidationError):
        ReturnCreate.model_validate({"condition_status": "WORKING", field: "Forged snapshot"})


@pytest.mark.parametrize("field", CHECKOUT_SNAPSHOT_FIELDS)
def test_future_checkout_inputs_reject_snapshot_fabrication(field):
    checkout_snapshots_ready()
    with pytest.raises(ValidationError):
        CheckoutCreate.model_validate({"asset_id": str(uuid4()), "student_id": str(uuid4()),
                                      "due_at": datetime.now(timezone.utc) + timedelta(days=1), field: "Forged snapshot"})


def test_option_a_columns_preserve_exact_labels_and_explicit_legacy_unknowns():
    checkout_snapshots_ready()
    columns = EquipmentCheckout.__table__.c
    assert columns.equipment_name_at_checkout.type.length == 150 and columns.equipment_name_at_checkout.nullable
    assert columns.asset_code_at_checkout.type.length == 50 and columns.asset_code_at_checkout.nullable
    assert not columns.identifier_snapshot_status.nullable


def test_history_projection_uses_stored_snapshots_without_live_label_fallback():
    checkout_snapshots_ready()
    statement = _history_statement(datetime.now(timezone.utc))
    columns = statement.selected_columns
    table = EquipmentCheckout.__table__
    assert columns.equipment_name.base_columns == {table.c.equipment_name_at_checkout}
    assert columns.asset_code.base_columns == {table.c.asset_code_at_checkout}
    assert columns.identifier_snapshot_status.base_columns == {table.c.identifier_snapshot_status}


def test_legacy_unknown_history_is_explicit_and_never_invented_from_current_labels():
    checkout_snapshots_ready()
    now = datetime.now(timezone.utc)
    data = dict(id=uuid4(), asset_id=uuid4(), equipment_id=uuid4(), student_id=uuid4(), issued_by=uuid4(),
                equipment_name=None, asset_code=None, checked_out_at=now, due_at=now + timedelta(days=1),
                returned_at=None, returned_to=None, status="BORROWED", is_currently_overdue=False,
                was_returned_late=False, identifier_snapshot_status="LEGACY_UNKNOWN")
    result = HistoryItem.model_validate(data).model_dump(mode="json")
    assert result["equipment_name"] is None and result["asset_code"] is None
    assert result["identifier_snapshot_status"] == "LEGACY_UNKNOWN"


def test_patch_rejects_nul_and_nonnullable_null_before_persistence(contract, monkeypatch):
    kind, spec = contract
    app = registration_app(lambda: None, lambda: None)
    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid4(), role="FACULTY", is_active=True,
    )

    def unexpected_write(*args, **kwargs):
        pytest.fail("Invalid PATCH reached the persistence service")

    router_module = importlib.import_module(f"backend.app.routers.{kind}")
    monkeypatch.setattr(router_module, spec["service"], unexpected_write)
    path = f"{spec['path']}/{uuid4()}"
    invalid = [(field, "a\x00b") for field in sorted(spec["editable"] - {"purchase_price"})]
    invalid += [(field, None) for field in sorted(spec["required"])]
    for field, value in invalid:
        status, body = patch_json(app, path, {field: value})
        assert status == 422, field
        assert any(error["loc"] == ["body", field] for error in body["detail"]), field


def test_patch_preserves_valid_unicode_in_text_fields(contract, update_schema):
    _, spec = contract
    text = "प्रयोगशाला 測試 café"
    for field in sorted(spec["editable"] - {"purchase_price"}):
        payload = update_schema.model_validate({field: f"  {text}  "})
        assert payload.model_dump(exclude_unset=True) == {field: text}


def test_patch_openapi_distinguishes_omission_and_explicit_null(contract):
    kind, spec = contract
    document = registration_app(lambda: None, lambda: None).openapi()
    operation = document["paths"][f"{spec['path']}/{{{kind}_id}}"]["patch"]
    reference = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = document["components"]["schemas"][reference.rsplit("/", 1)[1]]

    assert not schema.get("required")
    assert set(schema["properties"]) == spec["editable"]
    for name, property_schema in schema["properties"].items():
        accepts_null = any(
            option.get("type") == "null"
            for option in property_schema.get("anyOf", [property_schema])
        )
        assert accepts_null == (name in spec["nullable"]), name
        if name in spec["required"]:
            assert property_schema.get("type") == "string"
            assert "default" not in property_schema
