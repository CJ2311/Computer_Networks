"""Equipment/asset registration validation, authorization and transaction regressions."""
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.app.database import engine
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.equipment import EquipmentCreate, EquipmentResponse
from backend.app.schemas.asset import AssetCreate, AssetResponse
from backend.app.services.equipment import create_equipment
from backend.app.services.asset import create_asset
from backend.tests.inventory_session_stub import configure_session
from backend.tests.registration_helpers import equipment_payload, asset_payload, registration_app, post_json


@pytest.fixture(autouse=True)
def block_application_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Application database connections forbidden in registration unit tests")
    monkeypatch.setattr(engine, "connect", forbidden)
    monkeypatch.setattr(engine, "raw_connection", forbidden)


@pytest.mark.parametrize("tracking", ["INDIVIDUAL", "QUANTITY"])
def test_equipment_minimal_valid_payload_and_defaults(tracking):
    data = EquipmentCreate.model_validate(equipment_payload(tracking_type=tracking))
    assert data.tracking_type == tracking
    assert all(getattr(data, field) is None for field in ("manufacturer", "model_number", "description", "purchase_price"))


@pytest.mark.parametrize("field", ["name", "category", "tracking_type", "default_location"])
@pytest.mark.parametrize("value", ["missing", None])
def test_equipment_required_fields(field, value):
    data = equipment_payload()
    if value == "missing":
        data.pop(field)
    else:
        data[field] = value
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(data)


@pytest.mark.parametrize("field, maximum", [("name", 150), ("category", 100), ("default_location", 150),
                                           ("manufacturer", 100), ("model_number", 100)])
def test_equipment_text_length_boundaries(field, maximum):
    assert getattr(EquipmentCreate.model_validate(equipment_payload(**{field: "x" * maximum})), field) == "x" * maximum
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(equipment_payload(**{field: "x" * (maximum + 1)}))


@pytest.mark.parametrize("field", ["name", "category", "default_location"])
@pytest.mark.parametrize("value", ["", "  ", "\t\n", 123, [], {}])
def test_equipment_invalid_required_text(field, value):
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(equipment_payload(**{field: value}))


@pytest.mark.parametrize("tracking", ["individual", "quantity", "OTHER", "", 1, [], {}])
def test_invalid_equipment_tracking_type(tracking):
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(equipment_payload(tracking_type=tracking))


@pytest.mark.parametrize("price", ["0", "0.01", "9999999999.99", None])
def test_valid_purchase_price(price):
    data = EquipmentCreate.model_validate(equipment_payload(purchase_price=price))
    assert data.purchase_price == (Decimal(price) if price is not None else None)


@pytest.mark.parametrize("price", ["-0.01", "1.001", "10000000000.00", "NaN", "Infinity", "invalid"])
def test_invalid_purchase_price(price):
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(equipment_payload(purchase_price=price))


def test_equipment_normalizes_text_and_rejects_extra_fields():
    data = EquipmentCreate.model_validate(equipment_payload(name=" Sensor ", manufacturer="  ", model_number=" ", description=" Description "))
    assert data.name == "Sensor" and data.manufacturer is None and data.model_number is None
    assert data.description == "Description"
    with pytest.raises(ValidationError):
        EquipmentCreate.model_validate(equipment_payload(id=str(uuid4())))


@pytest.mark.parametrize("field", ["equipment_id", "asset_code", "location"])
@pytest.mark.parametrize("value", ["missing", None])
def test_asset_required_fields(field, value):
    data = asset_payload()
    if value == "missing":
        data.pop(field)
    else:
        data[field] = value
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(data)


@pytest.mark.parametrize("identifier", ["invalid", "", 123, [], {}])
def test_asset_requires_equipment_uuid(identifier):
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(asset_payload(equipment_id=identifier))


@pytest.mark.parametrize("field, maximum", [("asset_code", 50), ("location", 150)])
def test_asset_required_text_boundaries(field, maximum):
    assert getattr(AssetCreate.model_validate(asset_payload(**{field: "x" * maximum})), field) == "x" * maximum
    for invalid in ("", "x" * (maximum + 1), 12):
        with pytest.raises(ValidationError):
            AssetCreate.model_validate(asset_payload(**{field: invalid}))


