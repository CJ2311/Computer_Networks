"""Provisioning contracts with fake connections only; never open a database."""
import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from scripts import provision_equipment_scanner as cli

SETTINGS = cli.baseline.Settings("inventory_nfc_demo", "127.0.0.1", 5432, "postgres")


@pytest.fixture
def rig(monkeypatch):
    now = datetime.now(timezone.utc)
    scanner = {"id": uuid4(), "enabled": True, "capture_session_id": None,
               "capture_expires_at": None, "last_sequence": 17, "request_count": 9}
    staff = {"role": "FACULTY", "is_active": True}
    calls, events = [], []
    connection = MagicMock()
    connection.in_transaction.return_value = True
    def execute(statement, parameters=None):
        sql = str(statement)
        calls.append((sql, parameters))
        result = MagicMock()
        if "SELECT * FROM" in sql:
            result.mappings.return_value.one_or_none.return_value = scanner
        elif "SELECT role, is_active" in sql:
            result.mappings.return_value.one_or_none.return_value = staff
        elif "clock_timestamp()" in sql:
            result.scalar_one.return_value = now
        elif "SELECT EXISTS" in sql:
            result.scalar_one.return_value = False
        return result
    connection.execute.side_effect = execute
    @contextmanager
    def begin():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    connection.begin.side_effect = begin
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    check = Mock()
    verify = Mock()
    lock = Mock()
    monkeypatch.setattr(cli.baseline, "check_connection", check)
    monkeypatch.setattr(cli.baseline, "acquire_stage_lock", lock)
    monkeypatch.setattr(cli, "verify_application", verify)
    return SimpleNamespace(c=connection, engine=engine, scanner=scanner, staff=staff,
                           calls=calls, events=events, now=now, check=check, verify=verify, lock=lock)


def invoke(rig, request):
    return cli.provision(SETTINGS, request, engine_factory=lambda *a: rig.engine,
                         prompt=lambda _: "synthetic-password", confirm=lambda message: message[5:-13])


def test_registration_stores_only_canonical_digest_after_commit(rig):
    result = invoke(rig, cli.Request("register"))
    assert rig.events == ["begin", "commit"]
    assert len(result.secret) == 43
    assert len(base64.urlsafe_b64decode(result.secret + "=")) == 32
    assert base64.urlsafe_b64encode(base64.urlsafe_b64decode(result.secret + "=")).rstrip(b"=").decode() == result.secret
    inserts = [(sql, p) for sql, p in rig.calls if sql.startswith("INSERT")]
    assert len(inserts) == 1
    assert inserts[0][1] == {"id": result.scanner_id, "digest": cli.token_digest(result.secret)}
    assert result.secret not in repr(rig.calls) and result.secret not in repr(result)
    # Schema defaults initialize sequence/rate counters; no extra DML or grants.
    assert all(not sql.startswith(("UPDATE", "DELETE", "GRANT", "ALTER")) for sql, _ in rig.calls)
    assert rig.verify.call_count == rig.check.call_count == 2
    rig.lock.assert_called_once_with(rig.c)
    rig.engine.dispose.assert_called_once()


@pytest.mark.parametrize("role", ["FACULTY", "LAB_ASSISTANT"])
def test_session_creation_has_independent_credential_and_bounded_lease(rig, role):
    rig.staff["role"] = role
    staff_id = uuid4()
    result = invoke(rig, cli.Request("start-session", rig.scanner["id"], staff_id, minutes=7))
    assert result.session_id and result.staff_user_id == staff_id
    assert result.expires_at == rig.now + timedelta(minutes=7)
    update, parameters = next((s, p) for s, p in rig.calls if s.startswith("UPDATE"))
    assert parameters["digest"] == cli.token_digest(result.secret)
    assert parameters["session"] == result.session_id
    assert "last_sequence" not in update and "request_count" not in update
    assert "FOR UPDATE" in rig.calls[0][0] and any("FOR SHARE" in s for s, _ in rig.calls)


@pytest.mark.parametrize("role,active", [("ADMIN", True), ("STUDENT", True), ("FACULTY", False), ("LAB_ASSISTANT", False)])
def test_ineligible_staff_rolls_back_without_dml(rig, role, active):
    rig.staff.update(role=role, is_active=active)
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, cli.Request("start-session", rig.scanner["id"], uuid4()))
    assert rig.events == ["begin", "rollback"]
    assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s, _ in rig.calls)


