"""Real PostgreSQL tests, gated by container identity and explicit opt-in.

No production runner or migration function is replaced. SQLAlchemy events only
pause/fail the client's real DDL/commit boundary for deterministic injection.
"""
from contextlib import contextmanager
from dataclasses import replace
import threading
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy import event, text

from infra.nfc_postgres.harness import ADMIN, IsolationError
from scripts import deploy_nfc_demo as runner


def revision(db):
    return runner.read_checkpoint(db.engine(), db.settings)


def reach_b7(db):
    db.stage(None, runner.B7, runner.MIGRATOR)


@contextmanager
def paused_stage(db, user, before, after):
    engine = db.engine(user)
    paused, resume = threading.Event(), threading.Event()
    errors, pid = [], []
    def pause(connection, cursor, statement, parameters, context, many):
        if not paused.is_set() and statement.lstrip().upper().startswith("CREATE TABLE"):
            paused.set()
            if not resume.wait(20):
                raise RuntimeError("Test barrier timed out")
    event.listen(engine, "after_cursor_execute", pause)
    def execute():
        try:
            with engine.connect() as connection:
                db.verify_engine_identity(connection)
                connection.rollback()
                pid.append(connection.connection.driver_connection.info.backend_pid)
                runner.run_stage(connection, db.settings, user, before, after)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    try:
        assert paused.wait(20)
        yield pid[0], errors
    finally:
        resume.set()
        thread.join(25)
        if thread.is_alive() and pid:
            db.terminate(pid[0])
            thread.join(10)
        event.remove(engine, "after_cursor_execute", pause)
        assert not thread.is_alive()


def test_successful_staged_deployment_and_verify_only_rerun(database):
    database.deploy()
    assert revision(database) == runner.HEAD
    database.deploy()  # already-complete verification, never stamp or replay
    assert revision(database) == runner.HEAD


@pytest.mark.parametrize("holder,contender", [(runner.MIGRATOR, ADMIN), (ADMIN, runner.MIGRATOR)])
def test_transaction_lock_contention_across_identities(database, holder, contender):
    with database.engine(holder).connect() as first, first.begin():
        database.verify_engine_identity(first)
        runner.acquire_stage_lock(first)
        with database.engine(contender).connect() as second:
            with pytest.raises(runner.DeploymentError):
                with second.begin():
                    runner.acquire_stage_lock(second)
        assert first.in_transaction()
    with database.engine(contender).connect() as second, second.begin():
        runner.acquire_stage_lock(second)  # commit released holder's transaction lock


@pytest.mark.parametrize("user,before,after", [(runner.MIGRATOR, None, runner.B7), (ADMIN, runner.B7, runner.C4)])
def test_concurrent_runner_stage_fails_before_migration(database, user, before, after):
    if before == runner.B7:
        reach_b7(database)
    other = ADMIN if user == runner.MIGRATOR else runner.MIGRATOR
    with paused_stage(database, user, before, after) as (_, errors):
        with database.engine(other).connect() as connection:
            with pytest.raises(runner.DeploymentError, match="holds the lock"):
                runner.run_stage(connection, database.settings, other, before, after)
    assert not errors
    assert revision(database) == after


@pytest.mark.parametrize("user,before,after", [(runner.MIGRATOR, None, runner.B7), (ADMIN, runner.B7, runner.C4)])
def test_backend_termination_rolls_back_ddl_and_version(database, user, before, after):
    if before == runner.B7:
        reach_b7(database)
    with paused_stage(database, user, before, after) as (pid, errors):
        database.terminate(pid)
    assert errors
    assert revision(database) == before
    database.deploy()  # lock is gone; safe resumption from last committed state
    assert revision(database) == runner.HEAD


def test_client_failure_after_ddl_rolls_back(database):
    engine = database.engine()
    def fail(connection, cursor, statement, *args):
        if statement.lstrip().upper().startswith("CREATE TABLE"):
            raise RuntimeError("Injected test failure after DDL")
    event.listen(engine, "after_cursor_execute", fail)
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError):
                runner.run_stage(connection, database.settings, runner.MIGRATOR, None, runner.B7)
    finally:
        event.remove(engine, "after_cursor_execute", fail)
    assert revision(database) is None


