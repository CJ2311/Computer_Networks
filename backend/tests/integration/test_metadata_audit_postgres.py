"""Exercise database-owned metadata auditing without Task 3B-3 services."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.metadata_audit import MetadataAuditEvent
from backend.app.models.user import User
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.checkout import create_checkout, record_return
from backend.tests.integration.inventory_helpers import database_now, set_metadata_actor
from backend.tests.integration.test_checkout_postgres import records

AUDIT = MetadataAuditEvent.__table__


def equipment_id(connection, records):
    return connection.scalar(sa.select(EquipmentAsset.equipment_id).where(EquipmentAsset.id == records["asset"]))


def events(connection):
    return connection.execute(sa.select(AUDIT).order_by(AUDIT.c.entity_type, AUDIT.c.entity_revision)).mappings().all()


def change_equipment(connection, records, **changes):
    connection.execute(sa.update(Equipment).where(Equipment.id == equipment_id(connection, records)).values(**changes))


def create_lookalike_schema(connection):
    # Same-name decoys need their own namespace. The caller rolls back the
    # transaction that creates this sibling of the generated test schema.
    schema = connection.scalar(sa.text("SELECT current_schema()"))
    quote = connection.dialect.identifier_preparer.quote_identifier
    source = quote(schema)
    lookalike = quote(f"{schema}_lookalike")
    connection.execute(sa.text(f"CREATE SCHEMA {lookalike}"))
    return source, lookalike


def expect_error(connection, statement, *states):
    with pytest.raises(sa.exc.DBAPIError) as error:
        with connection.begin_nested():
            connection.execute(statement)
    assert error.value.orig.sqlstate in states


@pytest.mark.parametrize("role", ["ADMIN", "FACULTY", "LAB_ASSISTANT"])
def test_exact_diffs_and_revision_chain(database, records, role):
    actor = records["users"][role]
    with database.begin() as connection:
        assert events(connection) == []
        set_metadata_actor(connection, actor)
        earliest = connection.scalar(sa.select(sa.func.clock_timestamp()))
        change_equipment(connection, records, name="Corrected name", purchase_price=Decimal("1234567890.10"), category="IoT")
        change_equipment(connection, records, purchase_price=Decimal("12.30"))
        change_equipment(connection, records, purchase_price=Decimal("12.30"))
        connection.execute(sa.update(EquipmentAsset).where(EquipmentAsset.id == records["asset"])
                           .values(location="  New lab  ", nfc_tag_id="Tag-1"))
        audit = events(connection)
        latest = connection.scalar(sa.select(sa.func.clock_timestamp()))
        assert len(audit) == 3
        assert all(row.actor_id == actor and row.action == "UPDATE" for row in audit)
        assert all(earliest <= row.occurred_at <= latest for row in audit)
        first, second = [row for row in audit if row.entity_type == "EQUIPMENT"]
        assert first.entity_id == first.equipment_id == equipment_id(connection, records)
        assert first.asset_id is None
        assert first.previous_values == {"name": "Test sensor", "purchase_price": None}
        assert first.new_values == {"name": "Corrected name", "purchase_price": "1234567890.10"}
        assert second.previous_values == {"purchase_price": "1234567890.10"}
        assert second.new_values == {"purchase_price": "12.30"}
        assert (first.entity_revision, second.entity_revision) == (1, 2)
        asset, = [row for row in audit if row.entity_type == "ASSET"]
        assert asset.entity_id == asset.asset_id == records["asset"] and asset.equipment_id is None
        assert asset.previous_values == {"location": "Test lab", "nfc_tag_id": None}
        assert asset.new_values == {"location": "  New lab  ", "nfc_tag_id": "Tag-1"}
    with database.connect() as connection:
        assert len(events(connection)) == 3


@pytest.mark.parametrize("actor", ["missing", "empty", "malformed", "unknown", "student", "inactive", "lookalike_user"])
def test_invalid_actor_cannot_change_metadata(database, records, actor):
    with database.connect() as connection, connection.begin() as transaction:
        faculty = records["users"]["FACULTY"]
        if actor == "inactive":
            connection.execute(sa.update(User).where(User.id == faculty).values(is_active=False))
        bogus = uuid4()
        if actor == "lookalike_user":
            source, lookalike = create_lookalike_schema(connection)
            connection.execute(sa.text(f"CREATE TABLE {lookalike}.users (id uuid, role text, is_active boolean)"))
            connection.execute(sa.text(f"INSERT INTO {lookalike}.users VALUES (:id, 'ADMIN', true)"), {"id": bogus})
            connection.execute(sa.select(sa.func.set_config("search_path", f"{lookalike}, {source}", True)))
            assert connection.scalar(sa.text("SELECT id FROM users")) == bogus
            assert connection.scalar(sa.text(
                f"SELECT id FROM {source}.users WHERE id = :id"
            ), {"id": bogus}) is None
        if actor != "missing":
            set_metadata_actor(connection, {
                "empty": "", "malformed": "invalid uuid", "unknown": bogus,
                "student": records["users"]["STUDENT"], "inactive": faculty, "lookalike_user": bogus,
            }[actor])
        expect_error(connection, sa.update(Equipment).where(Equipment.id == equipment_id(connection, records))
                     .values(name="Rejected"), "42501")
        assert connection.scalar(sa.select(Equipment.name)) == "Test sensor"
        assert events(connection) == []
        transaction.rollback()


@pytest.mark.parametrize("commit", [False, True])
def test_actor_context_does_not_leak_and_rollback_is_atomic(database, records, commit):
    with database.connect() as connection:
        transaction = connection.begin()
        set_metadata_actor(connection, records["users"]["FACULTY"])
        change_equipment(connection, records, name="Edited")
        assert len(events(connection)) == 1
        if commit:
            transaction.commit()
        else:
            transaction.rollback()
        assert connection.scalar(sa.text("SELECT current_setting('inventory.metadata_actor_id', true)")) in (None, "")
        expect_error(connection, sa.update(Equipment).values(name="No actor"), "42501")
        assert connection.scalar(sa.select(Equipment.name)) == ("Edited" if commit else "Test sensor")
        assert len(events(connection)) == int(commit)


def test_checkout_return_and_noops_need_no_audit_actor(database, records):
    with database.begin() as connection:
        connection.execute(sa.update(Equipment).values(name=Equipment.name))
        connection.execute(sa.update(EquipmentAsset).values(location=EquipmentAsset.location))
    actor = User(id=records["users"]["FACULTY"])
    with Session(database) as db:
        loan = create_checkout(db, records["asset"], records["users"]["STUDENT"],
                               actor, database_now(database) + timedelta(days=1))
    with Session(database) as db:
        record_return(db, loan.id, actor, ReturnCreate(condition_status="WORKING"))
    with database.begin() as connection:
        assert events(connection) == []
        set_metadata_actor(connection, actor.id)
        change_equipment(connection, records, name="Renamed")
        snapshot = connection.execute(sa.text(
            "SELECT equipment_name_at_checkout, asset_code_at_checkout, identifier_snapshot_status "
            "FROM equipment_checkouts WHERE id = :id"), {"id": loan.id}).one()
        assert tuple(snapshot) == ("Test sensor", "TEST-001", "CAPTURED")
        assert len(events(connection)) == 1


def test_append_only_and_restrictive_foreign_keys(database, records):
    with database.begin() as connection:
        set_metadata_actor(connection, records["users"]["FACULTY"])
        change_equipment(connection, records, name="Edited")
        connection.execute(sa.update(EquipmentAsset).values(location="Other lab"))
        before = events(connection)
        forged = dict(before[0], id=uuid4(), entity_revision=2)
        expect_error(connection, AUDIT.insert().values(**forged), "42501")
        expect_error(connection, AUDIT.update().values(action="UPDATE"), "55000")
        expect_error(connection, AUDIT.delete(), "55000")
        expect_error(connection, sa.text("TRUNCATE metadata_audit_events"), "55000")
        expect_error(connection, sa.delete(EquipmentAsset), "23503", "23001")
        expect_error(connection, sa.delete(User).where(User.id == records["users"]["FACULTY"]), "23503", "23001")
        assert events(connection) == before


def test_catalog_guards_and_public_privileges(database):
    with database.connect() as connection:
        inspector = sa.inspect(connection)
        assert {key["name"] for key in inspector.get_check_constraints(AUDIT.name)} == {
            "ck_metadata_audit_action", "ck_metadata_audit_entity", "ck_metadata_audit_diff", "ck_metadata_audit_revision",
        }
        assert {tuple(key["constrained_columns"]): key["options"]["ondelete"]
                for key in inspector.get_foreign_keys(AUDIT.name)} == {
            ("actor_id",): "RESTRICT", ("equipment_id",): "RESTRICT", ("asset_id",): "RESTRICT",
        }
        assert {tuple(item["column_names"]) for item in inspector.get_unique_constraints(AUDIT.name)} == {
            ("entity_type", "entity_id", "entity_revision"),
        }
        assert not connection.scalar(sa.text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl
                WHERE c.oid = 'metadata_audit_events'::regclass AND acl.grantee = 0
            )
        """))
        functions = connection.execute(sa.text("""
            SELECT proname, prosecdef, proconfig, EXISTS (
                SELECT 1 FROM aclexplode(coalesce(proacl, acldefault('f', proowner))) WHERE grantee = 0
            ) AS public_grant
            FROM pg_proc WHERE pronamespace = current_schema()::regnamespace
              AND proname LIKE 'metadata_audit_%'
        """)).all()
        assert {name: definer for name, definer, _, _ in functions} == {
            "metadata_audit_equipment_change": True, "metadata_audit_asset_change": True,
            "metadata_audit_protect": False,
        }
        assert all("search_path=pg_catalog, pg_temp" in config and not public for _, _, config, public in functions)


