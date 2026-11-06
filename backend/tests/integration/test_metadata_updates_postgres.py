"""Metadata update contract on the isolated PostgreSQL harness."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import time
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset, EquipmentStock
from backend.app.models.maintenance import MaintenanceRequest, MaintenanceEvent
from backend.app.models.user import User
from backend.app.services.checkout import create_checkout, record_return
from backend.app.services.maintenance import report_maintenance_issue
from backend.app.schemas.checkout import ReturnCreate
from backend.app.schemas.borrowing_history import HistoryFilters
from backend.app.services.borrowing_history import get_borrowing_history, list_borrowing_history
from backend.tests.integration.test_checkout_postgres import records
from backend.tests.integration.test_checkout_snapshots_postgres import revision as snapshot_revision
from backend.tests.integration.test_metadata_audit_postgres import create_lookalike_schema
from backend.tests.integration.inventory_helpers import database_now, label_transaction
from backend.tests.registration_helpers import registration_app
from backend.tests.update_contract_helpers import (
    CONTRACTS, runtime_for, patch_json, CHECKOUT_SNAPSHOT_FIELDS,
    AUDIT_ACTOR_SETTING, AUDIT_WRITER_ROLE, AUDIT_OWNER_ROLE, AUDIT_GENERATORS,
)


pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SADeprecationWarning")

@pytest.fixture(params=["equipment", "asset"])
def target(request, database, records):
    kind = request.param
    schema, operation, audit = runtime_for(kind)
    assert set(CHECKOUT_SNAPSHOT_FIELDS) <= set(EquipmentCheckout.__table__.c.keys()), (
        "Option A snapshots are required before exposing mutable names/codes")
    with database.connect() as connection:
        assert set(CHECKOUT_SNAPSHOT_FIELDS) <= {c["name"] for c in sa.inspect(connection).get_columns("equipment_checkouts")}
        assert sa.inspect(connection).has_table(audit.__tablename__), (
            "Approved audit migration must be added to the isolated revision fixture; never run env.py")
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        identifier = asset.equipment_id if kind == "equipment" else asset.id
        original = db.get(CONTRACTS[kind]["model"], identifier)
        values = {column.name: getattr(original, column.name) for column in original.__table__.columns}
    return dict(kind=kind, schema=schema, operation=operation, audit=audit, id=identifier,
                contract=CONTRACTS[kind], original=values)


def call(database, records, target, payload, actor="FACULTY", db=None):
    kwargs = {f"{target['kind']}_id": target["id"], "update_data": target["schema"].model_validate(payload),
              "actor": User(id=records["users"][actor])}
    if db is not None:
        return target["operation"](db, **kwargs)
    with Session(database, autoflush=False, expire_on_commit=False) as session:
        return target["operation"](session, **kwargs)


def audits(database, target):
    model = target["audit"]
    with Session(database) as db:
        rows = db.scalars(sa.select(model).where(model.entity_type == target["kind"].upper(),
                          model.entity_id == target["id"]).order_by(model.entity_revision)).all()
        return [{c.name: getattr(row, c.name) for c in model.__table__.columns} for row in rows]


def snapshot(database, target):
    with database.connect() as connection:
        return tuple(connection.execute(sa.select(table).order_by(*table.primary_key.columns)).all() for table in (
            Equipment.__table__, EquipmentAsset.__table__, EquipmentStock.__table__, EquipmentCheckout.__table__,
            MaintenanceRequest.__table__, MaintenanceEvent.__table__, target["audit"].__table__,
        ))


def field_for(target):
    return "name" if target["kind"] == "equipment" else "asset_code"


def conflict(action, status=409):
    with pytest.raises(HTTPException) as error:
        action()
    assert error.value.status_code == status
    return error.value


def borrow(database, records):
    due = database_now(database) + timedelta(days=1)
    with Session(database, autoflush=False) as db:
        return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                               User(id=records["users"]["FACULTY"]), due)


@pytest.fixture
def api(database, monkeypatch, target):
    monkeypatch.setattr("backend.app.auth.dependencies.is_dev_auth_enabled", lambda: True)
    def reads():
        with Session(database, autoflush=False) as db:
            yield db
    def writes():
        with Session(database, autoflush=False) as db:
            yield db
    app = registration_app(reads, writes)
    path = target["contract"]["path"] + "/{equipment_id}" if target["kind"] == "equipment" else "/assets/{asset_id}"
    assert "patch" in app.openapi().get("paths", {}).get(path, {})
    return lambda payload, header="DEV-FACULTY", identifier=None: patch_json(
        app, f"{target['contract']['path']}/{identifier or target['id']}", payload, header)


@pytest.mark.parametrize("actor", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
def test_successful_update_changes_only_supplied_metadata_and_creates_atomic_audit(database, records, target, actor):
    field = field_for(target)
    before = snapshot(database, target)
    started = database_now(database)
    response = call(database, records, target, {field: " Updated metadata "}, actor)
    with Session(database) as db:
        row = db.get(target["contract"]["model"], target["id"])
        assert getattr(row, field) == getattr(response, field) == "Updated metadata"
        for name, original in target["original"].items():
            if name != field:
                assert getattr(row, name) == original
    event, = audits(database, target)
    assert isinstance(event["id"], UUID) and event["actor_id"] == records["users"][actor]
    assert event["entity_type"] == target["kind"].upper() and event["entity_id"] == target["id"]
    assert event["action"] == "UPDATE" and event["entity_revision"] == 1
    assert event["previous_values"] == {field: target["original"][field]}
    assert event["new_values"] == {field: "Updated metadata"}
    assert event["occurred_at"].utcoffset() is not None and started <= event["occurred_at"] <= database_now(database)
    after = snapshot(database, target)
    # Stock, borrowings, and maintenance history must never change.
    assert after[2:6] == before[2:6]
    assert after[1 if target["kind"] == "equipment" else 0] == before[1 if target["kind"] == "equipment" else 0]


def test_normalized_noop_does_not_emit_update_or_audit(database, records, target):
    before = snapshot(database, target)
    writes = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            writes.append(statement)
    sa.event.listen(database, "before_cursor_execute", capture)
    try:
        field = field_for(target)
        response = call(database, records, target, {field: " " + target["original"][field] + " "})
        assert getattr(response, field) == target["original"][field]
    finally:
        sa.event.remove(database, "before_cursor_execute", capture)
    assert not writes and snapshot(database, target) == before and audits(database, target) == []


def test_explicit_null_clears_nullable_field_and_omission_preserves_siblings(database, records, target):
    field = "manufacturer" if target["kind"] == "equipment" else "nfc_tag_id"
    call(database, records, target, {field: "Original optional value"})
    call(database, records, target, {field: None})
    with Session(database) as db:
        row = db.get(target["contract"]["model"], target["id"])
        assert getattr(row, field) is None
        for name, value in target["original"].items():
            if name != field:
                assert getattr(row, name) == value
    events = audits(database, target)
    assert len(events) == 2 and events[-1]["previous_values"] == {field: "Original optional value"}
    assert events[-1]["new_values"] == {field: None}


@pytest.mark.parametrize("target", ["equipment"], indirect=True)
def test_equipment_default_location_does_not_relocate_existing_assets(database, records, target):
    before = snapshot(database, target)
    call(database, records, target, {"default_location": "New default room"})
    assert snapshot(database, target)[1:6] == before[1:6]


@pytest.mark.parametrize("header, expected", [(None, 401), ("DEV-UNKNOWN", 401), ("DEV-STUDENT", 403)])
def test_patch_authorization_denials_are_read_only(database, target, api, header, expected):
    before = snapshot(database, target)
    assert api({field_for(target): "Denied"}, header)[0] == expected
    assert snapshot(database, target) == before


@pytest.mark.parametrize("inactive", [False, True])
def test_persisted_role_activation_rechecked_despite_forged_caller(database, records, target, inactive):
    with Session(database) as db:
        staff = db.get(User, records["users"]["FACULTY"])
        staff.is_active = not inactive
        if not inactive:
            staff.role = "STUDENT"
        db.commit()
    before = snapshot(database, target)
    conflict(lambda: call(database, records, target, {field_for(target): "Denied"}), 403)
    assert snapshot(database, target) == before


def test_invalid_empty_unknown_and_immutable_payloads_are_422(database, target, api):
    before = snapshot(database, target)
    payloads = [{}, {field_for(target): None}, {field_for(target): "  "}, {"unknown": 1}]
    payloads += [{field: str(uuid4())} for field in target["contract"]["immutable"]]
    for payload in payloads:
        assert api(payload)[0] == 422
    assert snapshot(database, target) == before


def test_missing_entity_is_404_without_audit(database, target, api):
    before = snapshot(database, target)
    assert api({field_for(target): "Updated"}, identifier=uuid4())[0] == 404
    assert snapshot(database, target) == before


@pytest.mark.parametrize("identifier", ["asset_code", "nfc_tag_id", "serial_number"])
@pytest.mark.parametrize("same_equipment", [True, False])
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_identifier_uniqueness_retains_database_scope(database, records, target, identifier, same_equipment):
    with Session(database) as db:
        equipment_id = target["original"]["equipment_id"]
        if not same_equipment:
            equipment = Equipment(id=uuid4(), name="Other equipment", category="IoT", tracking_type="INDIVIDUAL", default_location="Test")
            db.add(equipment)
            db.flush()
            equipment_id = equipment.id
        other = EquipmentAsset(id=uuid4(), equipment_id=equipment_id, asset_code="DUP-CODE", nfc_tag_id="DUP-NFC",
                               serial_number="DUP-SERIAL", location="Test")
        db.add(other)
        db.commit()
        duplicate = getattr(other, identifier)
    before = snapshot(database, target)
    payload = {identifier: " " + duplicate + " "}
    if identifier == "serial_number" and not same_equipment:
        call(database, records, target, payload)
        assert len(audits(database, target)) == 1
    else:
        conflict(lambda: call(database, records, target, payload))
        assert snapshot(database, target) == before


@pytest.mark.parametrize("state", ["AVAILABLE", "BORROWED", "RESERVED", "MAINTENANCE", "RETIRED", "DAMAGED", "UNDER_REPAIR", "MISSING"])
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_location_requires_available_working_unheld_asset(database, records, target, state):
    if state == "BORROWED":
        borrow(database, records)
    elif state == "MAINTENANCE":
        with Session(database) as db:
            report_maintenance_issue(db, records["asset"], User(id=records["users"]["FACULTY"]), "DAMAGE", "Existing fault")
    elif state != "AVAILABLE":
        with Session(database) as db:
            asset = db.get(EquipmentAsset, records["asset"])
            asset.availability_status = state if state in {"RESERVED", "RETIRED"} else "RESERVED"
            asset.condition_status = state if state in {"DAMAGED", "UNDER_REPAIR", "MISSING"} else "WORKING"
            db.commit()
    before = snapshot(database, target)
    if state == "AVAILABLE":
        call(database, records, target, {"location": "New room"})
        assert len(audits(database, target)) == 1
    else:
        conflict(lambda: call(database, records, target, {"location": "New room"}))
        # Providing the same location must not bypass state eligibility either.
        conflict(lambda: call(database, records, target, {"location": target["original"]["location"]}))
        assert snapshot(database, target) == before


@pytest.mark.parametrize("custody", ["borrowed", "returned", "maintenance"])
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_metadata_corrections_do_not_rewrite_borrowing_or_maintenance_history(database, records, target, custody):
    if custody in {"borrowed", "returned"}:
        loan = borrow(database, records)
        if custody == "returned":
            with Session(database) as db:
                record_return(db, loan.id, User(id=records["users"]["FACULTY"]), ReturnCreate(condition_status="WORKING"))
    else:
        with Session(database) as db:
            report_maintenance_issue(db, records["asset"], User(id=records["users"]["FACULTY"]), "DAMAGE", "Fault")
    before = snapshot(database, target)
    response = call(database, records, target, {"asset_code": "Corrected metadata"})
    after = snapshot(database, target)
    assert after[2:6] == before[2:6]
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        assert response.availability_status == asset.availability_status
        assert response.condition_status == asset.condition_status
        assert response.maintenance_hold == asset.maintenance_hold


@pytest.mark.parametrize("stage", ["flush", "response", "audit"])
def test_update_failures_roll_back_entity_and_audit_together(database, records, target, monkeypatch, stage):
    before = snapshot(database, target)
    with Session(database, autoflush=False) as db:
        if stage == "flush":
            flush = db.flush
            def fail_after_sql():
                flush()
                raise RuntimeError("Injected post-UPDATE failure")
            monkeypatch.setattr(db, "flush", fail_after_sql)
        elif stage == "response":
            module = __import__(f"backend.app.services.{target['kind']}", fromlist=["unused"])
            response_type = getattr(module, "EquipmentResponse" if target["kind"] == "equipment" else "AssetResponse")
            monkeypatch.setattr(response_type, "model_validate", lambda value: (_ for _ in ()).throw(ValueError("Invalid response")))
        else:
            # A server-side audit failure must abort its triggering UPDATE.
            with database.begin() as connection:
                connection.execute(sa.text("CREATE FUNCTION test_fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                                           "BEGIN RAISE EXCEPTION 'Injected audit failure' USING ERRCODE = '23514'; END; $$"))
                table = connection.dialect.identifier_preparer.quote(target["audit"].__tablename__)
                connection.execute(sa.text(f"CREATE TRIGGER test_audit_failure BEFORE INSERT ON {table} "
                                           "FOR EACH ROW EXECUTE FUNCTION test_fail_audit()"))
        with pytest.raises((HTTPException, RuntimeError, ValueError)):
            call(database, records, target, {field_for(target): "Must roll back"}, db=db)
        assert not db.in_transaction() and not db.info
    assert snapshot(database, target) == before


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_does_not_claim_that_entity_and_audit_were_rolled_back(database, records, target, monkeypatch, committed):
    before = snapshot(database, target)
    with Session(database, autoflush=False) as db:
        begin = db.begin
        def faulty_begin():
            root = begin()
            commit = root.commit
            def lost_acknowledgement():
                if committed:
                    commit()
                raise sa.exc.OperationalError("Sensitive SQL", {}, Exception("Connection details"))
            monkeypatch.setattr(root, "commit", lost_acknowledgement)
            return root
        monkeypatch.setattr(db, "begin", faulty_begin)
        error = conflict(lambda: call(database, records, target, {field_for(target): "Possibly committed"}, db=db), 503)
        assert "could not be confirmed" in error.detail and "before retrying" in error.detail
        assert "Sensitive SQL" not in error.detail and "Connection details" not in error.detail
    if committed:
        assert len(audits(database, target)) == 1
        with Session(database) as observer:
            assert getattr(observer.get(target["contract"]["model"], target["id"]), field_for(target)) == "Possibly committed"
    else:
        assert snapshot(database, target) == before


@pytest.mark.parametrize("work", ["pending", "flushed", "sql", "savepoint"])
def test_owned_transaction_entry_rejects_caller_work_without_losing_it(database, records, target, work):
    before = snapshot(database, target)
    with Session(database, autoflush=False) as db:
        caller = db.get(User, records["users"]["STUDENT"])
        if work in {"pending", "flushed"}:
            caller.full_name = "Caller change"
            if work == "flushed":
                db.flush()
        elif work == "sql":
            db.execute(sa.update(User).where(User.id == caller.id).values(full_name="Caller change"))
        else:
            db.begin_nested()
        root, nested = db.get_transaction(), db.get_nested_transaction()
        conflict(lambda: call(database, records, target, {field_for(target): "Denied"}, db=db))
        assert db.get_transaction() is root and root.is_active and db.get_nested_transaction() is nested
        db.commit()
    assert snapshot(database, target) == before
    if work != "savepoint":
        with Session(database) as db:
            assert db.get(User, records["users"]["STUDENT"]).full_name == "Caller change"


def test_target_is_refreshed_and_locked_before_metadata_update(database, records, target):
    locks = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FOR UPDATE" in statement.upper():
            locks.append((statement, context.execution_options.get("populate_existing")))
    sa.event.listen(database, "before_cursor_execute", capture)
    try:
        call(database, records, target, {field_for(target): "Locked update"})
    finally:
        sa.event.remove(database, "before_cursor_execute", capture)
    entity = target["contract"]["model"].__tablename__
    assert any(entity in statement and refreshed is True for statement, refreshed in locks)


def race(database, target, commands):
    label = "metadata_race_" + uuid4().hex
    def worker(command):
        with Session(database, autoflush=False) as db:
            label_transaction(db, label)
            try:
                return 200, command(db)
            except HTTPException as error:
                return error.status_code, None
    model = target["contract"]["model"]
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        root = blocker.begin()
        blocker.execute(sa.select(model).where(model.id == target["id"]).with_for_update())
        futures = [pool.submit(worker, command) for command in commands]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    count = observer.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                                    "AND application_name = :label AND wait_event_type = 'Lock'"), {"label": label})
                if count == 2:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Both metadata race workers must wait on the PostgreSQL row lock")
        finally:
            root.rollback()
        return [future.result(timeout=12) for future in futures]


def test_concurrent_updates_preserve_serial_before_after_audit_chain(database, records, target):
    field = field_for(target)
    results = race(database, target, [lambda db: call(database, records, target, {field: "First correction"}, db=db),
                                       lambda db: call(database, records, target, {field: "Second correction"}, db=db)])
    assert [code for code, _ in results] == [200, 200]
    first, second = audits(database, target)
    assert [first["entity_revision"], second["entity_revision"]] == [1, 2]
    assert first["previous_values"] == {field: target["original"][field]}
    assert first["new_values"] == second["previous_values"]
    with Session(database) as db:
        assert second["new_values"] == {field: getattr(db.get(target["contract"]["model"], target["id"]), field)}


@pytest.mark.parametrize("other", ["checkout", "maintenance"])
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_location_races_use_post_lock_state_and_preserve_workflows(database, records, target, other):
    due = database_now(database) + timedelta(days=1)
    def workflow(db):
        actor = User(id=records["users"]["FACULTY"])
        if other == "checkout":
            return create_checkout(db, records["asset"], records["users"]["STUDENT"], actor, due)
        return report_maintenance_issue(db, records["asset"], actor, "DAMAGE", "Concurrent fault")
    results = race(database, target, [lambda db: call(database, records, target, {"location": "Moved room"}, db=db), workflow])
    assert results[0][0] in {200, 409} and results[1][0] == 200
    events = audits(database, target)
    assert len(events) == (1 if results[0][0] == 200 else 0)
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        assert asset.location == ("Moved room" if results[0][0] == 200 else target["original"]["location"])
        if other == "checkout":
            assert asset.availability_status == "BORROWED"
            assert db.scalar(sa.select(sa.func.count()).select_from(EquipmentCheckout)) == 1
        else:
            assert asset.maintenance_hold and asset.availability_status == "MAINTENANCE"
            assert db.scalar(sa.select(sa.func.count()).select_from(MaintenanceRequest)) == 1


@pytest.mark.parametrize("operation", ["update", "delete", "truncate"])
def test_audit_history_cannot_be_rewritten_or_removed_by_ordinary_dml(database, records, target, operation):
    call(database, records, target, {field_for(target): "Audited correction"})
    before = snapshot(database, target)
    table = target["audit"].__table__
    with database.connect() as connection:
        transaction = connection.begin()
        statement = (table.update().values(action="UPDATE") if operation == "update" else table.delete())
        if operation == "truncate":
            statement = sa.text("TRUNCATE " + connection.dialect.identifier_preparer.quote(table.name))
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(statement)
        assert error.value.orig.sqlstate == "55000"
        transaction.rollback()
    assert snapshot(database, target) == before


def test_null_already_null_is_a_true_noop(database, records, target):
    field = "manufacturer" if target["kind"] == "equipment" else "nfc_tag_id"
    assert target["original"][field] is None
    before = snapshot(database, target)
    call(database, records, target, {field: None})
    assert snapshot(database, target) == before and audits(database, target) == []


def test_audit_contains_only_actual_changes_from_mixed_noop_payload(database, records, target):
    unchanged = field_for(target)
    changed = "manufacturer" if target["kind"] == "equipment" else "nfc_tag_id"
    call(database, records, target, {unchanged: target["original"][unchanged], changed: "Actual correction"})
    event, = audits(database, target)
    assert event["previous_values"] == {changed: target["original"][changed]}
    assert event["new_values"] == {changed: "Actual correction"}


@pytest.mark.parametrize("target", ["equipment"], indirect=True)
def test_price_audit_uses_exact_two_decimal_strings_without_float_loss(database, records, target):
    call(database, records, target, {"purchase_price": "12.30"})
    event, = audits(database, target)
    assert event["previous_values"] == {"purchase_price": None}
    assert event["new_values"] == {"purchase_price": "12.30"}
    call(database, records, target, {"purchase_price": "12.300"})
    assert audits(database, target) == [event]
    with Session(database) as db:
        assert db.get(Equipment, target["id"]).purchase_price == Decimal("12.30")


def test_concurrent_disjoint_updates_preserve_both_partial_changes(database, records, target):
    field = field_for(target)
    other = "category" if target["kind"] == "equipment" else "nfc_tag_id"
    results = race(database, target, [lambda db: call(database, records, target, {field: "First field"}, db=db),
                                       lambda db: call(database, records, target, {other: "Second field"}, db=db)])
    assert [code for code, _ in results] == [200, 200]
    events = audits(database, target)
    assert len(events) == 2 and {tuple(event["new_values"].keys()) for event in events} == {(field,), (other,)}
    with Session(database) as db:
        entity = db.get(target["contract"]["model"], target["id"])
        assert getattr(entity, field) == "First field" and getattr(entity, other) == "Second field"


def test_lock_timeout_rolls_back_without_metadata_or_audit_changes(database, records, target):
    before = snapshot(database, target)
    model = target["contract"]["model"]
    with database.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(sa.select(model).where(model.id == target["id"]).with_for_update())
        try:
            with Session(database, autoflush=False) as db:
                def timeout(session, root, connection):
                    connection.execute(sa.text("SELECT set_config('lock_timeout', '100ms', true)"))
                sa.event.listen(db, "after_begin", timeout)
                error = conflict(lambda: call(database, records, target, {field_for(target): "Blocked"}, db=db))
                assert "retry" in error.detail.lower() and not db.in_transaction()
                assert not db.info
        finally:
            transaction.rollback()
    assert snapshot(database, target) == before


def test_audit_has_restrictive_typed_entity_and_actor_foreign_keys(database, target):
    with database.connect() as connection:
        keys = sa.inspect(connection).get_foreign_keys(target["audit"].__tablename__)
    expected = {(("actor_id",), "users"), (("equipment_id",), "equipment"), (("asset_id",), "equipment_assets")}
    actual = {(tuple(key["constrained_columns"]), key["referred_table"]) for key in keys
              if key["options"].get("ondelete") == "RESTRICT"}
    assert expected <= actual


def test_direct_audit_fabrication_is_rejected(database, records, target):
    call(database, records, target, {field_for(target): "Legitimate correction"})
    before = snapshot(database, target)
    forged = dict(audits(database, target)[0])
    forged["id"], forged["entity_revision"] = uuid4(), forged["entity_revision"] + 1
    with database.connect() as connection:
        root = connection.begin()
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(target["audit"].__table__.insert().values(**forged))
        assert error.value.orig.sqlstate == "42501"  # Invoker insert guard rejects non-writer identity.
        root.rollback()
    assert snapshot(database, target) == before


def test_reused_identity_map_is_refreshed_after_new_owned_transaction(database, records, target):
    model = target["contract"]["model"]
    field = field_for(target)
    other = "category" if target["kind"] == "equipment" else "serial_number"
    with Session(database, autoflush=False, expire_on_commit=False) as stale:
        cached = stale.get(model, target["id"])
        stale.commit()  # Caller deliberately ends its own setup read transaction.
        call(database, records, target, {field: "Concurrent correction"})
        assert getattr(cached, field) == target["original"][field]
        response = call(database, records, target, {other: "Independent correction"}, db=stale)
        assert getattr(response, field) == "Concurrent correction"
        assert getattr(response, other) == "Independent correction"
        assert not stale.in_transaction()
    events = audits(database, target)
    assert len(events) == 2 and events[-1]["new_values"] == {other: "Independent correction"}


def history(database, records, loan_id, student=False):
    with Session(database) as db:
        role = "STUDENT" if student else "FACULTY"
        user = db.get(User, records["users"][role])
        detail = get_borrowing_history(db, user, loan_id)
        page = list_borrowing_history(db, user, HistoryFilters(asset_id=records["asset"]))
        listed = next(item for item in page.items if item.id == loan_id)
        assert listed.equipment_name == detail.equipment_name and listed.asset_code == detail.asset_code
        assert listed.identifier_snapshot_status == detail.identifier_snapshot_status
        return detail


def loan_snapshots(database, loan_id):
    with Session(database) as db:
        row = db.get(EquipmentCheckout, loan_id)
        return tuple(getattr(row, field) for field in CHECKOUT_SNAPSHOT_FIELDS)


def assert_snapshot_history(database, records, loan_id, expected):
    assert loan_snapshots(database, loan_id) == (*expected, "CAPTURED")
    for student in (False, True):
        result = history(database, records, loan_id, student)
        assert (result.equipment_name, result.asset_code) == expected
        assert result.identifier_snapshot_status == "CAPTURED"


@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_option_a_preserves_checkout_labels_across_rename_return_and_reborrow(database, records, target):
    loan = borrow(database, records)
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        equipment_id = asset.equipment_id
        expected = (db.get(Equipment, equipment_id).name, asset.asset_code)
    assert_snapshot_history(database, records, loan.id, expected)
    for kind, identifier, payload in (("equipment", equipment_id, {"name": "Renamed after checkout"}),
                                      ("asset", records["asset"], {"asset_code": "RECODED-AFTER-CHECKOUT"})):
        schema, operation, _ = runtime_for(kind)
        with Session(database) as db:
            operation(db, **{f"{kind}_id": identifier}, update_data=schema.model_validate(payload),
                      actor=User(id=records["users"]["FACULTY"]))
    assert_snapshot_history(database, records, loan.id, expected)
    with Session(database) as db:
        record_return(db, loan.id, User(id=records["users"]["FACULTY"]), ReturnCreate(condition_status="WORKING"))
    assert_snapshot_history(database, records, loan.id, expected)
    next_loan = borrow(database, records)
    assert_snapshot_history(database, records, next_loan.id, ("Renamed after checkout", "RECODED-AFTER-CHECKOUT"))
    assert_snapshot_history(database, records, loan.id, expected)


@pytest.mark.parametrize("field", CHECKOUT_SNAPSHOT_FIELDS)
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_checkout_snapshot_columns_cannot_be_rewritten_with_ordinary_dml(database, records, target, field):
    loan = borrow(database, records)
    before = snapshot(database, target)
    value = "LEGACY_UNKNOWN" if field == "identifier_snapshot_status" else "Fabricated label"
    with database.connect() as connection:
        root = connection.begin()
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.update(EquipmentCheckout).where(EquipmentCheckout.id == loan.id).values(**{field: value}))
        assert error.value.orig.sqlstate == "55000"
        root.rollback()
    assert snapshot(database, target) == before


@pytest.mark.parametrize("field", CHECKOUT_SNAPSHOT_FIELDS)
@pytest.mark.parametrize("target", ["asset"], indirect=True)
def test_checkout_api_rejects_client_supplied_snapshots(database, records, target, field):
    from backend.tests.registration_helpers import post_json
    from backend.app.database import get_db, get_inventory_write_db
    from backend.app.routers.checkout import router
    from fastapi import FastAPI
    from unittest.mock import patch
    app = FastAPI()
    app.include_router(router)
    def reads():
        with Session(database) as db:
            yield db
    def writes():
        with Session(database) as db:
            yield db
    app.dependency_overrides[get_db], app.dependency_overrides[get_inventory_write_db] = reads, writes
    data = dict(asset_id=str(records["asset"]), student_id=str(records["users"]["STUDENT"]),
                due_at=(database_now(database) + timedelta(days=1)).isoformat(), **{field: "Forged snapshot"})
    before = snapshot(database, target)
    with patch("backend.app.auth.dependencies.is_dev_auth_enabled", return_value=True):
        assert post_json(app, "/checkouts", data)[0] == 422
    assert snapshot(database, target) == before


def test_label_races_capture_consistent_values_and_never_rewrite_history(database, records, target):
    field = field_for(target)
    due = database_now(database) + timedelta(days=1)
    def checkout(db):
        return create_checkout(db, records["asset"], records["users"]["STUDENT"],
                               User(id=records["users"]["FACULTY"]), due)
    results = race(database, target, [lambda db: call(database, records, target, {field: "Renamed during checkout"}, db=db), checkout])
    assert [code for code, _ in results] == [200, 200]
    loan = results[1][1]
    captured = loan_snapshots(database, loan.id)
    assert captured[2] == "CAPTURED"
    with Session(database) as db:
        asset = db.get(EquipmentAsset, records["asset"])
        equipment = db.get(Equipment, asset.equipment_id)
        if target["kind"] == "equipment":
            assert captured[0] in {target["original"]["name"], "Renamed during checkout"}
            assert captured[1] == asset.asset_code
        else:
            assert captured[1] in {target["original"]["asset_code"], "Renamed during checkout"}
            assert captured[0] == equipment.name
    # A later correction must leave whichever serialized snapshot won intact.
    call(database, records, target, {field: "Changed later"})
    assert_snapshot_history(database, records, loan.id, captured[:2])


@pytest.mark.parametrize("target", ["equipment"], indirect=True)
def test_checkout_preserves_asset_first_order_and_protects_parent_name(database, records, target):
    locks = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        upper = statement.upper()
        if upper.lstrip().startswith("SELECT") and ("FOR UPDATE" in upper or "FOR SHARE" in upper):
            locks.append((statement, context.execution_options.get("populate_existing")))
    sa.event.listen(database, "before_cursor_execute", capture)
    try:
        borrow(database, records)
    finally:
        sa.event.remove(database, "before_cursor_execute", capture)
    asset_index = next(i for i, (sql, refreshed) in enumerate(locks) if "equipment_assets" in sql and "FOR UPDATE" in sql.upper())
    equipment_index = next(i for i, (sql, refreshed) in enumerate(locks)
                           if "FROM equipment " in sql and ("FOR SHARE" in sql.upper() or "FOR UPDATE" in sql.upper()))
    checkout_index = next(i for i, (sql, refreshed) in enumerate(locks) if "equipment_checkouts" in sql)
    assert asset_index < equipment_index < checkout_index
    assert locks[asset_index][1] is True and locks[equipment_index][1] is True


def test_audit_actor_context_is_transaction_local(database, records, target):
    call(database, records, target, {field_for(target): "Audited correction"})
    event, = audits(database, target)
    assert event["actor_id"] == records["users"]["FACULTY"]
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.current_setting(AUDIT_ACTOR_SETTING, True))) in {None, ""}
    # Workflow operations need no metadata actor context and create no metadata events.
    before = audits(database, target)
    borrow(database, records)
    assert audits(database, target) == before


@pytest.mark.parametrize("bad_actor", ["missing", "malformed", "unknown", "student", "inactive"])
def test_bad_actor_context_aborts_raw_metadata_update_and_audit(database, records, target, bad_actor):
    if bad_actor == "inactive":
        with Session(database) as db:
            db.get(User, records["users"]["FACULTY"]).is_active = False
            db.commit()
    value = {"missing": "", "malformed": "not-a-uuid", "unknown": str(uuid4()),
             "student": str(records["users"]["STUDENT"]), "inactive": str(records["users"]["FACULTY"])}[bad_actor]
    before = snapshot(database, target)
    with database.connect() as connection:
        root = connection.begin()
        connection.execute(sa.select(sa.func.set_config(AUDIT_ACTOR_SETTING, value, True)))
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.update(target["contract"]["model"]).where(target["contract"]["model"].id == target["id"])
                               .values(**{field_for(target): "Unattributed change"}))
        assert error.value.orig.sqlstate == "42501"
        root.rollback()
    assert snapshot(database, target) == before


def test_client_cannot_choose_audit_actor_or_values(database, target, api):
    before = snapshot(database, target)
    for field, value in (("actor_id", str(uuid4())), ("previous_values", {}), ("new_values", {}),
                         ("entity_revision", 123), ("occurred_at", "2026-01-01T00:00:00Z")):
        assert api({field_for(target): "Denied", field: value})[0] == 422
    assert snapshot(database, target) == before


@pytest.mark.parametrize("target", ["equipment"], indirect=True)
def test_audit_generator_catalog_security_and_exact_relation_bindings(database, target):
    # Catalog inspection is safe under the schema owner, but ACL enforcement
    # additionally needs the restricted-runtime fixture below.
    with database.connect() as connection:
        schema = connection.scalar(sa.text("SELECT current_schema()"))
        rows = connection.execute(sa.text(
            "SELECT p.proname, p.prosecdef, p.proconfig, p.provolatile, pg_get_userbyid(p.proowner) AS owner "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema "
            "AND p.proname IN ('metadata_audit_equipment_change','metadata_audit_asset_change')"), {"schema": schema}).mappings().all()
        assert {row["proname"] for row in rows} == set(AUDIT_GENERATORS)
        for row in rows:
            assert row["prosecdef"] is True
            # The existing harness owns its schema/functions. Real writer-role
            # separation is checked only by the explicitly provisioned ACL case.
            assert row["owner"]
            assert "search_path=pg_catalog, pg_temp" in row["proconfig"] and row["provolatile"] == "v"
        bindings = connection.execute(sa.text(
            "SELECT c.relname, p.proname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid "
            "WHERE n.nspname=:schema AND NOT t.tgisinternal AND p.proname IN "
            "('metadata_audit_equipment_change','metadata_audit_asset_change')"), {"schema": schema}).all()
        assert set(bindings) == {("equipment", "metadata_audit_equipment_change"), ("equipment_assets", "metadata_audit_asset_change")}


@pytest.mark.parametrize("target", ["equipment"], indirect=True)
def test_runtime_acl_separation_is_enforced_in_addition_to_owner_trigger_tests(request, database, target):
    # Configuration-only fixture. Query catalogs through the SAME validated test
    # Engine: never create a second connection with an unverified login/URL.
    try:
        deployment = request.getfixturevalue("metadata_audit_runtime_roles")
    except pytest.FixtureLookupError:
        pytest.skip("Pending approved runtime-role ACL fixture; single-owner schemas cannot prove separation")
    runtime = deployment["runtime_role"]
    with database.connect() as connection:
        schema = connection.scalar(sa.text("SELECT current_schema()"))
        assert runtime not in {AUDIT_OWNER_ROLE, AUDIT_WRITER_ROLE, connection.scalar(sa.text("SELECT current_user"))}
        role = connection.execute(sa.text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"),
            {"role": runtime}).one()
        assert not any(role)
        for privileged in (AUDIT_OWNER_ROLE, AUDIT_WRITER_ROLE):
            assert not connection.scalar(sa.text("SELECT pg_has_role(:runtime, :role, 'MEMBER')"),
                                         {"runtime": runtime, "role": privileged})
        quote = connection.dialect.identifier_preparer.quote
        qualified = quote(schema) + "." + quote(target["audit"].__tablename__)
        assert connection.scalar(sa.text("SELECT has_table_privilege(:runtime, :table, 'SELECT')"),
                                 {"runtime": runtime, "table": qualified})
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER"):
            assert not connection.scalar(sa.text("SELECT has_table_privilege(:runtime, :table, :privilege)"),
                                         {"runtime": runtime, "table": qualified, "privilege": privilege})
        for function in AUDIT_GENERATORS:
            signature = quote(schema) + "." + quote(function) + "()"
            assert not connection.scalar(sa.text("SELECT has_function_privilege(:runtime, :function, 'EXECUTE')"),
                                         {"runtime": runtime, "function": signature})
        owners = connection.execute(sa.text(
            "SELECT p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname=:schema AND p.proname IN ('metadata_audit_equipment_change','metadata_audit_asset_change')"),
            {"schema": schema}).all()
        assert set(owners) == {(name, AUDIT_WRITER_ROLE) for name in AUDIT_GENERATORS}
        assert connection.scalar(sa.text(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=:schema AND c.relname=:table"),
            {"schema": schema, "table": target["audit"].__tablename__}) == AUDIT_OWNER_ROLE
        assert not connection.scalar(sa.text("SELECT has_schema_privilege(:runtime, :schema, 'CREATE')"),
                                     {"runtime": runtime, "schema": schema})


@pytest.fixture
def checkout_before_snapshot_migration(database, records):
    loan_id = uuid4()
    with database.begin() as connection:
        snapshot_revision(connection, "downgrade")
        original = dict(connection.execute(sa.text(
            "INSERT INTO equipment_checkouts (id, asset_id, student_id, issued_by, checked_out_at, due_at) "
            "VALUES (:id, :asset, :student, :staff, clock_timestamp(), clock_timestamp() + interval '1 day') "
            "RETURNING *"
        ), {"id": loan_id, "asset": records["asset"], "student": records["users"]["STUDENT"],
            "staff": records["users"]["FACULTY"]}).mappings().one())
        snapshot_revision(connection, "upgrade")
    return {"engine": database, "records": records, "loan_id": loan_id, "original_loan": original}


def test_legacy_unknown_history_never_falls_back_to_current_labels(test_database, checkout_before_snapshot_migration):
    legacy = checkout_before_snapshot_migration
    database, records, loan_id = legacy["engine"], legacy["records"], legacy["loan_id"]
    if database.url != test_database.url:
        pytest.fail("Legacy engine must use the already identity-verified dedicated database/login", pytrace=False)
    with database.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) == "inventory_checkout_test"
    with Session(database) as db:
        loan = db.get(EquipmentCheckout, loan_id)
        assert loan.identifier_snapshot_status == "LEGACY_UNKNOWN"
        assert loan.equipment_name_at_checkout is None and loan.asset_code_at_checkout is None
        for field, value in legacy["original_loan"].items():
            assert getattr(loan, field) == value
    for student in (False, True):
        result = history(database, records, loan_id, student)
        assert result.equipment_name is None and result.asset_code is None
        assert result.identifier_snapshot_status == "LEGACY_UNKNOWN"


def test_lookalike_users_cannot_supply_a_fake_eligible_actor(database, records, target):
    before = snapshot(database, target)
    with database.connect() as connection:
        root = connection.begin()
        source, lookalike = create_lookalike_schema(connection)
        connection.execute(sa.text(f"CREATE TABLE {lookalike}.users (id uuid, role text, is_active boolean)"))
        fake_actor = uuid4()
        connection.execute(sa.text(f"INSERT INTO {lookalike}.users VALUES (:id, 'ADMIN', true)"), {"id": fake_actor})
        connection.execute(sa.select(sa.func.set_config("search_path", f"{lookalike}, {source}", True)))
        assert connection.scalar(sa.text("SELECT id FROM users")) == fake_actor
        connection.execute(sa.select(sa.func.set_config(AUDIT_ACTOR_SETTING, str(fake_actor), True)))
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.update(target["contract"]["model"]).where(target["contract"]["model"].id == target["id"])
                               .values(**{field_for(target): "Spoofed actor"}))
        assert error.value.orig.sqlstate == "42501"
        root.rollback()
    assert snapshot(database, target) == before


def test_generator_rejects_lookalike_relation_even_when_privileged_owner_attaches_it(database, records, target):
    before = snapshot(database, target)
    table = target["contract"]["model"].__tablename__
    function = AUDIT_GENERATORS[0 if target["kind"] == "equipment" else 1]
    with database.connect() as connection:
        root = connection.begin()
        quote = connection.dialect.identifier_preparer.quote
        schema, lookalike = create_lookalike_schema(connection)
        source = schema + "." + quote(table)
        fake = lookalike + "." + quote(table)
        connection.execute(sa.text(f"CREATE TABLE {fake} (LIKE {source} INCLUDING ALL)"))
        connection.execute(sa.text(f"INSERT INTO {fake} SELECT * FROM {source}"))
        # The ordinary runtime must lack EXECUTE/TRIGGER privileges. The schema
        # owner can attach it, so this additionally exercises the TG_RELID check.
        connection.execute(sa.text(f"CREATE TRIGGER impostor_audit AFTER UPDATE ON {fake} FOR EACH ROW "
                                   f"EXECUTE FUNCTION {schema}.{quote(function)}()"))
        connection.execute(sa.select(sa.func.set_config(AUDIT_ACTOR_SETTING, str(records["users"]["FACULTY"]), True)))
        with pytest.raises(sa.exc.DBAPIError) as error:
            connection.execute(sa.text(f"UPDATE {fake} SET {quote(field_for(target))} = :value WHERE id = :id"),
                               {"value": "Fake source change", "id": target["id"]})
        assert error.value.orig.sqlstate == "42501"
        root.rollback()
    assert snapshot(database, target) == before


def test_staff_deactivation_during_entity_lock_wait_is_rechecked(database, records, target):
    before = snapshot(database, target)
    label = "metadata_auth_wait_" + uuid4().hex
    model = target["contract"]["model"]
    def worker():
        with Session(database, autoflush=False) as db:
            label_transaction(db, label)
            return conflict(lambda: call(database, records, target, {field_for(target): "Denied after wait"}, db=db), 403)
    with database.connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        root = blocker.begin()
        blocker.execute(sa.select(model).where(model.id == target["id"]).with_for_update())
        future = pool.submit(worker)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with database.connect() as observer:
                    waiting = observer.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                                                      "AND application_name=:label AND wait_event_type='Lock'"), {"label": label})
                if waiting == 1:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Metadata updater must wait for the entity lock before deactivation")
            with Session(database) as admin:
                admin.get(User, records["users"]["FACULTY"]).is_active = False
                admin.commit()
        finally:
            root.rollback()
        future.result(timeout=12)
    assert snapshot(database, target) == before