@pytest.mark.parametrize("field", ["asset_code", "location"])
def test_asset_whitespace_only_required_values_are_rejected(field):
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(asset_payload(**{field: "   "}))


@pytest.mark.parametrize("field", ["nfc_tag_id", "serial_number"])
@pytest.mark.parametrize("value", [None, "", "   ", "x" * 101])
def test_optional_identifiers_normalize_blanks_and_enforce_column_lengths(field, value):
    if value is not None and len(value) > 100:
        with pytest.raises(ValidationError):
            AssetCreate.model_validate(asset_payload(**{field: value}))
    else:
        data = AssetCreate.model_validate(asset_payload(**{field: value}))
        assert getattr(data, field) is None


@pytest.mark.parametrize("field", ["nfc_tag_id", "serial_number"])
def test_optional_identifiers_reject_non_strings(field):
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(asset_payload(**{field: 123}))


@pytest.mark.parametrize("field, value", [("availability_status", "AVAILABLE"), ("availability_status", "BORROWED"),
                                         ("availability_status", "INVALID"), ("condition_status", "DAMAGED"),
                                         ("condition_status", "INVALID"), ("maintenance_hold", True), ("id", str(uuid4()))])
def test_asset_state_and_identity_overrides_never_enter_service_payload(field, value):
    with pytest.raises(ValidationError) as error:
        AssetCreate.model_validate(asset_payload(**{field: value}))
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.fixture
def case(monkeypatch):
    c = SimpleNamespace(db=Mock(spec=Session), now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    c.equipment = Equipment(id=uuid4(), tracking_type="INDIVIDUAL")
    c.staff = User(id=uuid4(), role="FACULTY", is_active=True)
    c.persisted_staff = c.staff
    c.db.new, c.db.dirty, c.db.deleted, c.db.info = set(), set(), set(), {}
    c.db.no_autoflush = nullcontext()
    c.db.in_nested_transaction.return_value = False
    configure_session(c.db, monkeypatch)
    c.db.get.side_effect = lambda model, identifier, **kw: c.persisted_staff if model is User else c.equipment
    c.db.scalar.return_value = c.staff
    c.inserted = []
    c.db.add.side_effect = c.inserted.append
    def flush():
        for obj in c.inserted:
            obj.id = uuid4()
            if isinstance(obj, Equipment):
                obj.created_at = c.now
            else:
                # Unit emulation only; PostgreSQL tests verify actual defaults.
                obj.availability_status, obj.condition_status, obj.maintenance_hold = "AVAILABLE", "WORKING", False
    c.db.flush.side_effect = flush
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    def sessions():
        yield c.db
    c.app = registration_app(sessions, sessions)
    return c


@pytest.mark.parametrize("tracking", ["INDIVIDUAL", "QUANTITY"])
def test_equipment_service_preserves_validated_values_and_transaction(case, tracking):
    payload = EquipmentCreate.model_validate(equipment_payload(tracking_type=tracking, purchase_price="123.45", manufacturer="Maker"))
    result = create_equipment(case.db, actor=case.staff, equipment_data=payload)
    for field, value in payload.model_dump().items():
        assert getattr(result, field) == value
    assert EquipmentResponse.model_validate(result).purchase_price == Decimal("123.45")
    assert [call[0] for call in case.db.method_calls if call[0] in {"add", "flush", "commit", "refresh"}] == ["add", "flush", "commit"]
    case.db.refresh.assert_not_called()
    case.db.rollback.assert_not_called()


@pytest.mark.parametrize("stage", ["add", "flush", "commit", "response"])
def test_equipment_service_rolls_back_and_reraises_errors(case, monkeypatch, stage):
    if stage == "response":
        monkeypatch.setattr(EquipmentResponse, "model_validate", Mock(side_effect=RuntimeError("Response failure")))
    else:
        getattr(case.db, stage).side_effect = RuntimeError("Injected registration failure")
    with pytest.raises(RuntimeError):
        create_equipment(case.db, actor=case.staff, equipment_data=EquipmentCreate.model_validate(equipment_payload()))
    case.db.rollback.assert_called_once()


def test_asset_service_uses_existing_individual_equipment_and_safe_payload(case):
    data = AssetCreate.model_validate(asset_payload(case.equipment.id, nfc_tag_id="NFC-1", serial_number="SERIAL-1"))
    result = create_asset(case.db, actor=case.staff, asset_data=data)
    case.db.get.assert_any_call(Equipment, case.equipment.id, populate_existing=True)
    case.db.get.assert_any_call(User, case.staff.id, populate_existing=True)
    assert result.equipment_id == case.equipment.id and result.nfc_tag_id == "NFC-1" and result.serial_number == "SERIAL-1"
    assert AssetResponse.model_validate(result).maintenance_hold is False
    assert [call[0] for call in case.db.method_calls if call[0] in {"add", "flush", "commit", "refresh"}] == ["add", "flush", "commit"]
    case.db.refresh.assert_not_called()


@pytest.mark.parametrize("tracking", [None, "QUANTITY", "INVALID"])
def test_asset_service_rejects_missing_or_nonindividual_equipment(case, tracking):
    case.equipment = None if tracking is None else Equipment(tracking_type=tracking)
    with pytest.raises(HTTPException) as error:
        create_asset(case.db, actor=case.staff, asset_data=AssetCreate.model_validate(asset_payload()))
    assert error.value.status_code == (404 if tracking is None else 400)
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


@pytest.mark.parametrize("identifier", ["asset_code", "nfc_tag_id", "serial_number"])
def test_asset_duplicate_integrity_error_becomes_conflict_and_rolls_back(case, identifier):
    # This tests error translation, not PostgreSQL uniqueness enforcement.
    case.db.flush.side_effect = IntegrityError("insert", {identifier: "duplicate"}, Exception("constraint"))
    with pytest.raises(HTTPException) as error:
        create_asset(case.db, actor=case.staff, asset_data=AssetCreate.model_validate(asset_payload()))
    assert error.value.status_code == 409
    case.db.rollback.assert_called_once()
    case.db.refresh.assert_not_called()


@pytest.mark.parametrize("stage", ["add", "flush", "commit", "response"])
def test_asset_unexpected_failures_roll_back(case, monkeypatch, stage):
    if stage == "response":
        monkeypatch.setattr(AssetResponse, "model_validate", Mock(side_effect=RuntimeError("Response failure")))
    else:
        getattr(case.db, stage).side_effect = RuntimeError("Injected registration failure")
    with pytest.raises(RuntimeError):
        create_asset(case.db, actor=case.staff, asset_data=AssetCreate.model_validate(asset_payload()))
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("path", ["/equipment", "/assets"])
@pytest.mark.parametrize("role, header", [("ADMIN", "DEV-ADMIN"), ("FACULTY", "DEV-FACULTY"), ("LAB_ASSISTANT", "DEV-ASSISTANT")])
def test_registration_http_all_authorized_roles(case, path, role, header):
    case.staff.role = role
    payload = equipment_payload() if path == "/equipment" else asset_payload(case.equipment.id)
    status, response = post_json(case.app, path, payload, header)
    assert status == 201 and UUID(response["id"])
    if path == "/equipment":
        assert response["tracking_type"] == "INDIVIDUAL" and response["created_at"].endswith("Z")
    else:
        assert response["availability_status"] == "AVAILABLE" and response["condition_status"] == "WORKING"
        assert response["maintenance_hold"] is False
    case.db.commit.assert_called_once()


@pytest.mark.parametrize("path", ["/equipment", "/assets"])
@pytest.mark.parametrize("header, role, active, client, enabled, expected", [
    (None, "FACULTY", True, "127.0.0.1", True, 401),
    ("DEV-UNKNOWN", "FACULTY", True, "127.0.0.1", True, 401),
    ("DEV-STUDENT", "STUDENT", True, "127.0.0.1", True, 403),
    ("DEV-FACULTY", "FACULTY", False, "127.0.0.1", True, 403),
    ("DEV-ADMIN", "ADMIN", False, "127.0.0.1", True, 403),
    ("DEV-ASSISTANT", "LAB_ASSISTANT", False, "127.0.0.1", True, 403),
    ("DEV-FACULTY", "FACULTY", True, "192.0.2.1", True, 403),
    ("DEV-FACULTY", "FACULTY", True, "127.0.0.1", False, 403),
])
def test_registration_http_denies_unauthorized_requests_without_writes(case, monkeypatch, path, header, role, active, client, enabled, expected):
    case.staff.role, case.staff.is_active = role, active
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: enabled)
    data = equipment_payload() if path == "/equipment" else asset_payload()
    assert post_json(case.app, path, data, header, client)[0] == expected
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