def test_generator_rejects_lookalike_source(database, records):
    with database.connect() as connection, connection.begin() as transaction:
        source, lookalike = create_lookalike_schema(connection)
        connection.execute(sa.text(f"CREATE TABLE {lookalike}.equipment AS TABLE {source}.equipment"))
        connection.execute(sa.text(
            f"CREATE TRIGGER impostor AFTER UPDATE ON {lookalike}.equipment FOR EACH ROW "
            f"EXECUTE FUNCTION {source}.metadata_audit_equipment_change()"))
        set_metadata_actor(connection, records["users"]["FACULTY"])
        expect_error(connection, sa.text(f"UPDATE {lookalike}.equipment SET name = 'Forged'"), "42501")
        assert connection.scalar(sa.text(f"SELECT name FROM {lookalike}.equipment")) == "Test sensor"
        assert events(connection) == []
        transaction.rollback()


def test_failed_audit_insert_aborts_source_update(database, records):
    with database.begin() as connection:
        connection.execute(sa.text("""
            CREATE FUNCTION fail_audit_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Test audit failure' USING ERRCODE = '23514'; END; $$;
            CREATE TRIGGER fail_audit_insert BEFORE INSERT ON metadata_audit_events
            FOR EACH ROW EXECUTE FUNCTION fail_audit_insert()
        """))
        set_metadata_actor(connection, records["users"]["FACULTY"])
        expect_error(connection, sa.update(Equipment).values(name="Must roll back"), "23514")
        assert connection.scalar(sa.select(Equipment.name)) == "Test sensor"
        assert events(connection) == []


