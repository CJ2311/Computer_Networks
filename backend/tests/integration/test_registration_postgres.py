"""Registration persistence/uniqueness using the unchanged dedicated DB fixtures."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.user import User
from backend.app.schemas.equipment import EquipmentCreate
from backend.app.schemas.asset import AssetCreate
from backend.app.services import equipment as equipment_service, asset as asset_service
from backend.tests.integration.test_checkout_postgres import records
from backend.tests.registration_helpers import equipment_payload, asset_payload, registration_app, post_json


@pytest.fixture
def registration_api(database, records, monkeypatch):
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    def sessions():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            yield db
    def write_sessions():
        with Session(database, autoflush=False, expire_on_commit=False) as db:
            yield db
    app = registration_app(sessions, write_sessions)
    return lambda path, payload, header="DEV-FACULTY": post_json(app, path, payload, header)


@pytest.fixture
def equipment_id(database, records):
    with Session(database) as db:
        return db.get(EquipmentAsset, records["asset"]).equipment_id


def asset_rows(database):
    with Session(database) as db:
        return db.execute(select(EquipmentAsset.__table__).order_by(EquipmentAsset.id)).all()


@pytest.mark.parametrize("tracking", ["INDIVIDUAL", "QUANTITY"])
@pytest.mark.parametrize("header", ["DEV-ADMIN", "DEV-FACULTY", "DEV-ASSISTANT"])
def test_equipment_http_registration_persists_all_values(database, registration_api, tracking, header):
    data = equipment_payload(tracking_type=tracking, manufacturer="Maker", model_number="MODEL-1",
                             description="Network analyser", purchase_price="123.45")
    status, response = registration_api("/equipment", data, header)
    assert status == 201
    with Session(database) as db:
        equipment = db.get(Equipment, UUID(response["id"]))
        assert equipment is not None
        for key, value in data.items():
            assert getattr(equipment, key) == (Decimal(value) if key == "purchase_price" else value)
        assert equipment.created_at.utcoffset() is not None
        assert response["tracking_type"] == tracking
        assert db.scalar(select(func.count()).select_from(EquipmentAsset)) == 1


@pytest.mark.parametrize("header", ["DEV-ADMIN", "DEV-FACULTY", "DEV-ASSISTANT"])
def test_asset_http_registration_persists_identifiers_and_safe_defaults(database, equipment_id, registration_api, header):
    data = asset_payload(equipment_id, nfc_tag_id=" NFC-REGISTER ", serial_number=" SERIAL-REGISTER ")
    status, response = registration_api("/assets", data, header)
    assert status == 201
    with Session(database) as db:
        asset = db.get(EquipmentAsset, UUID(response["id"]))
        assert asset.equipment_id == equipment_id and asset.asset_code == data["asset_code"]
        assert asset.nfc_tag_id == data["nfc_tag_id"].strip() and asset.serial_number == data["serial_number"].strip()
        assert asset.location == data["location"]
        assert asset.availability_status == response["availability_status"] == "AVAILABLE"
        assert asset.condition_status == response["condition_status"] == "WORKING"
        assert asset.maintenance_hold is response["maintenance_hold"] is False


@pytest.mark.parametrize("kind", ["missing", "quantity"])
def test_asset_missing_or_quantity_equipment_rejected_without_persistence(database, registration_api, kind):
    identifier = uuid4()
    if kind == "quantity":
        status, response = registration_api("/equipment", equipment_payload(tracking_type="QUANTITY"))
        assert status == 201
        identifier = UUID(response["id"])
    before = asset_rows(database)
    status, _ = registration_api("/assets", asset_payload(identifier))
    assert status == (404 if kind == "missing" else 400)
    assert asset_rows(database) == before


@pytest.mark.parametrize("identifier", ["asset_code", "nfc_tag_id", "serial_number"])
@pytest.mark.parametrize("same_equipment", [True, False])
def test_duplicate_identifiers_follow_actual_database_scope(database, equipment_id, registration_api, identifier, same_equipment):
    first = asset_payload(equipment_id, asset_code="FIRST", nfc_tag_id="NFC-FIRST", serial_number="SERIAL-FIRST")
    assert registration_api("/assets", first)[0] == 201
    second_equipment = equipment_id
    if not same_equipment:
        status, response = registration_api("/equipment", equipment_payload(name="Other sensor"))
        assert status == 201
        second_equipment = UUID(response["id"])
    second = asset_payload(second_equipment, asset_code="SECOND", nfc_tag_id="NFC-SECOND", serial_number="SERIAL-SECOND")
    second[identifier] = first[identifier]
    before = asset_rows(database)
    status, response = registration_api("/assets", second)
    allowed = identifier == "serial_number" and not same_equipment
    assert status == (201 if allowed else 409)
    if allowed:
        assert len(asset_rows(database)) == len(before) + 1
    else:
        assert asset_rows(database) == before
        # A failed registration must not poison the next request/transaction.
        second[identifier] = "UNIQUE-AFTER-CONFLICT"
        assert registration_api("/assets", second)[0] == 201


def test_multiple_assets_may_omit_optional_unique_identifiers(database, equipment_id, registration_api):
    for number in range(2):
        assert registration_api("/assets", asset_payload(equipment_id, asset_code=f"NULL-{number}"))[0] == 201
    with Session(database) as db:
        rows = db.scalars(select(EquipmentAsset).where(EquipmentAsset.asset_code.in_(["NULL-0", "NULL-1"]))).all()
        assert len(rows) == 2 and all(row.nfc_tag_id is None and row.serial_number is None for row in rows)


@pytest.mark.parametrize("path", ["/equipment", "/assets"])
@pytest.mark.parametrize("header", [None, "DEV-UNKNOWN", "DEV-STUDENT", "DEV-FACULTY-INACTIVE"])
def test_unauthorized_registrations_leave_database_unchanged(database, equipment_id, registration_api, header, path):
    if header == "DEV-FACULTY-INACTIVE":
        with Session(database) as db:
            user = db.scalar(select(User).where(User.college_id == "DEV-FACULTY"))
            user.is_active = False
            db.commit()
        header = "DEV-FACULTY"
    with Session(database) as db:
        before_equipment = db.execute(select(Equipment.__table__).order_by(Equipment.id)).all()
    before_assets = asset_rows(database)
    status, _ = registration_api(path, equipment_payload() if path == "/equipment" else asset_payload(equipment_id), header)
    assert status == (401 if header is None or header == "DEV-UNKNOWN" else 403)
    assert asset_rows(database) == before_assets
    with Session(database) as db:
        assert db.execute(select(Equipment.__table__).order_by(Equipment.id)).all() == before_equipment


@pytest.mark.parametrize("identifier", ["nfc_tag_id", "serial_number"])
def test_overlong_optional_identifiers_are_422_before_database_write(database, equipment_id, registration_api, identifier):
    before = asset_rows(database)
    status, _ = registration_api("/assets", asset_payload(equipment_id, **{identifier: "x" * 101}))
    assert status == 422 and asset_rows(database) == before


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("stage", ["flush", "response"])
def test_registration_failure_before_commit_preserves_database_state(database, equipment_id, records, monkeypatch, kind, stage):
    before = registration_rows(database)
    with Session(database, autoflush=False) as db:
        if stage == "flush":
            flush = db.flush
            def fail_after_flush():
                flush()  # Verify rollback after real SQL INSERT.
                raise RuntimeError("Injected post-flush failure")
            monkeypatch.setattr(db, "flush", fail_after_flush)
        else:
            response_type = equipment_service.EquipmentResponse if kind == "equipment" else asset_service.AssetResponse
            monkeypatch.setattr(response_type, "model_validate", lambda value: (_ for _ in ()).throw(ValueError("Invalid response")))
        with pytest.raises((RuntimeError, ValueError)):
            register_direct(db, records, equipment_id, kind)
        assert not db.in_transaction() and not db.info
    assert registration_rows(database) == before


def registration_rows(database):
    with database.connect() as connection:
        return tuple(connection.execute(select(table).order_by(table.c.id)).all()
                     for table in (Equipment.__table__, EquipmentAsset.__table__))


def register_direct(db, records, equipment_id, kind):
    actor = User(id=records["users"]["FACULTY"])
    if kind == "equipment":
        return equipment_service.create_equipment(db, EquipmentCreate.model_validate(equipment_payload(name="COMMIT-OUTCOME")), actor)
    return asset_service.create_asset(db, AssetCreate.model_validate(asset_payload(equipment_id, asset_code="COMMIT-OUTCOME")), actor)


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_outcome_is_not_misrepresented_as_rollback(database, equipment_id, records, monkeypatch, kind, committed):
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError
    before = registration_rows(database)
    with Session(database, autoflush=False) as db:
        begin = db.begin
        def begin_with_fault():
            root = begin()
            commit = root.commit
            def fail_commit():
                if committed:
                    commit()  # Server committed; simulate losing acknowledgement.
                raise OperationalError("Sensitive SQL", {"secret": "hidden"}, Exception("Connection details"))
            monkeypatch.setattr(root, "commit", fail_commit)
            return root
        monkeypatch.setattr(db, "begin", begin_with_fault)
        with pytest.raises(HTTPException) as error:
            register_direct(db, records, equipment_id, kind)
        assert error.value.status_code == 503
        assert "could not be confirmed" in error.value.detail
        assert "before retrying" in error.value.detail
        assert all(text not in error.value.detail for text in ("Sensitive", "secret", "Connection details"))
        assert not db.in_transaction() and not db.info
    after = registration_rows(database)
    if not committed:
        assert after == before
    else:
        index = 0 if kind == "equipment" else 1
        assert len(after[index]) == len(before[index]) + 1
        assert after[1 - index] == before[1 - index]


@pytest.mark.parametrize("field, value", [("asset_code", "  "), ("location", "\t "),
                                         ("asset_code", "x" * 51), ("location", "x" * 151),
                                         ("unknown", "value"), ("id", str(uuid4())),
                                         ("availability_status", "AVAILABLE"), ("condition_status", "WORKING"),
                                         ("maintenance_hold", False)])
def test_invalid_asset_input_cannot_write_any_records(database, equipment_id, registration_api, field, value):
    before = registration_rows(database)
    status, _ = registration_api("/assets", asset_payload(equipment_id, **{field: value}))
    assert status == 422 and registration_rows(database) == before


def test_blank_optional_identifiers_become_null_and_do_not_conflict(database, equipment_id, registration_api):
    for number in range(2):
        status, _ = registration_api("/assets", asset_payload(equipment_id, asset_code=f" BLANK-{number} ",
                                                             location=" Networks lab ", nfc_tag_id="  ", serial_number="\t"))
        assert status == 201
    with Session(database) as db:
        rows = db.scalars(select(EquipmentAsset).where(EquipmentAsset.asset_code.in_(["BLANK-0", "BLANK-1"]))).all()
        assert len(rows) == 2
        assert all(row.location == "Networks lab" and row.nfc_tag_id is None and row.serial_number is None for row in rows)


@pytest.mark.parametrize("identifier", ["asset_code", "nfc_tag_id", "serial_number"])
def test_normalized_identifiers_preserve_database_unique_constraints(database, equipment_id, registration_api, identifier):
    first = asset_payload(equipment_id, asset_code="FIRST", nfc_tag_id="NFC-FIRST", serial_number="SERIAL-FIRST")
    assert registration_api("/assets", first)[0] == 201
    before = registration_rows(database)
    second = asset_payload(equipment_id, asset_code="SECOND", nfc_tag_id="NFC-SECOND", serial_number="SERIAL-SECOND")
    second[identifier] = "  " + first[identifier] + "  "
    assert registration_api("/assets", second)[0] == 409
    assert registration_rows(database) == before


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("work", ["read", "pending", "flushed", "sql", "savepoint"])
def test_registration_rejects_caller_owned_work_without_touching_it(database, equipment_id, records, kind, work):
    from fastapi import HTTPException
    before = registration_rows(database)
    with Session(database, autoflush=False) as db:
        caller = db.get(User, records["users"]["STUDENT"])
        if work in {"pending", "flushed"}:
            caller.full_name = "Caller-owned work"
            if work == "flushed":
                db.flush()
        elif work == "sql":
            import sqlalchemy as sa
            db.execute(sa.update(User).where(User.id == caller.id).values(full_name="Caller-owned work"))
        elif work == "savepoint":
            db.begin_nested()
        root, nested = db.get_transaction(), db.get_nested_transaction()
        with pytest.raises(HTTPException) as error:
            register_direct(db, records, equipment_id, kind)
        assert error.value.status_code == 409 and db.get_transaction() is root and root.is_active
        assert db.get_nested_transaction() is nested
        db.commit()  # Only the caller commits its work, after entry rejection.
    assert registration_rows(database) == before
    with Session(database) as db:
        assert db.get(User, records["users"]["STUDENT"]).full_name == (
            "Caller-owned work" if work in {"pending", "flushed", "sql"} else "Integration test")


@pytest.mark.parametrize("kind", ["equipment", "asset"])
@pytest.mark.parametrize("inactive", [False, True])
def test_registration_rechecks_persisted_staff_after_authentication_read(database, equipment_id, records, kind, inactive):
    from fastapi import HTTPException
    before = registration_rows(database)
    with Session(database, expire_on_commit=False) as authentication:
        cached_staff = authentication.get(User, records["users"]["FACULTY"])
        auth_root = authentication.get_transaction()
        with Session(database) as admin:
            persisted = admin.get(User, cached_staff.id)
            if inactive:
                persisted.is_active = False
            else:
                persisted.role = "STUDENT"
            admin.commit()
        with Session(database) as write:
            with pytest.raises(HTTPException) as error:
                if kind == "equipment":
                    equipment_service.create_equipment(write, EquipmentCreate.model_validate(equipment_payload()), cached_staff)
                else:
                    asset_service.create_asset(write, AssetCreate.model_validate(asset_payload(equipment_id)), cached_staff)
            assert error.value.status_code == 403 and not write.in_transaction()
        assert authentication.get_transaction() is auth_root and auth_root.is_active
    assert registration_rows(database) == before


@pytest.mark.parametrize("kind", ["equipment", "asset"])
def test_unrelated_work_added_after_registration_flush_is_never_committed(database, equipment_id, records, monkeypatch, kind):
    from fastapi import HTTPException
    before = registration_rows(database)
    with Session(database, autoflush=False) as db:
        flush = db.flush
        def inject():
            flush()
            db.add(User(id=uuid4(), college_id="UNRELATED-REGISTRATION", full_name="Must not persist",
                        email="unrelated-registration@example.invalid", role="STUDENT", is_active=True))
        monkeypatch.setattr(db, "flush", inject)
        with pytest.raises(HTTPException) as error:
            register_direct(db, records, equipment_id, kind)
        assert error.value.status_code == 409 and not db.in_transaction()
    assert registration_rows(database) == before
    with Session(database) as db:
        assert db.scalar(select(User.id).where(User.college_id == "UNRELATED-REGISTRATION")) is None


def test_all_asset_text_limits_match_database_after_normalization(database, equipment_id, registration_api):
    data = asset_payload(equipment_id, asset_code=" " + "A" * 50 + " ", location=" " + "L" * 150 + " ",
                         nfc_tag_id=" " + "N" * 100 + " ", serial_number=" " + "S" * 100 + " ")
    status, response = registration_api("/assets", data)
    assert status == 201
    with Session(database) as db:
        asset = db.get(EquipmentAsset, UUID(response["id"]))
        assert asset.asset_code == "A" * 50 and asset.location == "L" * 150
        assert asset.nfc_tag_id == "N" * 100 and asset.serial_number == "S" * 100