@pytest.mark.parametrize("path, payload", [("/equipment", {}), ("/equipment", equipment_payload(tracking_type="BAD")),
                                           ("/equipment", equipment_payload(purchase_price="-1")),
                                           ("/assets", {}), ("/assets", asset_payload(asset_code="")),
                                           ("/assets", asset_payload(equipment_id="invalid"))])
def test_http_invalid_payloads_are_422_without_writes(case, path, payload):
    assert post_json(case.app, path, payload)[0] == 422
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


def test_registration_schema_matches_identifier_uniqueness_scope():
    table = EquipmentAsset.__table__
    unique = {tuple(c.name for c in constraint.columns) for constraint in table.constraints if isinstance(constraint, UniqueConstraint)}
    assert ("asset_code",) in unique and ("nfc_tag_id",) in unique
    assert ("equipment_id", "serial_number") in unique
    assert ("serial_number",) not in unique
    assert table.c.nfc_tag_id.nullable and table.c.serial_number.nullable


def test_asset_http_rejects_all_client_supplied_state_and_identity_fields(case):
    supplied_id = str(uuid4())
    data = asset_payload(case.equipment.id, id=supplied_id, availability_status="RETIRED",
                         condition_status="DAMAGED", maintenance_hold=True)
    status, response = post_json(case.app, "/assets", data)
    assert status == 422
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