def test_downgrade_round_trip_and_populated_refusal(database, records):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from backend.tests.test_metadata_audit import migration

    with database.begin() as connection:
        context = MigrationContext.configure(connection, opts={"metadata_audit_isolated_test": True})
        with Operations.context(context):
            migration.downgrade()
            assert not sa.inspect(connection).has_table(AUDIT.name)
            migration.upgrade()
            set_metadata_actor(connection, records["users"]["FACULTY"])
            change_equipment(connection, records, name="Audited")
            with pytest.raises(sa.exc.DBAPIError) as error:
                with connection.begin_nested():
                    migration.downgrade()
            assert error.value.orig.sqlstate == "55000"
            assert len(events(connection)) == 1

@pytest.mark.parametrize("grant, message", [
    ("GRANT UPDATE (actor_id) ON metadata_audit_events TO PUBLIC", "table or column"),
    ("GRANT EXECUTE ON FUNCTION metadata_audit_equipment_change() TO PUBLIC", "function"),
    ("GRANT EXECUTE ON FUNCTION metadata_audit_protect() TO PUBLIC", "function"),
])
def test_acl_validator_rejects_unexpected_grants(database, grant, message):
    from backend.tests.test_metadata_audit import migration

    with database.begin() as connection:
        _, schema = migration._schema(connection)
        owner = connection.scalar(sa.text("SELECT current_user"))
        migration._validate_object_acls(connection, schema, owner=owner, writer=owner)
        with pytest.raises(RuntimeError, match=message):
            with connection.begin_nested():
                connection.execute(sa.text(grant))
                migration._validate_object_acls(connection, schema, owner=owner, writer=owner)
        migration._validate_object_acls(connection, schema, owner=owner, writer=owner)


def test_trigger_grant_can_tamper_with_new_event_and_fails_acl_validation(database, records):
    from backend.tests.test_metadata_audit import migration

    # The isolated login owns the objects. Demonstrate the consequence of
    # trigger attachment without creating or assuming any database roles.
    with database.begin() as connection:
        _, schema = migration._schema(connection)
        owner = connection.scalar(sa.text("SELECT current_user"))
        with pytest.raises(RuntimeError, match="table or column"):
            with connection.begin_nested():
                connection.execute(sa.text("GRANT TRIGGER ON metadata_audit_events TO PUBLIC"))
                connection.execute(sa.text("""
                    CREATE FUNCTION test_rewrite_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        NEW.new_values := jsonb_build_object('name', 'Forged');
                        RETURN NEW;
                    END; $$;
                    CREATE TRIGGER test_rewrite_audit BEFORE INSERT ON metadata_audit_events
                    FOR EACH ROW EXECUTE FUNCTION test_rewrite_audit()
                """))
                set_metadata_actor(connection, records["users"]["FACULTY"])
                change_equipment(connection, records, name="Real change")
                assert connection.scalar(sa.select(Equipment.name)) == "Real change"
                event, = events(connection)
                assert event.new_values == {"name": "Forged"}
                migration._validate_object_acls(connection, schema, owner=owner, writer=owner)
        migration._validate_object_acls(connection, schema, owner=owner, writer=owner)
        assert events(connection) == []
        assert connection.scalar(sa.select(Equipment.name)) == "Test sensor"
        assert connection.scalar(sa.text("SELECT to_regprocedure('test_rewrite_audit()')")) is None