def test_renewal_replaces_binding_without_resetting_counters_or_events(rig):
    old_id = uuid4()
    rig.scanner.update(capture_session_id=old_id, capture_expires_at=rig.now + timedelta(minutes=1))
    result = invoke(rig, cli.Request("renew-session", rig.scanner["id"], uuid4(), old_id))
    assert result.session_id != old_id
    update = next(s for s, _ in rig.calls if s.startswith("UPDATE"))
    assert "last_sequence" not in update and "window_started_at" not in update and "request_count" not in update
    assert not any("scanner_scan_events" in s for s, _ in rig.calls)


@pytest.mark.parametrize("operation", ["start-session", "renew-session"])
def test_active_or_stale_binding_cannot_be_overwritten(rig, operation):
    rig.scanner.update(capture_session_id=uuid4(), capture_expires_at=rig.now + timedelta(minutes=1))
    request = cli.Request(operation, rig.scanner["id"], uuid4(), uuid4() if operation == "renew-session" else None)
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, request)
    assert rig.events[-1] == "rollback"


def test_revocation_clears_every_capture_field_only(rig):
    result = invoke(rig, cli.Request("revoke", rig.scanner["id"]))
    assert result.secret is None
    sql, parameters = next((s, p) for s, p in rig.calls if s.startswith("UPDATE"))
    for name in ("capture_session_id", "capture_user_id", "capture_secret_sha256", "capture_started_at", "capture_expires_at"):
        assert name + "=NULL" in sql
    assert "enabled=false" in sql and parameters == {"id": rig.scanner["id"]}
    assert "last_sequence" not in sql and not any(s.startswith("DELETE") for s, _ in rig.calls)


@pytest.mark.parametrize("operation", ["register", "start-session", "revoke"])
def test_dry_run_is_read_only_and_generates_no_secrets(rig, monkeypatch, operation):
    generate = Mock(side_effect=AssertionError("must not generate"))
    monkeypatch.setattr(cli.secrets, "token_urlsafe", generate)
    request = cli.Request(operation, rig.scanner["id"] if operation != "register" else None,
                          uuid4() if operation == "start-session" else None, dry_run=True)
    result = invoke(rig, request)
    assert result.dry_run and result.secret is None
    assert rig.calls[0][0] == "SET TRANSACTION READ ONLY"
    assert not any("FOR UPDATE" in s or "FOR SHARE" in s or s.startswith(("UPDATE", "INSERT", "DELETE")) for s, _ in rig.calls)
    generate.assert_not_called()


@pytest.mark.parametrize("proposal", [
    cli.Request("bad"), cli.Request("register", uuid4()), cli.Request("revoke"),
    cli.Request("revoke", "invalid"), cli.Request("start-session", uuid4()),
    cli.Request("renew-session", uuid4(), uuid4()), cli.Request("register", minutes=0),
    cli.Request("register", minutes=16), cli.Request("register", minutes=True),
    cli.Request("revoke", uuid4(), uuid4()),
])
def test_invalid_requests_fail_before_engine_creation(proposal):
    factory = Mock()
    with pytest.raises(cli.ProvisioningError):
        cli.provision(SETTINGS, proposal, engine_factory=factory)
    factory.assert_not_called()


@pytest.mark.parametrize("settings", [replace(SETTINGS, database="inventory_db"), replace(SETTINGS, server_address="192.0.2.1"),
    replace(SETTINGS, administrator="inventory_app"), replace(SETTINGS, server_address="localhost"),
    replace(SETTINGS, schema='public"; DROP TABLE users;--')])
def test_unsafe_targets_fail_without_prompt_or_connection(settings):
    factory, prompt, confirm = Mock(), Mock(), Mock()
    with pytest.raises(cli.baseline.DeploymentError):
        cli.provision(settings, cli.Request("register"), engine_factory=factory, prompt=prompt, confirm=confirm)
    factory.assert_not_called(); prompt.assert_not_called(); confirm.assert_not_called()


def test_denied_confirmation_never_prompts_password_or_connects():
    prompt, factory = Mock(), Mock()
    with pytest.raises(cli.ProvisioningError):
        cli.provision(SETTINGS, cli.Request("register"), engine_factory=factory, prompt=prompt, confirm=lambda _: "NO")
    prompt.assert_not_called(); factory.assert_not_called()


def test_verification_failure_rolls_back_and_withholds_result(rig, capsys):
    rig.verify.side_effect = [None, RuntimeError("synthetic-sensitive-details")]
    with pytest.raises(cli.ProvisioningError) as error:
        invoke(rig, cli.Request("register"))
    assert "synthetic-sensitive-details" not in str(error.value)
    assert rig.events == ["begin", "rollback"]
    assert capsys.readouterr().out == ""