@pytest.mark.parametrize("path", ["/equipment", "/assets"])
def test_authentication_requires_an_existing_user_record(case, path):
    case.db.scalar.return_value = None
    data = equipment_payload() if path == "/equipment" else asset_payload(case.equipment.id)
    assert post_json(case.app, path, data)[0] == 401
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


@pytest.mark.parametrize("path", ["/equipment", "/assets"])
def test_admin_header_does_not_override_persisted_student_role(case, path):
    case.staff.role = "STUDENT"
    data = equipment_payload() if path == "/equipment" else asset_payload(case.equipment.id)
    assert post_json(case.app, path, data, "DEV-ADMIN")[0] == 403
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


@pytest.mark.parametrize("problem, expected", [("missing", 404), ("quantity", 400), ("duplicate", 409)])
def test_asset_http_preserves_service_error_codes(case, problem, expected):
    identifier = case.equipment.id
    if problem == "missing":
        case.equipment = None
    elif problem == "quantity":
        case.equipment.tracking_type = "QUANTITY"
    else:
        case.db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    assert post_json(case.app, "/assets", asset_payload(identifier))[0] == expected
    if problem == "duplicate":
        case.db.rollback.assert_called_once()
    else:
        case.db.commit.assert_not_called()


@pytest.mark.parametrize("field", ["nfc_tag_id", "serial_number"])
def test_optional_identifier_boundaries_strip_edges_and_preserve_case(field):
    value = "Case-Sensitive " + "x" * 85
    assert len(value) == 100
    data = AssetCreate.model_validate(asset_payload(**{field: "  " + value + "  "}))
    assert getattr(data, field) == value
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(asset_payload(**{field: value + "x"}))


def test_required_asset_text_is_trimmed_without_changing_identifier_case():
    data = AssetCreate.model_validate(asset_payload(asset_code=" Lab-Asset-01 ", location=" Room 101 "))
    assert data.asset_code == "Lab-Asset-01" and data.location == "Room 101"


@pytest.mark.parametrize("field, value", [("unknown", "x"), ("id", str(uuid4())), ("created_at", "2026-01-01T00:00:00Z"),
                                         ("availability_status", "AVAILABLE"), ("condition_status", "WORKING"),
                                         ("maintenance_hold", False), ("nfc_tag_id", "x" * 101),
                                         ("serial_number", "x" * 101), ("asset_code", "  "), ("location", "\t")])
