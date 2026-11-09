"""Database-free unit tests for application migration runner safety and incremental upgrades."""

from contextlib import nullcontext
from unittest.mock import MagicMock, Mock
import pytest
from sqlalchemy import text

from backend.alembic.application_safety import (
    HEAD,
    PREVIOUS_REVISIONS,
    apply_application,
)
from scripts import deploy_nfc_demo as baseline


@pytest.fixture
def migration_harness(monkeypatch):
    connection = MagicMock()
    connection.dialect.name = "postgresql"
    connection.dialect.identifier_preparer.quote_schema.return_value = "public"
    connection.begin.return_value = nullcontext()

    settings = Mock()
    settings.schema = "public"
    settings.administrator = "postgres"
    settings.validate.return_value = None

    checkpoint_mock = Mock()
    check_conn_mock = Mock()
    upgrade_mock = Mock()
    verify_mock = Mock()

    monkeypatch.setattr(baseline, "acquire_stage_lock", Mock())
    monkeypatch.setattr(baseline, "checkpoint", checkpoint_mock)
    monkeypatch.setattr(baseline, "check_connection", check_conn_mock)
    monkeypatch.setattr(baseline, "identity", Mock(return_value={"database": "test"}))
    monkeypatch.setattr("alembic.command.upgrade", upgrade_mock)
    monkeypatch.setattr("backend.alembic.application_safety.verify_application", verify_mock)

    return {
        "connection": connection,
        "settings": settings,
        "checkpoint": checkpoint_mock,
        "check_conn": check_conn_mock,
        "upgrade": upgrade_mock,
        "verify": verify_mock,
    }


def test_apply_application_from_baseline_success(migration_harness):
    c = migration_harness["connection"]
    # Database is at NFC baseline d6f2b8a931c0
    c.execute.return_value.scalars.return_value.all.return_value = [baseline.HEAD]

    apply_application(c, migration_harness["settings"])

    migration_harness["checkpoint"].assert_called_once_with(c, migration_harness["settings"])
    migration_harness["upgrade"].assert_called_once()
    assert migration_harness["upgrade"].call_args[0][1] == HEAD
    migration_harness["verify"].assert_called_once_with(c, migration_harness["settings"])


def test_apply_application_from_previous_revision_success(migration_harness):
    c = migration_harness["connection"]
    prev_rev = list(PREVIOUS_REVISIONS)[0]
    # Database is at intermediate approved revision e8a4c2d190ab
    c.execute.return_value.scalars.return_value.all.return_value = [prev_rev]

    apply_application(c, migration_harness["settings"])

    # Does not checkpoint NFC baseline again, upgrades to HEAD
    migration_harness["checkpoint"].assert_not_called()
    migration_harness["upgrade"].assert_called_once()
    assert migration_harness["upgrade"].call_args[0][1] == HEAD
    migration_harness["verify"].assert_called_once_with(c, migration_harness["settings"])


def test_apply_application_idempotent_at_head(migration_harness):
    c = migration_harness["connection"]
    # Database is already at HEAD c7d3e91024ab
    c.execute.return_value.scalars.return_value.all.return_value = [HEAD]

    apply_application(c, migration_harness["settings"])

    # No migration command executed; only connection check and schema verification
    migration_harness["checkpoint"].assert_not_called()
    migration_harness["upgrade"].assert_not_called()
    migration_harness["verify"].assert_called_once_with(c, migration_harness["settings"])


@pytest.mark.parametrize("invalid_revisions", [
    [],
    ["unknown_revision_abc"],
    ["c4e8a17d92f6"],  # Pre-baseline revision
    ["b7d2f90c4e61"],
    [baseline.HEAD, "e8a4c2d190ab"],  # Divergent multiple revisions
    ["e8a4c2d190ab", "other_head"],
])
def test_apply_application_rejects_invalid_or_divergent_revisions(migration_harness, invalid_revisions):
    c = migration_harness["connection"]
    c.execute.return_value.scalars.return_value.all.return_value = invalid_revisions

    with pytest.raises(RuntimeError, match="predecessor revision"):
        apply_application(c, migration_harness["settings"])

    migration_harness["checkpoint"].assert_not_called()
    migration_harness["upgrade"].assert_not_called()
    migration_harness["verify"].assert_not_called()
