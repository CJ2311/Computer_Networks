"""Database-free contract, classification, authorization, and ownership tests."""
from datetime import datetime, timezone
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy import CheckConstraint, Column, UniqueConstraint
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database import engine, get_inventory_write_db
from backend.app.models.inventory_audit import (
    InventoryAudit, InventoryAuditExpectedAsset, InventoryAuditScan, InventoryAuditDiscrepancy,
)
from backend.app.models.user import User
from backend.app.routers.inventory_audit import router
from backend.app.schemas.inventory_audit import (
    InventoryAuditCreate, InventoryAuditScanCreate, InventoryAuditFinalize, InventoryAuditCancel,
)
from backend.app.services import inventory_audit as service
from backend.tests.test_inventory_transaction_ownership import recording_engine, Parent
from backend.tests.registration_helpers import post_json


@pytest.fixture(autouse=True)
def no_application_database(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Application database forbidden")
    monkeypatch.setattr(engine, "connect", blocked)
    monkeypatch.setattr(engine, "raw_connection", blocked)


def expected(**changes):
    values = dict(
        id=uuid4(), asset_id=uuid4(), in_scope=True, expected_location="Lab",
        equipment_name_at_start="Sensor", asset_code_at_start="A-1", nfc_tag_id_at_start="TAG",
        availability_at_start="AVAILABLE", condition_at_start="WORKING", maintenance_hold_at_start=False,
        checkout_id_at_start=None, maintenance_id_at_start=None, maintenance_version_at_start=None,
    )
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("schema,payload", [
    (InventoryAuditCreate, {"name": "", "location": "Lab"}),
    (InventoryAuditCreate, {"name": "N", "location": " "}),
    (InventoryAuditCreate, {"name": "N", "location": "Lab", "status": "COMPLETED"}),
    (InventoryAuditCreate, {"name": "N", "location": "Lab", "created_by": str(uuid4())}),
    (InventoryAuditScanCreate, {"nfc_tag_id": "TAG", "observed_location": "Lab"}),
    (InventoryAuditScanCreate, {"idempotency_key": "bad", "nfc_tag_id": "TAG", "observed_location": "Lab"}),
    (InventoryAuditScanCreate, {"idempotency_key": uuid4(), "nfc_tag_id": "", "observed_location": "Lab"}),
    (InventoryAuditScanCreate, {"idempotency_key": uuid4(), "nfc_tag_id": "TAG", "observed_location": "Lab", "result": "MATCHED"}),
    (InventoryAuditScanCreate, {"idempotency_key": uuid4(), "nfc_tag_id": "TAG", "observed_location": "Lab", "asset_id": uuid4()}),
    (InventoryAuditScanCreate, {"idempotency_key": uuid4(), "nfc_tag_id": "a\0b", "observed_location": "Lab"}),
    (InventoryAuditCreate, {"name": "N", "location": "Lab", "notes": "a\0b"}),
    (InventoryAuditFinalize, {"expected_version": True}),
    (InventoryAuditFinalize, {"expected_version": 0}),
    (InventoryAuditCancel, {"expected_version": 1, "reason": " "}),
    (InventoryAuditCancel, {"expected_version": 1, "reason": "x" * 4001}),
])
def test_invalid_inputs_rejected(schema, payload):
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_normalization_and_unicode_are_preserved():
    data = InventoryAuditScanCreate(idempotency_key=uuid4(), nfc_tag_id="  AbC測試  ", observed_location="  प्रयोगशाला  ")
    assert data.nfc_tag_id == "AbC測試" and data.observed_location == "प्रयोगशाला"


@pytest.mark.parametrize("known,in_scope,duplicate,location,result", [
    (False, False, False, "Lab", "UNKNOWN_TAG"),
    (False, False, True, "Lab", "DUPLICATE"),
    (True, False, False, "Lab", "UNEXPECTED"),
    (True, True, False, "Lab", "MATCHED"),
    (True, True, False, "Elsewhere", "MISPLACED"),
    (True, True, True, "Elsewhere", "DUPLICATE"),
])
def test_server_classification(known, in_scope, duplicate, location, result):
    assert service._classify(expected(in_scope=in_scope) if known else None, object() if duplicate else None, location) == result


@pytest.mark.parametrize("checkout,maintenance,hold,kind", [
    (None, None, False, "NOT_VERIFIED"),
    (uuid4(), None, False, "EXPECTED_ON_LOAN"),
    (None, uuid4(), True, "EXPECTED_MAINTENANCE"),
    (None, None, True, "EXPECTED_MAINTENANCE"),
])
def test_unobserved_is_explained_without_mutating_state(checkout, maintenance, hold, kind):
    item = expected(checkout_id_at_start=checkout, maintenance_id_at_start=maintenance, maintenance_hold_at_start=hold)
    before = vars(item).copy()
    current = service._state_at_start(item)
    findings = service._findings([item], [], {item.asset_id: current})
    assert [finding.discrepancy_type for finding in findings] == [kind]
    assert vars(item) == before


def test_state_drift_and_physical_observation_with_active_checkout_are_reported():
    item = expected()
    scan = SimpleNamespace(
        id=uuid4(), expected_asset_id=item.id, asset_id=item.asset_id, result="MATCHED",
        nfc_tag_id="TAG", observed_location="Lab",
    )
    current = service._state_at_start(item) | {"checkout_id": str(uuid4()), "availability_status": "BORROWED"}
    findings = service._findings([item], [scan], {item.asset_id: current})
    assert {finding.discrepancy_type for finding in findings} == {"STATE_CHANGED", "CUSTODY_REVIEW"}


def test_out_of_scope_unobserved_assets_are_not_reported_as_unverified():
    item = expected(in_scope=False)
    assert service._findings([item], [], {}) == []


def test_unknown_duplicate_has_only_one_unknown_finding():
    first = SimpleNamespace(id=uuid4(), result="UNKNOWN_TAG", asset_id=None, expected_asset_id=None,
                            nfc_tag_id="UNKNOWN", observed_location="Lab")
    duplicate = SimpleNamespace(**(vars(first) | {"id": uuid4(), "result": "DUPLICATE"}))
    findings = service._findings([], [first, duplicate], {})
    assert len(findings) == 1 and findings[0].scan_id == first.id


def test_unexpected_asset_finding_exposes_its_historical_snapshot_labels():
    item = expected(in_scope=False, expected_location="Other lab")
    scan = SimpleNamespace(id=uuid4(), result="UNEXPECTED", asset_id=item.asset_id, expected_asset_id=item.id,
                           nfc_tag_id="TAG", observed_location="Lab")
    findings = service._findings([item], [scan], {item.asset_id: service._state_at_start(item)})
    assert len(findings) == 1
    assert findings[0].details["asset_code_at_start"] == "A-1"
    assert findings[0].details["equipment_name_at_start"] == "Sensor"
    assert findings[0].details["expected_location"] == "Other lab"


@pytest.mark.parametrize("different", ["tag", "location", "actor", None])
def test_network_retry_is_distinct_from_physical_scan(different, monkeypatch):
    actor = User(id=uuid4(), role="FACULTY", is_active=True)
    request = InventoryAuditScanCreate(idempotency_key=uuid4(), nfc_tag_id="TAG", observed_location="Lab")
    old = InventoryAuditScan(
        id=uuid4(), audit_id=uuid4(), idempotency_key=request.idempotency_key, nfc_tag_id="TAG",
        observed_location="Lab", scanned_by=actor.id, scanned_at=datetime.now(timezone.utc), result="UNKNOWN_TAG",
    )
    if different == "tag":
        old.nfc_tag_id = "OTHER"
    elif different == "location":
        old.observed_location = "OTHER"
    elif different == "actor":
        old.scanned_by = uuid4()
    db = Mock()
    db.scalar.return_value = old
    monkeypatch.setattr(service, "_authorize", lambda *args: actor)
    monkeypatch.setattr(service, "_audit", lambda *args: SimpleNamespace(id=old.audit_id, status="COMPLETED"))
    if different:
        with pytest.raises(HTTPException) as error:
            service.record_inventory_scan.__wrapped__(db, old.audit_id, request, actor)
        assert error.value.status_code == 409
    else:
        assert service.record_inventory_scan.__wrapped__(db, old.audit_id, request, actor).id == old.id
    db.flush.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize("work", ["root", "savepoint", "pending", "flushed", "sql"])
@pytest.mark.parametrize("operation", ["create", "scan", "finalize", "cancel"])
def test_owned_operations_reject_caller_work_without_ending_it(recording_engine, work, operation):
    recording, state = recording_engine
    with Session(recording) as db:
        if work == "root":
            db.begin()
        elif work == "savepoint":
            db.begin_nested()
        elif work == "sql":
            db.execute(Parent.__table__.insert().values(id=1))
        else:
            db.add(Parent(id=1))
            if work == "flushed":
                db.flush()
        before = (list(state.sql), state.commits, state.rollbacks)
        root, nested = db.get_transaction(), db.get_nested_transaction()
        actor = User(id=uuid4())
        commands = {
            "create": lambda: service.create_inventory_audit(db, InventoryAuditCreate(name="N", location="Lab"), actor),
            "scan": lambda: service.record_inventory_scan(db, uuid4(), InventoryAuditScanCreate(
                idempotency_key=uuid4(), nfc_tag_id="TAG", observed_location="Lab"), actor),
            "finalize": lambda: service.finalize_inventory_audit(db, uuid4(), InventoryAuditFinalize(expected_version=2), actor),
            "cancel": lambda: service.cancel_inventory_audit(db, uuid4(), InventoryAuditCancel(expected_version=2, reason="X"), actor),
        }
        with pytest.raises(HTTPException) as error:
            commands[operation]()
        assert error.value.status_code == 409
        assert db.get_transaction() is root and db.get_nested_transaction() is nested
        assert before == (state.sql, state.commits, state.rollbacks)


def test_snapshot_query_is_one_statement_and_excludes_quantity_inventory():
    statement = service._inventory_statement().compile(dialect=postgresql.dialect())
    sql = str(statement)
    assert sql.count("SELECT ") == 1 and "LEFT OUTER JOIN equipment_checkouts" in sql
    assert "LEFT OUTER JOIN maintenance_requests" in sql and "statement_timestamp()" in sql
    assert "INDIVIDUAL" in statement.params.values()
    assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql


@pytest.mark.parametrize("operation", ["get", "list", "report", "observations"])
def test_reads_preserve_unrelated_pending_caller_work(recording_engine, operation):
    recording, state = recording_engine
    with Session(recording) as db:
        unrelated = Parent(id=1)
        db.add(unrelated)
        root = db.get_transaction()
        before = (list(state.sql), state.commits, state.rollbacks)
        actor = User(id=uuid4())
        calls = {
            "get": lambda: service.get_inventory_audit(db, uuid4(), actor),
            "list": lambda: service.list_inventory_audits(db, actor),
            "report": lambda: service.get_inventory_audit_report(db, uuid4(), actor),
            "observations": lambda: service.list_inventory_observations(db, uuid4(), actor, scans=True),
        }
        with pytest.raises(HTTPException) as error:
            calls[operation]()
        assert error.value.status_code == 409
        assert unrelated in db.new and db.get_transaction() is root and root.is_active
        assert before == (state.sql, state.commits, state.rollbacks)


def test_migration_is_renderable_offline_and_has_no_destructive_upgrade(monkeypatch):
    revision = import_module("backend.alembic.versions.d6f2b8a931c0_inventory_audits")
    assert revision.down_revision == "c4e8a17d92f6"
    monkeypatch.setattr(revision, "_schema", lambda: '"isolated"')
    monkeypatch.setattr(revision, "_configure_runtime_privileges", lambda: None)
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        revision.upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE ") == 4
    assert "fk_inventory_discrepancy_scan_audit" in sql and "fk_inventory_discrepancy_scan_asset" in sql
    assert "uq_inventory_scan_idempotency" in sql and "uq_inventory_scan_first_tag" in sql
    assert "SECURITY DEFINER" not in sql and "SET search_path = pg_catalog, pg_temp" in sql
    assert "DROP TABLE" not in sql and "UPDATE equipment_assets" not in sql


def test_single_owner_migration_exception_is_restricted_to_the_test_identity(monkeypatch):
    revision = import_module("backend.alembic.versions.d6f2b8a931c0_inventory_audits")
    operations = Mock()
    connection = operations.get_bind.return_value
    operations.get_context.return_value.opts = {"metadata_audit_isolated_test": True}
    monkeypatch.setattr(revision, "op", operations)
    monkeypatch.setattr(revision, "_schema", lambda: '"isolated"')
    connection.execute.return_value.one.return_value = (
        "inventory_checkout_test", "inventory_checkout_test_login", "inventory_checkout_test_login",
    )
    connection.scalar.return_value = "public"
    with pytest.raises(RuntimeError, match="verified dedicated schema"):
        revision._configure_runtime_privileges()
    connection.scalar.return_value = "checkout_it_" + "a" * 32
    revision._configure_runtime_privileges()


def test_migration_columns_checks_and_uniqueness_match_model_metadata(monkeypatch):
    revision = import_module("backend.alembic.versions.d6f2b8a931c0_inventory_audits")
    operations = Mock()
    monkeypatch.setattr(revision, "op", operations)
    monkeypatch.setattr(revision, "_install_guards", lambda: None)
    monkeypatch.setattr(revision, "_configure_runtime_privileges", lambda: None)
    revision.upgrade()
    models = {model.__tablename__: model.__table__ for model in (
        InventoryAudit, InventoryAuditExpectedAsset, InventoryAuditScan, InventoryAuditDiscrepancy,
    )}
    assert {call.args[0] for call in operations.create_table.call_args_list} == set(models)
    for call in operations.create_table.call_args_list:
        name, *definitions = call.args
        model = models[name]
        columns = {column.name: column for column in definitions if isinstance(column, Column)}
        assert set(columns) == set(model.c.keys())
        for key, column in columns.items():
            assert column.nullable == model.c[key].nullable
            assert str(column.type.compile(dialect=postgresql.dialect())) == str(model.c[key].type.compile(dialect=postgresql.dialect()))
        checks = {item.name: str(item.sqltext) for item in definitions if isinstance(item, CheckConstraint)}
        assert checks == {item.name: str(item.sqltext) for item in model.constraints if isinstance(item, CheckConstraint)}
        uniques = {item.name: tuple(item._pending_colargs) for item in definitions if isinstance(item, UniqueConstraint)}
        for extra in operations.create_unique_constraint.call_args_list:
            constraint, table, keys = extra.args
            if table == name:
                uniques[constraint] = tuple(keys)
        assert uniques == {item.name: tuple(column.name for column in item.columns)
                           for item in model.constraints if isinstance(item, UniqueConstraint)}


@pytest.mark.parametrize("role,path", [
    ("STUDENT", "/inventory-audits"), ("ADMIN", "/inventory-audits/{id}/finalize"),
    ("LAB_ASSISTANT", "/inventory-audits/{id}/finalize"), ("LAB_ASSISTANT", "/inventory-audits/{id}/cancel"),
])
def test_routes_reject_unauthorized_roles_before_service(role, path):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: User(id=uuid4(), role=role, is_active=True)
    app.dependency_overrides[get_inventory_write_db] = lambda: None
    data = {"name": "N", "location": "Lab"} if path == "/inventory-audits" else {"expected_version": 2}
    if path.endswith("cancel"):
        data["reason"] = "X"
    assert post_json(app, path.replace("{id}", str(uuid4())), data)[0] == 403


def test_openapi_exposes_server_results_but_accepts_only_raw_scan_observation():
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    scan = schema["components"]["schemas"]["InventoryAuditScanCreate"]
    assert set(scan["properties"]) == {"idempotency_key", "nfc_tag_id", "observed_location"}
    assert scan["additionalProperties"] is False
    assert len(schema["paths"]) == 7