def test_asset_http_invalid_or_server_controlled_fields_are_422_without_writes(case, field, value):
    assert post_json(case.app, "/assets", asset_payload(case.equipment.id, **{field: value}))[0] == 422
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()


def call_registration(case, kind):
    if kind == "equipment":
        return create_equipment(case.db, EquipmentCreate.model_validate(equipment_payload()), case.staff)
    return create_asset(case.db, AssetCreate.model_validate(asset_payload(case.equipment.id)), case.staff)


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("role, active", [("STUDENT", True), ("FACULTY", False), ("UNKNOWN", True)])
def test_service_rechecks_persisted_authorization_in_write_session(case, kind, role, active):
    case.persisted_staff = User(id=case.staff.id, role=role, is_active=active)
    case.staff.role, case.staff.is_active = "ADMIN", True  # Forged/stale caller values.
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 403
    case.db.get.assert_called_once_with(User, case.staff.id, populate_existing=True)
    case.db.add.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("kind", ["equipment", "asset"])
def test_response_is_validated_before_commit_and_no_refresh_follows(case, monkeypatch, kind):
    response_type = EquipmentResponse if kind == "equipment" else AssetResponse
    validate = Mock(wraps=response_type.model_validate)
    monkeypatch.setattr(response_type, "model_validate", validate)
    def commit():
        validate.assert_called_once()
    case.db.commit.side_effect = commit
    response = call_registration(case, kind)
    assert isinstance(response, response_type)
    case.db.commit.assert_called_once()
    case.db.refresh.assert_not_called()


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("stage", ["flush", "commit"])
def test_unexpected_dbapi_errors_are_sanitized_and_rollback_is_attempted(case, kind, stage):
    getattr(case.db, stage).side_effect = OperationalError("Secret SQL", {"password": "secret"}, Exception("Host and credentials"))
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 503
    assert all(word not in error.value.detail for word in ("Secret SQL", "password", "Host", "credentials"))
    if stage == "commit":
        assert "could not be confirmed" in error.value.detail and "before retrying" in error.value.detail
    case.db.rollback.assert_called_once()
    case.db.refresh.assert_not_called()


@pytest.mark.parametrize("kind", ["equipment", "asset"])
def test_lost_commit_acknowledgement_does_not_claim_guaranteed_rollback(case, kind):
    def lost_ack():
        # Model an already completed commit; rollback must not adopt another root.
        root = case.db.get_transaction()
        root.is_active = False
        case.db.get_transaction.return_value = None
        case.db.in_transaction.return_value = False
        raise OperationalError("COMMIT", {}, Exception("Lost acknowledgement"))
    case.db.commit.side_effect = lost_ack
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 503 and "could not be confirmed" in error.value.detail
    case.db.rollback.assert_not_called()
    assert not case.db.info


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("state", ["root", "new", "dirty", "deleted", "nested", "failed"])
def test_registration_entry_rejection_does_not_discard_caller_work(case, kind, state):
    if state == "root":
        case.db.in_transaction.return_value = True
    elif state == "nested":
        case.db.in_nested_transaction.return_value = True
    elif state == "failed":
        case.db.is_active = False
    else:
        setattr(case.db, state, {object()})
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 409
    case.db.add.assert_not_called()
    case.db.begin.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_not_called()


@pytest.mark.parametrize("kind", ["equipment", "asset"])
def test_unrelated_objects_introduced_during_persistence_prevent_commit(case, kind):
    flush = case.db.flush.side_effect
    def introduce_unrelated():
        flush()
        case.db.new = {object()}
    case.db.flush.side_effect = introduce_unrelated
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 409
    case.db.flush.assert_called_once()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_called_once()


@pytest.mark.parametrize("kind", ["equipment", "asset"])
def test_commit_and_rollback_failure_still_report_uncertain_registration(case, kind):
    case.db.commit.side_effect = OperationalError("Sensitive COMMIT", {}, Exception("Credentials"))
    case.db.rollback.side_effect = RuntimeError("Cleanup failed")
    with pytest.raises(HTTPException) as error:
        call_registration(case, kind)
    assert error.value.status_code == 503
    assert "could not be confirmed" in error.value.detail
    assert "Sensitive" not in error.value.detail and "Credentials" not in error.value.detail
    assert not case.db.info
