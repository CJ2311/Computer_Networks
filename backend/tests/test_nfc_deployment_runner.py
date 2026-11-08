"""Deployment runner tests: no driver connections or migration execution."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from scripts import deploy_nfc_demo as runner

SETTINGS = runner.Settings(runner.DATABASE, "127.0.0.1", 5432, "postgres")
CONFIRM = "INITIALIZE inventory_nfc_demo"


@pytest.fixture
def rig(monkeypatch):
    original_stage = runner.run_stage
    events = []
    connection = MagicMock()
    @contextmanager
    def begin():
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    connection.begin.side_effect = begin
    connection.in_transaction.return_value = True
    def acquire(*args, **kwargs):
        events.append("lock")
        return True
    connection.scalar.side_effect = acquire
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    factory = Mock(return_value=engine)
    monkeypatch.setattr(runner, "check_connection", Mock())
    state = [None]
    monkeypatch.setattr(runner, "checkpoint", lambda *a: state[0])
    def stage(c, s, user, before, after):
        with c.begin():
            runner.acquire_stage_lock(c)
            assert state[0] == before
            events.append((user, before, after))
            state[0] = after
    monkeypatch.setattr(runner, "run_stage", stage)
    return SimpleNamespace(events=events, state=state, factory=factory, connection=connection, engine=engine,
                           original_stage=original_stage)


@pytest.mark.parametrize("start,expected", [
    (None, [runner.B7, runner.C4, runner.HEAD]),
    (runner.B7, [runner.C4, runner.HEAD]), (runner.C4, [runner.HEAD]), (runner.HEAD, []),
])
def test_stage_order_and_resume(rig, start, expected):
    rig.state[0] = start
    prompt = Mock(return_value="synthetic-secret")
    runner.deploy(SETTINGS, rig.factory, prompt, lambda _: CONFIRM)
    stages = [event for event in rig.events if isinstance(event, tuple)]
    assert [s[2] for s in stages] == expected
    assert [s[0] for s in stages] == [SETTINGS.administrator if s == runner.C4 else runner.MIGRATOR for s in expected]
    assert rig.events.index("lock") < (rig.events.index(stages[0]) if stages else len(rig.events))
    assert rig.events[-1] == "commit"
    assert prompt.call_count == (2 if runner.C4 in expected else 1)
    for call in rig.factory.call_args_list:
        assert call.args[0].database == runner.DATABASE
        assert call.args[2] == "synthetic-secret"


@pytest.mark.parametrize("changes", [
    {"database": "inventory_db"}, {"database": "inventory_checkout_test"},
    {"database": ""}, {"server_address": ""}, {"server_address": "192.0.2.1"},
    {"port": 0}, {"administrator": ""}, {"administrator": "inventory_migrator"},
    {"schema": "pg_catalog"}, {"schema": "public;evil"},
])
def test_invalid_settings_before_prompt_or_connect(rig, changes):
    prompt = Mock()
    with pytest.raises(runner.DeploymentError):
        runner.deploy(replace(SETTINGS, **changes), rig.factory, prompt)
    rig.factory.assert_not_called()
    prompt.assert_not_called()


def test_confirmation_required_before_password_or_connection(rig):
    prompt = Mock()
    with pytest.raises(runner.DeploymentError, match="not confirmed"):
        runner.deploy(SETTINGS, rig.factory, prompt, lambda _: "no")
    prompt.assert_not_called()
    rig.factory.assert_not_called()


def test_empty_password_does_not_fall_back_to_environment(rig):
    with pytest.raises(runner.DeploymentError, match="password"):
        runner.deploy(SETTINGS, rig.factory, lambda _: "", lambda _: CONFIRM)
    rig.factory.assert_not_called()


def test_failure_disposes_connections(rig, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic stage failure")
    monkeypatch.setattr(runner, "run_stage", fail)
    with pytest.raises(RuntimeError):
        runner.deploy(SETTINGS, rig.factory, lambda _: "synthetic", lambda _: CONFIRM)
    rig.engine.dispose.assert_called_once()
    rig.engine.connect.return_value.__exit__.assert_called()


@pytest.mark.parametrize("actual", [
    ("inventory_db", runner.MIGRATOR, runner.MIGRATOR, "public", ["pg_catalog", "public"]),
    (runner.DATABASE, "wrong", runner.MIGRATOR, "public", ["pg_catalog", "public"]),
    (runner.DATABASE, runner.MIGRATOR, "wrong", "public", ["pg_catalog", "public"]),
    (runner.DATABASE, runner.MIGRATOR, runner.MIGRATOR, "other", ["pg_catalog", "other"]),
])
def test_actual_identity_rejected(actual):
    connection = Mock()
    connection.dialect.name = "postgresql"
    connection.execute.return_value.one.return_value = actual
    with pytest.raises(RuntimeError):
        runner.check_connection(connection, SETTINGS, runner.MIGRATOR)


@pytest.mark.parametrize("server", [
    ("127.0.0.2", 5432, 180004, False, "read committed"),
    ("127.0.0.1", 5433, 180004, False, "read committed"),
    ("127.0.0.1", 5432, 170000, False, "read committed"),
    ("127.0.0.1", 5432, 180004, True, "read committed"),
])
def test_server_mismatch(monkeypatch, server):
    monkeypatch.setattr(runner, "verify_identity", Mock())
    connection = Mock()
    connection.execute.return_value.one.return_value = server
    with pytest.raises(runner.DeploymentError):
        runner.check_connection(connection, SETTINGS, runner.MIGRATOR)


def test_administrator_must_be_superuser(monkeypatch):
    monkeypatch.setattr(runner, "verify_identity", Mock())
    connection = Mock()
    connection.execute.return_value.one.return_value = ("127.0.0.1", 5432, 180004, False, "read committed")
    with pytest.raises(runner.DeploymentError):
        runner.check_connection(connection, SETTINGS, SETTINGS.administrator)


@pytest.mark.parametrize("revision", ["unknown", "f9bc7a594a00", ""])
def test_unknown_checkpoint(revision):
    with pytest.raises(runner.DeploymentError):
        runner.expected_tables(revision)


@pytest.mark.parametrize("tables,markers", [
    ({"equipment"}, []), ({"alembic_version"}, []),
    ({"alembic_version"}, ["unknown"]), ({"alembic_version"}, [runner.B7, runner.C4]),
    ({"alembic_version", "equipment"}, [runner.B7]),
])
def test_partial_deployment_fails(monkeypatch, tables, markers):
    inspector = Mock()
    inspector.get_table_names.return_value = tables
    monkeypatch.setattr(runner, "inspect", lambda c: inspector)
    connection = Mock()
    connection.dialect = postgresql.dialect()
    connection.execute.return_value.scalars.return_value.all.return_value = markers
    with pytest.raises(runner.DeploymentError):
        runner.checkpoint(connection, SETTINGS)


@pytest.mark.parametrize("fail", [True, False])
def test_stage_transaction_and_dedicated_config(monkeypatch, fail):
    events = []
    connection = Mock()
    @contextmanager
    def transaction():
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    connection.begin.side_effect = transaction
    monkeypatch.setattr(runner, "check_connection", Mock())
    monkeypatch.setattr(runner, "checkpoint", Mock(side_effect=[runner.B7, runner.C4]))
    upgrade = Mock(side_effect=RuntimeError("synthetic failure") if fail else None)
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    if fail:
        with pytest.raises(RuntimeError):
            runner.run_stage(connection, SETTINGS, SETTINGS.administrator, runner.B7, runner.C4)
    else:
        runner.run_stage(connection, SETTINGS, SETTINGS.administrator, runner.B7, runner.C4)
    assert events == ["begin", "rollback" if fail else "commit"]
    assert "pg_try_advisory_xact_lock" in str(connection.scalar.call_args.args[0])
    config, target = upgrade.call_args.args
    assert config.get_main_option("script_location").endswith("backend/alembic_nfc_demo")
    assert config.attributes["connection"] is connection
    assert config.attributes["nfc_demo_identity"]["session_user"] == SETTINGS.administrator
    assert target == runner.C4
    connection.close.assert_not_called()


def test_no_ordinary_config_even_if_ini_changed(monkeypatch):
    config = runner.Config(str(runner.ROOT / "alembic.ini"))
    monkeypatch.setattr(runner, "Config", lambda _: config)
    with pytest.raises(runner.DeploymentError, match="Dedicated"):
        runner.demo_config(Mock(), SETTINGS, runner.MIGRATOR)


@pytest.mark.parametrize("fail", [True, False])
def test_lock_acquisition_release_and_exception(fail):
    # Real SQLAlchemy transaction lifecycle, simulated PostgreSQL lock result.
    import sqlalchemy
    engine = sqlalchemy.create_engine("sqlite://")
    events = []
    from sqlalchemy import event
    event.listen(engine, "commit", lambda c: events.append("commit"))
    event.listen(engine, "rollback", lambda c: events.append("rollback"))
    try:
        with engine.connect() as connection:
            connection.scalar = Mock(return_value=True)
            if fail:
                with pytest.raises(RuntimeError, match="stage failure"):
                    with connection.begin():
                        runner.acquire_stage_lock(connection)
                        raise RuntimeError("stage failure")
            else:
                with connection.begin():
                    runner.acquire_stage_lock(connection)
            assert not connection.in_transaction()
            assert "pg_try_advisory_xact_lock" in str(connection.scalar.call_args.args[0])
        assert events == ["rollback" if fail else "commit"]
    finally:
        engine.dispose()


def test_contended_lock_never_unlocks_other_owner():
    connection = Mock()
    connection.scalar.return_value = False
    with pytest.raises(runner.DeploymentError):
        runner.acquire_stage_lock(connection)
    connection.scalar.assert_called_once()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()


def test_terminal_redacts_driver_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["deploy", "--database", runner.DATABASE,
        "--server-address", "127.0.0.1", "--port", "5432", "--administrator", "postgres", "--schema", "public"])
    monkeypatch.setattr(runner, "deploy", Mock(side_effect=RuntimeError("SYNTHETIC_PASSWORD postgres://secret")))
    previous = runner.logging.root.manager.disable
    try:
        assert runner.main() == 1
    finally:
        runner.logging.disable(previous)
    output = capsys.readouterr()
    assert "SYNTHETIC_PASSWORD" not in output.err
    assert "postgres://" not in output.err


def test_engine_credentials_are_in_memory_only(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(runner, "create_engine", factory)
    runner.make_engine(SETTINGS, SETTINGS.administrator, "SYNTHETIC_SECRET")
    url = factory.call_args.args[0]
    assert url.database == runner.DATABASE
    assert url.password == "SYNTHETIC_SECRET"
    assert factory.call_args.kwargs["hide_parameters"] is True
    assert factory.call_args.kwargs["echo"] is False

@pytest.mark.parametrize("revision", [None, runner.B7, runner.C4, runner.HEAD])
def test_consistent_schema_checkpoint(monkeypatch, revision):
    connection = Mock()
    connection.dialect = postgresql.dialect()
    connection.scalar.return_value = False
    connection.execute.return_value.scalars.return_value.all.return_value = [revision]
    inspector = Mock()
    inspector.get_table_names.return_value = runner.expected_tables(revision)
    inspector.get_view_names.return_value = []
    inspector.get_columns.side_effect = lambda name, **k: [
        dict(name=c.name, nullable=c.nullable, type=c.type) for c in runner.Base.metadata.tables[name].columns]
    inspector.get_check_constraints.side_effect = lambda name, **k: [
        dict(name=c.name) for c in runner.Base.metadata.tables[name].constraints if c.__class__.__name__ == "CheckConstraint"]
    inspector.get_pk_constraint.side_effect = lambda name, **k: dict(constrained_columns=[c.name for c in runner.Base.metadata.tables[name].primary_key])
    inspector.get_indexes.side_effect = lambda name, **k: [dict(name=i.name) for i in runner.Base.metadata.tables[name].indexes]
    inspector.get_unique_constraints.side_effect = lambda name, **k: [
        dict(column_names=[col.name for col in c.columns]) for c in runner.Base.metadata.tables[name].constraints if c.__class__.__name__ == "UniqueConstraint"]
    inspector.get_foreign_keys.side_effect = lambda name, **k: [dict(
        constrained_columns=[col.name for col in c.columns], referred_table=c.elements[0].column.table.name,
        referred_columns=[e.column.name for e in c.elements]) for c in runner.Base.metadata.tables[name].foreign_key_constraints]
    monkeypatch.setattr(runner, "inspect", lambda c: inspector)
    monkeypatch.setattr(runner, "validate_guards", Mock())
    assert runner.checkpoint(connection, SETTINGS) == revision
    if revision:
        inspector.get_columns.side_effect = lambda *a, **k: []
        with pytest.raises(runner.DeploymentError, match="columns"):
            runner.checkpoint(connection, SETTINGS)


def test_secure_password_refuses_echo_fallback(monkeypatch):
    import warnings
    def warning_prompt(message):
        warnings.warn("Cannot control terminal", runner.getpass.GetPassWarning)
        pytest.fail("Echo fallback must not occur")
    monkeypatch.setattr(runner.getpass, "getpass", warning_prompt)
    with pytest.raises(runner.getpass.GetPassWarning):
        runner.secure_password("test prompt")


def test_terminal_error_redaction_with_interactive_terminal(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["deploy", "--database", runner.DATABASE,
        "--server-address", "127.0.0.1", "--port", "5432", "--administrator", "postgres", "--schema", "public"])
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runner, "deploy", Mock(side_effect=RuntimeError("SYNTHETIC_PASSWORD postgres://secret")))
    previous = runner.logging.root.manager.disable
    try:
        assert runner.main() == 1
    finally:
        runner.logging.disable(previous)
    output = capsys.readouterr()
    assert "SYNTHETIC_PASSWORD" not in output.err
    assert "postgres://" not in output.err


def test_missing_lock_aborts():
    connection = Mock()
    connection.scalar.return_value = False
    with pytest.raises(runner.DeploymentError, match="holds the lock"):
        runner.acquire_stage_lock(connection)


def test_stage_revision_mismatch_never_calls_alembic(monkeypatch):
    from contextlib import nullcontext
    connection = Mock()
    connection.begin.side_effect = nullcontext
    monkeypatch.setattr(runner, "check_connection", Mock())
    monkeypatch.setattr(runner, "checkpoint", Mock(return_value=runner.HEAD))
    upgrade = Mock()
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    with pytest.raises(runner.DeploymentError, match="starting revision"):
        runner.run_stage(connection, SETTINGS, SETTINGS.administrator, runner.B7, runner.C4)
    upgrade.assert_not_called()


def test_admin_failure_disposes_admin_engine(rig, monkeypatch):
    rig.state[0] = runner.B7
    def fail(*args):
        raise RuntimeError("stage 2 failed")
    monkeypatch.setattr(runner, "run_stage", fail)
    with pytest.raises(RuntimeError):
        runner.deploy(SETTINGS, rig.factory, lambda _: "synthetic", lambda _: CONFIRM)
    assert rig.factory.call_count == 2
    assert rig.engine.dispose.call_count == 2
    rig.engine.connect.return_value.__exit__.assert_called()


def test_stage_lock_requires_transaction():
    connection = Mock()
    connection.in_transaction.return_value = False
    with pytest.raises(runner.DeploymentError, match="transaction"):
        runner.acquire_stage_lock(connection)
    connection.scalar.assert_not_called()


def test_credential_prompt_has_no_lock_or_transaction_and_revalidates(rig, monkeypatch):
    # The old inspection session can disappear while input is awaited: it no
    # longer protects a later administrator transaction. Simulate another runner
    # committing stage 2 during that gap, then verify stale work is rejected.
    rig.state[0] = runner.B7
    monkeypatch.setattr(runner, "run_stage", rig.original_stage)
    upgrade = Mock()
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    def prompt(message):
        if "administrator" in message:
            assert rig.events[-1] == "commit"  # inspection transaction ended
            rig.state[0] = runner.C4  # other runner advanced while prompt waited
        return "synthetic-secret"
    with pytest.raises(runner.DeploymentError, match="starting revision"):
        runner.deploy(SETTINGS, rig.factory, prompt, lambda _: CONFIRM)
    upgrade.assert_not_called()
    assert rig.events[-1] == "rollback"
    assert rig.events[-3:] == ["begin", "lock", "rollback"]


def test_lost_migration_connection_rolls_back_and_never_reconnects(rig, monkeypatch):
    monkeypatch.setattr(runner, "run_stage", rig.original_stage)
    rig.state[0] = runner.B7
    upgrade = Mock(side_effect=ConnectionError("migration connection terminated"))
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    with pytest.raises(ConnectionError):
        runner.deploy(SETTINGS, rig.factory, lambda _: "synthetic", lambda _: CONFIRM)
    assert rig.state[0] == runner.B7
    assert rig.events[-1] == "rollback"
    assert rig.factory.call_count == 2  # migrator and admin, no retry/reconnect
    assert upgrade.call_count == 1


def test_concurrent_stage_cannot_enter_checkpoint(monkeypatch):
    # Deterministic cooperative-lock model. The first stage's upgrade invokes
    # a second stage on another connection while the first transaction is active.
    held = [None]
    events = []
    def connection(label):
        c = Mock()
        c.in_transaction.return_value = True
        @contextmanager
        def begin():
            try:
                yield
            except Exception:
                events.append((label, "rollback"))
                raise
            else:
                events.append((label, "commit"))
            finally:
                if held[0] is c:
                    held[0] = None
        c.begin.side_effect = begin
        def lock(*args, **kwargs):
            if held[0] is not None:
                return False
            held[0] = c
            return True
        c.scalar.side_effect = lock
        return c
    first, second = connection("first"), connection("second")
    monkeypatch.setattr(runner, "check_connection", Mock())
    checks = Mock(side_effect=[runner.B7, runner.C4])
    monkeypatch.setattr(runner, "checkpoint", checks)
    def upgrade(*args):
        assert held[0] is first
        with pytest.raises(runner.DeploymentError, match="holds the lock"):
            runner.run_stage(second, SETTINGS, SETTINGS.administrator, runner.B7, runner.C4)
        assert held[0] is first
    monkeypatch.setattr(runner.command, "upgrade", upgrade)
    runner.run_stage(first, SETTINGS, SETTINGS.administrator, runner.B7, runner.C4)
    assert checks.call_count == 2  # the contender never inspected the schema
    assert held[0] is None
    assert events == [("second", "rollback"), ("first", "commit")]


def test_interrupted_deployment_resumes_from_last_committed_stage(rig, monkeypatch):
    original = runner.run_stage
    attempts = []
    def interrupted(c, s, user, before, after):
        attempts.append(after)
        if after == runner.C4:
            raise ConnectionError("admin connection lost")
        original(c, s, user, before, after)
    monkeypatch.setattr(runner, "run_stage", interrupted)
    with pytest.raises(ConnectionError):
        runner.deploy(SETTINGS, rig.factory, lambda _: "synthetic", lambda _: CONFIRM)
    assert rig.state[0] == runner.B7
    monkeypatch.setattr(runner, "run_stage", original)
    runner.deploy(SETTINGS, rig.factory, lambda _: "synthetic", lambda _: CONFIRM)
    stages = [event[2] for event in rig.events if isinstance(event, tuple)]
    assert stages == [runner.B7, runner.C4, runner.HEAD]


def guard_catalog(revision):
    # Independent fixtures transcribed from approved CREATE TRIGGER definitions.
    triggers = [
        ("maintenance_requests", "maintenance_request_validate", "maintenance_request_guard", 23),
        ("maintenance_requests", "maintenance_request_no_delete", "maintenance_history_no_rewrite", 11),
        ("maintenance_requests", "maintenance_request_no_truncate", "maintenance_history_no_rewrite", 34),
        ("maintenance_events", "maintenance_event_no_rewrite", "maintenance_history_no_rewrite", 27),
        ("maintenance_events", "maintenance_event_no_truncate", "maintenance_history_no_rewrite", 34),
        ("equipment_assets", "maintenance_asset_hold_validate", "maintenance_asset_hold_guard", 19),
        ("equipment_checkouts", "checkout_identifier_snapshots", "checkout_identifier_snapshots", 23),
    ] if revision else []
    if revision in {runner.C4, runner.HEAD}:
        triggers += [
            ("metadata_audit_events", "metadata_audit_no_rewrite", "metadata_audit_protect", 31),
            ("metadata_audit_events", "metadata_audit_no_truncate", "metadata_audit_protect", 34),
            ("equipment", "metadata_audit_equipment_change", "metadata_audit_equipment_change", 17),
            ("equipment_assets", "metadata_audit_asset_change", "metadata_audit_asset_change", 17),
        ]
    if revision == runner.HEAD:
        for table in ("inventory_audits", "inventory_audit_expected_assets", "inventory_audit_scans", "inventory_audit_discrepancies"):
            parent = table == "inventory_audits"
            triggers += [(table, "inventory_audit_parent_validate" if parent else "inventory_audit_child_validate",
                          "inventory_audit_parent_guard" if parent else "inventory_audit_child_guard", 23 if parent else 7),
                         (table, "inventory_audit_immutable", "inventory_audit_no_rewrite", 11 if parent else 27),
                         (table, "inventory_audit_no_truncate", "inventory_audit_no_rewrite", 34)]
    functions = []
    for name in sorted({t[2] for t in triggers}):
        generator = name in {"metadata_audit_equipment_change", "metadata_audit_asset_change"}
        owner = "inventory_metadata_audit_writer" if generator else (
            "inventory_metadata_audit_owner" if name == "metadata_audit_protect" else runner.MIGRATOR)
        path = "search_path=pg_catalog" if name.startswith("maintenance_") else "search_path=pg_catalog, pg_temp"
        functions.append((name, 0, owner, generator, [path]))
    rows = [(table, name, function, "O", "public", kind, 0,
             None, 0, False, False, 0, None, None, "",
             ["maintenance_hold"] if name == "maintenance_asset_hold_validate" else [])
            for table, name, function, kind in triggers]
    return functions, rows


def catalog_connection(revision):
    functions, triggers = guard_catalog(revision)
    c = Mock()
    c.dialect = postgresql.dialect()
    c.execute.side_effect = [Mock(all=Mock(return_value=functions)), Mock(all=Mock(return_value=triggers))]
    c.scalar.return_value = False  # read-only ACL/role checks: no unsafe conditions
    return c, functions, triggers


@pytest.mark.parametrize("revision", [None, runner.B7, runner.C4, runner.HEAD])
def test_complete_guard_catalog_without_skipping_acl_checks(revision):
    c, _, _ = catalog_connection(revision)
    runner.validate_guards(c, "public", revision)
    if revision in {runner.C4, runner.HEAD}:
        assert c.scalar.call_count >= 10  # real migration ACL validators ran
    assert "t.tgqual::text" in str(c.execute.call_args.args[0])


@pytest.mark.parametrize("field,value", [
    (7, "false"), (7, "true"), (8, 1), (9, True), (10, True), (11, 12),
    (12, "old_rows"), (13, "new_rows"), (14, "00"), (15, ["name"]),
    (3, "D"), (4, "other_schema"), (5, 23), (6, 1),
])
def test_rejects_altered_metadata_trigger_security_properties(field, value):
    c, _, triggers = catalog_connection(runner.C4)
    index = next(i for i, row in enumerate(triggers) if row[1] == "metadata_audit_equipment_change")
    altered = list(triggers[index])
    altered[field] = value
    triggers[index] = tuple(altered)
    with pytest.raises(runner.DeploymentError):
        runner.validate_guards(c, "public", runner.C4)


def test_catalog_acl_failure_is_not_bypassed():
    c, _, _ = catalog_connection(runner.C4)
    c.scalar.return_value = True
    with pytest.raises(RuntimeError, match="membership"):
        runner.validate_guards(c, "public", runner.C4)


@pytest.mark.parametrize("which", ["missing_function", "wrong_function_owner", "missing_trigger"])
def test_incomplete_guard_catalog_rejected(which):
    c, functions, triggers = catalog_connection(runner.C4)
    if which == "missing_function":
        functions.pop()
    elif which == "wrong_function_owner":
        row = list(functions[0]); row[2] = "inventory_app"; functions[0] = tuple(row)
    else:
        triggers.pop()
    with pytest.raises(runner.DeploymentError):
        runner.validate_guards(c, "public", runner.C4)
