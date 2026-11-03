"""Database foundation tests; shared fixtures enforce dedicated test isolation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from backend.app.database import Base
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User


REQUESTS = MaintenanceRequest.__table__
EVENTS = MaintenanceEvent.__table__
ASSETS = EquipmentAsset.__table__


def revision():
    path = next((Path(__file__).resolve().parents[2] / "alembic" / "versions").glob("a3c7e91b6d20_*.py"))
    spec = importlib.util.spec_from_file_location("foundation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migrate(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(revision(), direction)()


@pytest.fixture
def foundation_rows(database):
    with database.begin() as connection:
        staff = [uuid4(), uuid4()]
        for number, user_id in enumerate(staff):
            connection.execute(sa.insert(User), dict(
                id=user_id, college_id=f"MAINT-{number}", full_name="Test staff",
                email=f"maint-{number}@example.invalid", role="FACULTY", is_active=True,
            ))
        equipment_id = uuid4()
        connection.execute(sa.insert(Equipment), dict(id=equipment_id, name="Test sensor", category="IoT",
                                                     tracking_type="INDIVIDUAL", default_location="Test lab"))
        assets = [uuid4(), uuid4()]
        for number, asset_id in enumerate(assets):
            connection.execute(sa.insert(ASSETS), dict(id=asset_id, equipment_id=equipment_id,
                                                      asset_code=f"MAINT-{number}", location="Test lab"))
    return dict(staff=staff, assets=assets)


def report(connection, rows, **changes):
    values = dict(id=uuid4(), asset_id=rows["assets"][0], issue_type="DAMAGE",
                  issue_description="Test equipment fault", reported_by=rows["staff"][0])
    values.update(changes)
    connection.execute(sa.update(ASSETS).where(ASSETS.c.id == values["asset_id"]).values(
        availability_status="MAINTENANCE", maintenance_hold=True,
    ))
    connection.execute(sa.insert(REQUESTS), values)
    connection.execute(sa.insert(EVENTS), dict(id=uuid4(), request_id=values["id"], sequence=1,
                                              event_type="REPORTED", actor_id=rows["staff"][0],
                                              to_status=values.get("status", "OPEN")))
    return values["id"]


def cancelled_fields(connection, rows):
    now = connection.scalar(sa.select(sa.func.clock_timestamp()))
    return dict(status="CANCELLED", reported_at=now - timedelta(minutes=1), cancelled_at=now,
                cancelled_by=rows["staff"][0], cancellation_reason="Requires replacement inspection")


def assert_error(connection, action, code):
    with pytest.raises(DBAPIError) as error:
        with connection.begin_nested():
            action()
    assert error.value.orig.sqlstate in ((code,) if isinstance(code, str) else code)


def test_foundation_upgrade_matches_metadata_and_installs_guards(database):
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, Base.metadata) == []
        inspector = sa.inspect(connection)
        indexes = {i["name"]: i for i in inspector.get_indexes("maintenance_requests")}
        assert indexes["uq_active_maintenance_per_asset"]["unique"]
        predicate = indexes["uq_active_maintenance_per_asset"]["dialect_options"]["postgresql_where"]
        assert all(status in predicate for status in ("OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION"))
        assert "COMPLETED" not in predicate and "CANCELLED" not in predicate
        triggers = connection.execute(sa.text(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND NOT t.tgisinternal"
        )).scalars().all()
        assert set(triggers) == {"maintenance_request_validate", "maintenance_request_no_delete",
                                 "maintenance_request_no_truncate", "maintenance_event_no_rewrite",
                                 "maintenance_event_no_truncate", "maintenance_asset_hold_validate"}
        functions = connection.execute(sa.text(
            "SELECT p.prosecdef, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname LIKE 'maintenance_%'"
        )).all()
        assert len(functions) == 3
        assert all(not security_definer and "search_path=pg_catalog" in config for security_definer, config in functions)


def test_upgrade_preserves_legacy_reserved_values_and_empty_downgrade_round_trip(database, foundation_rows):
    with database.begin() as connection:
        migrate(connection, "downgrade")
        assert "maintenance_requests" not in sa.inspect(connection).get_table_names()
        assert "maintenance_hold" not in {c["name"] for c in sa.inspect(connection).get_columns("equipment_assets")}
        for asset_id, condition in zip(foundation_rows["assets"], ("WORKING", "DAMAGED")):
            connection.execute(sa.text(
                "UPDATE equipment_assets SET availability_status = 'RESERVED', condition_status = :condition WHERE id = :id"
            ), {"condition": condition, "id": asset_id})
        before = connection.execute(sa.text("SELECT id, availability_status, condition_status FROM equipment_assets ORDER BY id")).all()
        migrate(connection, "upgrade")
        after = connection.execute(sa.text("SELECT id, availability_status, condition_status FROM equipment_assets ORDER BY id")).all()
        assert before == after
        assert not any(connection.execute(sa.select(ASSETS.c.maintenance_hold)).scalars())


@pytest.mark.parametrize("availability, condition, hold, valid", [
    ("MAINTENANCE", "DAMAGED", True, True), ("RESERVED", "DAMAGED", True, True),
    ("BORROWED", "DAMAGED", True, True), ("RESERVED", "WORKING", False, True),
    ("AVAILABLE", "WORKING", False, True), ("AVAILABLE", "DAMAGED", False, False),
    ("AVAILABLE", "WORKING", True, False), ("MAINTENANCE", "DAMAGED", False, False),
])
def test_asset_condition_availability_and_hold_constraints(database, foundation_rows, availability, condition, hold, valid):
    with database.begin() as connection:
        action = lambda: connection.execute(sa.update(ASSETS).where(ASSETS.c.id == foundation_rows["assets"][0]).values(
            availability_status=availability, condition_status=condition, maintenance_hold=hold,
        ))
        if valid:
            action()
        else:
            assert_error(connection, action, "23514")


@pytest.mark.parametrize("changes", [
    {"status": "INVALID"}, {"issue_type": "INVALID"}, {"issue_description": " \t\n"},
    {"issue_description": "x" * 4001}, {"version": 0}, {"version": 2},
    {"status": "IN_PROGRESS"}, {"status": "AWAITING_VERIFICATION"},
    {"status": "COMPLETED"}, {"status": "CANCELLED"},
])
def test_invalid_request_values_are_rejected(database, foundation_rows, changes):
    with database.begin() as connection:
        assert_error(connection, lambda: report(connection, foundation_rows, **changes), "23514")


def test_duplicate_active_request_and_deferred_audit_requirement(database, foundation_rows):
    with database.begin() as connection:
        request_id = report(connection, foundation_rows)
        assert_error(connection, lambda: report(connection, foundation_rows), "23505")
        def missing_event():
            connection.execute(sa.insert(REQUESTS), dict(
                id=uuid4(), asset_id=foundation_rows["assets"][1], issue_type="INSPECTION",
                issue_description="Missing audit event", reported_by=foundation_rows["staff"][0],
                status="CANCELLED", **{key: value for key, value in cancelled_fields(connection, foundation_rows).items() if key != "status"},
            ))
            connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert_error(connection, missing_event, "23503")
        connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert connection.scalar(sa.select(REQUESTS.c.version).where(REQUESTS.c.id == request_id)) == 1


def test_request_and_event_changes_are_atomic_and_versioned(database, foundation_rows):
    with database.begin() as connection:
        request_id = report(connection, foundation_rows)
        def without_event():
            connection.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(
                assigned_to=foundation_rows["staff"][1], version=2,
            ))
            connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert_error(connection, without_event, "23503")
        original = connection.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id)).one()
        assert original.version == 1 and original.assigned_to is None
        connection.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(
            assigned_to=foundation_rows["staff"][1], version=2,
        ))
        connection.execute(sa.insert(EVENTS), dict(id=uuid4(), request_id=request_id, sequence=2,
                                                  event_type="ASSIGNED", actor_id=foundation_rows["staff"][0],
                                                  to_assigned_to=foundation_rows["staff"][1]))
        connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.parametrize("statement", [
    "UPDATE maintenance_events SET note = 'overwrite'", "DELETE FROM maintenance_events",
    "DELETE FROM maintenance_requests", "TRUNCATE maintenance_requests, maintenance_events",
    "UPDATE maintenance_requests SET issue_description = 'overwrite', version = 2",
    "UPDATE maintenance_requests SET reported_at = clock_timestamp(), version = 2",
])
def test_history_cannot_be_rewritten_or_removed(database, foundation_rows, statement):
    with database.begin() as connection:
        report(connection, foundation_rows)
        before = (
            connection.execute(sa.select(REQUESTS).order_by(REQUESTS.c.id)).all(),
            connection.execute(sa.select(EVENTS).order_by(EVENTS.c.id)).all(),
        )
        # Pending deferred FK events may block TRUNCATE before our guard runs.
        codes = ("55000", "55006") if statement.startswith("TRUNCATE") else "55000"
        assert_error(connection, lambda: connection.execute(sa.text(statement)), codes)
        if statement.startswith("TRUNCATE"):
            # Also prove the explicit no-truncate trigger protects the tables
            # when the transaction no longer has pending deferred FK events.
            connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert_error(connection, lambda: connection.execute(sa.text(statement)), "55000")
        assert before == (
            connection.execute(sa.select(REQUESTS).order_by(REQUESTS.c.id)).all(),
            connection.execute(sa.select(EVENTS).order_by(EVENTS.c.id)).all(),
        )


def test_terminal_records_are_immutable_and_recovery_is_linked(database, foundation_rows):
    with database.begin() as connection:
        parent = report(connection, foundation_rows, **cancelled_fields(connection, foundation_rows))
        assert_error(connection, lambda: connection.execute(sa.update(REQUESTS).where(REQUESTS.c.id == parent).values(version=2)), "55000")
        child = report(connection, foundation_rows, recovery_of_request_id=parent, issue_type="INSPECTION")
        assert connection.scalar(sa.select(REQUESTS.c.recovery_of_request_id).where(REQUESTS.c.id == child)) == parent
        assert_error(connection, lambda: report(connection, foundation_rows, asset_id=foundation_rows["assets"][1],
                                                recovery_of_request_id=parent, issue_type="INSPECTION"), "23503")


def test_recovery_of_active_request_is_rejected(database, foundation_rows):
    with database.begin() as connection:
        parent = report(connection, foundation_rows)
        assert_error(connection, lambda: report(connection, foundation_rows, recovery_of_request_id=parent,
                                                issue_type="INSPECTION"), "23514")


def test_active_request_requires_and_preserves_asset_hold(database, foundation_rows):
    with database.begin() as connection:
        assert_error(connection, lambda: connection.execute(sa.insert(REQUESTS), dict(
            id=uuid4(), asset_id=foundation_rows["assets"][0], issue_type="DAMAGE",
            issue_description="No hold", reported_by=foundation_rows["staff"][0],
        )), "23514")
        report(connection, foundation_rows)
        assert_error(connection, lambda: connection.execute(sa.update(ASSETS).where(
            ASSETS.c.id == foundation_rows["assets"][0]
        ).values(maintenance_hold=False, availability_status="AVAILABLE")), "23514")


@pytest.mark.parametrize("populated", ["history", "hold", "completed"])
def test_downgrade_refuses_to_discard_maintenance_data(database, foundation_rows, populated):
    with database.begin() as connection:
        if populated == "hold":
            connection.execute(sa.update(ASSETS).where(ASSETS.c.id == foundation_rows["assets"][0]).values(
                maintenance_hold=True, availability_status="MAINTENANCE",
            ))
        elif populated == "completed":
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            report(connection, foundation_rows, status="COMPLETED", reported_at=now - timedelta(hours=2),
                   assigned_to=foundation_rows["staff"][0], started_at=now - timedelta(hours=1),
                   repair_completed_at=now - timedelta(minutes=30), repair_completed_by=foundation_rows["staff"][0],
                   verified_at=now, verified_by=foundation_rows["staff"][1], returned_to_service_at=now)
        else:
            report(connection, foundation_rows)
        assert_error(connection, lambda: migrate(connection, "downgrade"), "55000")
        assert "maintenance_requests" in sa.inspect(connection).get_table_names()


def test_failed_upgrade_does_not_reclassify_inconsistent_legacy_asset(database, foundation_rows):
    with database.begin() as connection:
        migrate(connection, "downgrade")
        connection.execute(sa.text("UPDATE equipment_assets SET condition_status = 'DAMAGED'"))
        assert_error(connection, lambda: migrate(connection, "upgrade"), "23514")
        assert "maintenance_requests" not in sa.inspect(connection).get_table_names()
        assert "maintenance_hold" not in {c["name"] for c in sa.inspect(connection).get_columns("equipment_assets")}
        assert set(connection.execute(sa.text("SELECT availability_status, condition_status FROM equipment_assets")).all()) == {("AVAILABLE", "DAMAGED")}


def test_concurrent_active_request_insert_has_one_winner(database, foundation_rows):
    from threading import Barrier
    ready = Barrier(2)

    def attempt():
        try:
            with database.begin() as connection:
                ready.wait(timeout=5)
                report(connection, foundation_rows)
            return "created"
        except DBAPIError as error:
            return error.orig.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(results) == ["23505", "created"]
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(REQUESTS)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(EVENTS)) == 1


@pytest.mark.parametrize("changes, code", [
    ({"sequence": 0}, "23514"), ({"sequence": 1}, "23505"),
    ({"event_type": "INVALID"}, "23514"), ({"note": " \t\n"}, "23514"),
    ({"note": "x" * 5001}, "23514"), ({"event_type": "REPAIR_COMPLETED", "note": None}, "23514"),
    ({"from_status": "INVALID"}, "23514"), ({"to_availability": "INVALID"}, "23514"),
    ({"actor_id": uuid4()}, "23503"),
])
def test_event_constraints_and_actor_foreign_key(database, foundation_rows, changes, code):
    with database.begin() as connection:
        request_id = report(connection, foundation_rows)
        values = dict(id=uuid4(), request_id=request_id, sequence=2,
                      event_type="NOTE_ADDED", note="Inspection note", actor_id=foundation_rows["staff"][0])
        values.update(changes)
        assert_error(connection, lambda: connection.execute(sa.insert(EVENTS), values), code)


@pytest.mark.parametrize("target", ["asset", "reporter"])
def test_restrictive_foreign_keys_preserve_maintenance_history(database, foundation_rows, target):
    with database.begin() as connection:
        request_id = report(connection, foundation_rows)
        before = connection.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id)).one()
        statement = (sa.delete(ASSETS).where(ASSETS.c.id == foundation_rows["assets"][0]) if target == "asset"
                     else sa.delete(User).where(User.id == foundation_rows["staff"][0]))
        # RESTRICT uses 23001; 23503 is also a blocking referential-integrity error.
        assert_error(connection, lambda: connection.execute(statement), ("23001", "23503"))
        assert connection.scalar(sa.select(ASSETS.c.id).where(ASSETS.c.id == foundation_rows["assets"][0])) == foundation_rows["assets"][0]
        assert connection.scalar(sa.select(User.id).where(User.id == foundation_rows["staff"][0])) == foundation_rows["staff"][0]
        assert connection.execute(sa.select(REQUESTS).where(REQUESTS.c.id == request_id)).one() == before
        assert connection.scalar(sa.select(sa.func.count()).select_from(EVENTS).where(EVENTS.c.request_id == request_id)) == 1


@pytest.mark.parametrize("invalid", ["self_verification", "repair_before_start", "service_before_verification"])
def test_request_timestamp_and_independent_verifier_constraints(database, foundation_rows, invalid):
    with database.begin() as connection:
        now = connection.scalar(sa.select(sa.func.clock_timestamp()))
        values = dict(status="COMPLETED", reported_at=now - timedelta(hours=2),
                      assigned_to=foundation_rows["staff"][0], started_at=now - timedelta(hours=1),
                      repair_completed_at=now - timedelta(minutes=30), repair_completed_by=foundation_rows["staff"][0],
                      verified_at=now, verified_by=foundation_rows["staff"][1], returned_to_service_at=now)
        if invalid == "self_verification":
            values["verified_by"] = foundation_rows["staff"][0]
        elif invalid == "repair_before_start":
            values["repair_completed_at"] = now - timedelta(minutes=90)
        else:
            values["returned_to_service_at"] = now - timedelta(minutes=1)
        assert_error(connection, lambda: report(connection, foundation_rows, **values), "23514")