def test_commit_acknowledgement_failure_never_returns_credential(rig, capsys):
    @contextmanager
    def fail_commit():
        yield
        raise RuntimeError("synthetic-commit-detail")
    rig.c.begin.side_effect = fail_commit
    with pytest.raises(cli.ProvisioningError) as error:
        invoke(rig, cli.Request("register"))
    assert "synthetic-commit-detail" not in str(error.value)
    assert capsys.readouterr().out == ""
    rig.engine.dispose.assert_called_once()


def test_deployment_lock_contention_aborts_before_registry_work(rig):
    rig.lock.side_effect = cli.baseline.DeploymentError("Another NFC deployment stage holds the lock")
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, cli.Request("register"))
    assert rig.events == ["begin", "rollback"]
    assert rig.calls == []
    rig.verify.assert_not_called()


@pytest.mark.parametrize("missing", ["scanner", "staff"])
def test_missing_identity_rolls_back_without_writes(rig, missing):
    original = rig.c.execute.side_effect
    def execute(statement, parameters=None):
        result = original(statement, parameters)
        if (missing == "scanner" and "SELECT * FROM" in str(statement)
                or missing == "staff" and "SELECT role, is_active" in str(statement)):
            result.mappings.return_value.one_or_none.return_value = None
        return result
    rig.c.execute.side_effect = execute
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, cli.Request("start-session", rig.scanner["id"], uuid4()))
    assert rig.events[-1] == "rollback"
    assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) for s, _ in rig.calls)


def test_revoked_scanner_cannot_start_session(rig):
    rig.scanner["enabled"] = False
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, cli.Request("start-session", rig.scanner["id"], uuid4()))
    assert rig.events[-1] == "rollback"


def test_expired_binding_can_be_replaced_by_start(rig):
    old_id = uuid4()
    rig.scanner.update(capture_session_id=old_id, capture_expires_at=rig.now - timedelta(seconds=1))
    result = invoke(rig, cli.Request("start-session", rig.scanner["id"], uuid4()))
    assert result.session_id != old_id


def test_database_write_error_rolls_back_without_rendering_driver_details(rig, capsys):
    original = rig.c.execute.side_effect
    def execute(statement, parameters=None):
        if str(statement).startswith("INSERT"):
            raise RuntimeError("synthetic-driver-details")
        return original(statement, parameters)
    rig.c.execute.side_effect = execute
    with pytest.raises(cli.ProvisioningError) as error:
        invoke(rig, cli.Request("register"))
    assert "synthetic-driver-details" not in str(error.value)
    assert capsys.readouterr().out == "" and rig.events[-1] == "rollback"


def test_main_refuses_redirected_terminal_without_provisioning(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    provision = Mock()
    monkeypatch.setattr(cli, "provision", provision)
    args = ["register", "--database", SETTINGS.database, "--server-address", "127.0.0.1",
            "--port", "5432", "--administrator", "postgres", "--schema", "public"]
    assert cli.main(args) == 1
    provision.assert_not_called()
    assert "Details withheld" in capsys.readouterr().err


def test_credential_collisions_are_bounded_and_rollback(rig):
    original = rig.c.execute.side_effect
    def execute(statement, parameters=None):
        result = original(statement, parameters)
        if "SELECT EXISTS" in str(statement):
            result.scalar_one.return_value = True
        return result
    rig.c.execute.side_effect = execute
    with pytest.raises(cli.ProvisioningError):
        invoke(rig, cli.Request("register"))
    assert sum("SELECT EXISTS" in s for s, _ in rig.calls) == 4
    assert rig.events[-1] == "rollback"


def test_parser_errors_do_not_echo_unrecognized_secret(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--synthetic-secret-do-not-display"])
    assert error.value.code == 2
    assert "synthetic-secret-do-not-display" not in capsys.readouterr().err


def test_main_emits_secret_once_and_only_after_provision_returns(monkeypatch, capsys):
    for stream in (cli.sys.stdin, cli.sys.stdout, cli.sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    result = cli.Result("register", uuid4(), secret="synthetic-once-only")
    def committed(*args):
        assert "synthetic-once-only" not in capsys.readouterr().out
        return result
    monkeypatch.setattr(cli, "provision", committed)
    args = ["register", "--database", SETTINGS.database, "--server-address", "127.0.0.1",
            "--port", "5432", "--administrator", "postgres", "--schema", "public"]
    assert cli.main(args) == 0
    assert capsys.readouterr().out.count(result.secret) == 1
