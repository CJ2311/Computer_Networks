"""Database-free structural regression checks for the additive snapshot revision."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import CheckConstraint
from backend.app.models.checkout import EquipmentCheckout


def load_revision():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/b7d2f90c4e61_checkout_identifier_snapshots.py"
    spec = importlib.util.spec_from_file_location("snapshot_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_snapshot_revision_is_additive_and_model_check_matches(monkeypatch):
    revision = load_revision()
    assert revision.down_revision == "a3c7e91b6d20"
    operations = Mock()
    monkeypatch.setattr(revision, "op", operations)
    monkeypatch.setattr(revision, "_schema", lambda: '"isolated_test"')
    revision.upgrade()
    assert [call.args[1].name for call in operations.add_column.call_args_list] == [
        "equipment_name_at_checkout", "asset_code_at_checkout", "identifier_snapshot_status",
    ]
    operations.alter_column.assert_called_once_with(
        "equipment_checkouts", "identifier_snapshot_status", server_default=None)
    check = next(c for c in EquipmentCheckout.__table__.constraints
                 if isinstance(c, CheckConstraint) and c.name == "ck_checkout_identifier_snapshots")
    assert str(check.sqltext) == revision.SNAPSHOT_CHECK
    ddl = str(operations.execute.call_args.args[0])
    assert "SECURITY INVOKER" in ddl and "FOR SHARE" in ddl
    assert 'FROM "isolated_test".equipment_assets' in ddl
    assert 'FROM "isolated_test".equipment AS' in ddl
    assert "IS DISTINCT FROM OLD.equipment_name_at_checkout" in ddl
    assert "ERRCODE = '55000'" in ddl and "ERRCODE = '42501'" in ddl
    assert "UPDATE equipment_checkouts" not in ddl


def test_snapshot_downgrade_checks_history_under_exclusive_lock(monkeypatch):
    revision = load_revision()
    operations = Mock()
    monkeypatch.setattr(revision, "op", operations)
    monkeypatch.setattr(revision, "_schema", lambda: '"isolated_test"')
    revision.downgrade()
    sql = [str(call.args[0]) for call in operations.execute.call_args_list]
    assert "ACCESS EXCLUSIVE" in sql[0]
    assert "EXISTS" in sql[1] and "ERRCODE = '55000'" in sql[1]
    assert all("CASCADE" not in statement for statement in sql)
    assert operations.drop_column.call_count == 3