def test_metadata_ownership_and_runtime_permissions(database):
    database.deploy()
    with database.engine(ADMIN).begin() as connection:
        database.verify_engine_identity(connection)
        assert connection.scalar(text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.metadata_audit_events'::regclass")) == "inventory_metadata_audit_owner"
        rows = connection.execute(text("SELECT proname, pg_get_userbyid(proowner), prosecdef FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace WHERE n.nspname='public' AND proname LIKE 'metadata_audit_%'" )).all()
        assert set(rows) == {
            ("metadata_audit_protect", "inventory_metadata_audit_owner", False),
            ("metadata_audit_equipment_change", "inventory_metadata_audit_writer", True),
            ("metadata_audit_asset_change", "inventory_metadata_audit_writer", True)}
        actor, equipment = uuid.uuid4(), uuid.uuid4()
        connection.execute(text("INSERT INTO users(id,college_id,full_name,email,role) VALUES (:id,'SYNTHETIC','Synthetic','synthetic@example.invalid','FACULTY')"), {"id": actor})
        connection.execute(text("INSERT INTO equipment(id,name,category,tracking_type,default_location) VALUES (:id,'Synthetic','Demo','QUANTITY','Demo')"), {"id": equipment})
        connection.exec_driver_sql("GRANT SELECT ON users TO inventory_app")
        connection.exec_driver_sql("GRANT SELECT, UPDATE ON equipment TO inventory_app")
    with database.engine("inventory_app").begin() as connection:
        database.verify_engine_identity(connection)
        connection.execute(text("SELECT set_config('inventory.metadata_actor_id', :actor, true)"), {"actor": str(actor)})
        connection.execute(text("UPDATE equipment SET name='Synthetic changed' WHERE id=:id"), {"id": equipment})
        assert connection.scalar(text("SELECT count(*) FROM metadata_audit_events")) == 1
        for statement in (
            "DELETE FROM metadata_audit_events", "UPDATE metadata_audit_events SET entity_revision=999",
            "TRUNCATE metadata_audit_events", "UPDATE users SET role='ADMIN'",
            "SELECT metadata_audit_equipment_change()",
            "CREATE TRIGGER forged AFTER UPDATE ON equipment FOR EACH ROW EXECUTE FUNCTION metadata_audit_equipment_change()",
        ):
            with pytest.raises(Exception):
                with connection.begin_nested():
                    connection.exec_driver_sql(statement)
    assert revision(database) == runner.HEAD


@pytest.mark.parametrize("alteration", ["predicate", "column", "disabled", "deferred"])
def test_trigger_catalog_rejects_drift(database, alteration):
    database.deploy()
    with database.engine(ADMIN).begin() as connection:
        database.verify_engine_identity(connection)
        if alteration == "disabled":
            connection.exec_driver_sql("ALTER TABLE equipment DISABLE TRIGGER metadata_audit_equipment_change")
        else:
            connection.exec_driver_sql("DROP TRIGGER metadata_audit_equipment_change ON equipment")
            if alteration == "deferred":
                definition = "CREATE CONSTRAINT TRIGGER metadata_audit_equipment_change AFTER UPDATE ON equipment DEFERRABLE INITIALLY DEFERRED"
            else:
                definition = "CREATE TRIGGER metadata_audit_equipment_change AFTER UPDATE" + (" OF name" if alteration == "column" else "") + " ON equipment"
            definition += " FOR EACH ROW" + (" WHEN (false)" if alteration == "predicate" else "")
            connection.exec_driver_sql(definition + " EXECUTE FUNCTION metadata_audit_equipment_change()")
    with pytest.raises(runner.DeploymentError):
        revision(database)


@pytest.mark.parametrize("checkpoint", [runner.B7, runner.C4])
def test_interrupted_deployment_resume(database, checkpoint):
    reach_b7(database)
    if checkpoint == runner.C4:
        database.stage(runner.B7, runner.C4, ADMIN)
    assert revision(database) == checkpoint
    database.deploy()
    assert revision(database) == runner.HEAD


def test_administrator_prompt_race_fails_closed(database):
    reach_b7(database)
    def prompt(message):
        if "administrator" in message:
            # A real second runner completes the deployment while A has no lock.
            database.deploy()
            return database.passwords[ADMIN]
        return database.passwords[runner.MIGRATOR]
    def factory(settings, user, password):
        assert settings == database.settings
        return database.engine(user)
    with pytest.raises(runner.DeploymentError, match="starting revision"):
        runner.deploy(database.settings, factory, prompt, lambda _: "INITIALIZE inventory_nfc_demo")
    assert revision(database) == runner.HEAD


class LostCommitAcknowledgement(ConnectionError):
    pass


def test_ambiguous_commit_real_server_commit_then_lost_ack(database):
    # A real driver COMMIT occurs; only its acknowledgement is hidden from the
    # client. This is deterministic outcome injection, not a network-fault claim.
    engine = database.engine()
    original_commit = engine.dialect.do_commit
    fired = [False]
    def commit_then_disconnect(dbapi_connection):
        original_commit(dbapi_connection)
        if not fired[0]:
            fired[0] = True
            raise LostCommitAcknowledgement("Injected lost acknowledgement")
    engine.dialect.do_commit = commit_then_disconnect
    try:
        with engine.connect() as connection:
            with pytest.raises(LostCommitAcknowledgement):
                runner.run_stage(connection, database.settings, runner.MIGRATOR, None, runner.B7)
    finally:
        engine.dialect.do_commit = original_commit
    assert fired[0]
    assert revision(database) == runner.B7
    database.deploy()
    assert revision(database) == runner.HEAD


@pytest.mark.parametrize("changes", [{"database": "inventory_db"}, {"database": "inventory_checkout_test"}, {"database": ""}, {"schema": "pg_catalog"}])
def test_unsafe_configuration_rejected_before_connections(changes):
    from infra.nfc_postgres.harness import ADMIN
    settings = runner.Settings(runner.DATABASE, "127.0.0.1", 5432, ADMIN)
    factory = Mock(side_effect=AssertionError("No connection may be constructed"))
    with pytest.raises(runner.DeploymentError):
        runner.deploy(replace(settings, **changes), factory)
    factory.assert_not_called()


def test_destructive_controller_rejects_foreign_identity(database):
    saved = database.system_identifier
    database.system_identifier = "unrelated-instance"
    try:
        with pytest.raises(IsolationError):
            with database.control():
                pytest.fail("Foreign instance accepted")
    finally:
        database.system_identifier = saved


def test_interactive_cli_uses_unchanged_demo_entry_point(database):
    import os
    import select
    import subprocess
    import sys
    import time
    master, slave = os.openpty()
    process = subprocess.Popen([sys.executable, "scripts/deploy_nfc_demo.py", "--database", runner.DATABASE,
        "--server-address", "127.0.0.1", "--port", "5432", "--schema", "public", "--administrator", ADMIN],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    exchanges = [
        (b"Type INITIALIZE inventory_nfc_demo to permit migration writes:", "INITIALIZE inventory_nfc_demo"),
        (b"Migrator password (hidden):", database.passwords[runner.MIGRATOR]),
        (b"Deployment administrator password (hidden):", database.passwords[ADMIN]),
    ]
    received, index = b"", 0
    try:
        deadline = time.monotonic() + 90
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    received += os.read(master, 4096)
                except OSError:
                    break
                if index < len(exchanges) and exchanges[index][0] in received:
                    os.write(master, exchanges[index][1].encode() + b"\n")
                    index += 1
        assert process.wait(timeout=5) == 0
        assert index == len(exchanges)
        assert all(password.encode() not in received for password in database.passwords.values())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
    assert revision(database) == runner.HEAD
