"""Timing-migration source/DDL contracts only: no SQL reaches a database."""
from importlib import import_module
from unittest.mock import Mock, call

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy.dialects import postgresql

from backend.alembic import application_safety, student_nfc_safety
from scripts import deploy_nfc_demo as baseline

timing = import_module("backend.alembic.application_versions.f6c1a8d420be_student_nfc_lock_time")
previous = import_module("backend.alembic.versions.e4b7c9d210af_student_nfc")


def test_fresh_time_is_sampled_after_both_lock_paths_before_deadline_checks():
    body = timing.routine_body("inventory")
    assert "at_time timestamptz :=" not in body
    assert body.count("at_time := clock_timestamp();") == 2
    init_lock = body.index("WHERE student_id=v_student FOR UPDATE;")
    init_clock = body.index("at_time := clock_timestamp();")
    pending_reuse = body.index("r.expires_at>at_time")
    assert init_lock < init_clock < pending_reuse
    verify_lock = body.index("WHERE id=p_id FOR UPDATE;")
    verify_clock = body.index("at_time := clock_timestamp();", init_clock + 1)
    expiry_check = body.index("r.expires_at<=at_time")
    assert verify_lock < verify_clock < expiry_check
    assert body.index("RETURN jsonb_build_object('status',404);", verify_lock) < verify_clock


def test_expiry_and_replay_rejections_remain_before_activation():
    body = timing.routine_body("inventory")
    expiry = body.index("r.expires_at<=at_time")
    failed = body.index("SET status='FAILED'")
    pending_only = body.index("IF r.status<>'PENDING_WRITE' THEN RETURN jsonb_build_object('status',409);")
    activation = body.index("SET status='ACTIVE',verified_at=at_time")
    assert expiry < failed < pending_only < activation
    assert body.index("r.payload_sha256 IS DISTINCT FROM") < activation
    assert body.index("r.card_uid IS DISTINCT FROM") < activation
    assert body.index("r.write_profile IS DISTINCT FROM") < activation
    assert body.index("r.scanner_id IS DISTINCT FROM") < expiry
    # Exact equality also expires: neither a stale clock nor a '<' comparison.
    assert "r.expires_at<=at_time" in body


def test_only_clock_placement_changes_authorization_and_challenge_guards():
    old = previous.routine_body('"inventory"')
    new = timing.routine_body('"inventory"')
    restored = new.replace("at_time timestamptz;", "at_time timestamptz := clock_timestamp();", 1)
    restored = restored.replace("        at_time := clock_timestamp();\n", "", 1)
    restored = restored.replace("    at_time := clock_timestamp();\n", "", 1)
    assert restored == old
    assert timing.OWNER == previous.OWNER and timing.SIGNATURE == previous.SIGNATURE


def test_unexpected_predecessor_body_cannot_silently_install_partial_fix(monkeypatch):
    monkeypatch.setattr(timing.PREVIOUS, "routine_body", lambda _: "unexpected routine")
    with pytest.raises(RuntimeError, match="Unexpected student NFC predecessor"):
        timing.routine_body("inventory")


@pytest.fixture
def ddl(monkeypatch):
    operations = Mock()
    c = operations.get_bind.return_value
    c.dialect = postgresql.dialect()
    c.scalar.side_effect = [True, "inventory"]
    verify = Mock()
    monkeypatch.setattr(timing, "op", operations)
    monkeypatch.setattr(student_nfc_safety, "verify_student_nfc", verify)
    return operations, c, verify


def test_mocked_migration_replaces_only_function_and_verifies_before_and_after(ddl):
    operations, c, verify = ddl
    timing.upgrade()  # Mock Alembic operations: no engine, connection or DDL execution.
    operations.execute.assert_called_once()
    statement = str(operations.execute.call_args.args[0])
    assert statement.startswith("CREATE OR REPLACE FUNCTION inventory.student_nfc_manage(")
    assert "SECURITY DEFINER SET search_path=pg_catalog,pg_temp" in statement
    assert "$studentnfc$" + timing.routine_body("inventory") + "$studentnfc$" in statement
    assert verify.call_args_list == [call(c, "inventory", routine_migration=previous), call(c, "inventory")]
    c.commit.assert_not_called(); c.rollback.assert_not_called()


def test_failed_preflight_prevents_replacement(ddl):
    operations, _, verify = ddl
    verify.side_effect = RuntimeError("Missing or altered student NFC routine")
    with pytest.raises(RuntimeError, match="altered student NFC"):
        timing.upgrade()
    operations.execute.assert_not_called()


@pytest.mark.parametrize("responses", [[False], [None], [True, "pg_catalog"], [True, "public;other"], [True, None]])
def test_unsafe_deployment_identity_fails_before_any_change(ddl, responses):
    operations, c, verify = ddl
    c.scalar.side_effect = responses
    with pytest.raises(RuntimeError):
        timing.upgrade()
    verify.assert_not_called(); operations.execute.assert_not_called()


def test_non_postgresql_is_rejected_before_queries(ddl):
    operations, c, _ = ddl
    c.dialect.name = "sqlite"
    with pytest.raises(RuntimeError, match="trusted deployment administrator"):
        timing.upgrade()
    c.scalar.assert_not_called(); operations.execute.assert_not_called()


def test_default_deployment_verifier_binds_new_body_and_rejects_stale_routine():
    c = Mock()
    c.scalar.side_effect = [False, True]
    with pytest.raises(RuntimeError, match="Missing or altered student NFC routine"):
        student_nfc_safety.verify_student_nfc(c, '"inventory"')
    _, parameters = c.scalar.call_args.args
    assert parameters["body"] == timing.routine_body('"inventory"')
    assert parameters["body"] != previous.routine_body('"inventory"')


def test_forward_revision_extends_merge_without_changing_demo_target():
    graph = ScriptDirectory.from_config(Config("alembic_nfc_application.ini"))
    assert graph.get_current_head() == timing.revision == application_safety.HEAD
    assert timing.down_revision == "a9c2e4f610bd"
    assert timing.down_revision in application_safety.PREVIOUS_REVISIONS
    assert baseline.HEAD == "d6f2b8a931c0"
    assert baseline.STAGES[-1][1] == "d6f2b8a931c0"
    with pytest.raises(RuntimeError, match="forward migration"):
        timing.downgrade()
