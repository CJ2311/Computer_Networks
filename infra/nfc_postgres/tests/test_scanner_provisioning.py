"""Provisioning verification ONLY within the existing opt-in disposable harness."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.alembic.application_safety import verify_application
from backend.app.auth.scanners import ScannerCredential, token_digest
from infra.nfc_postgres.tests.test_scanner_ingestion import case, data, send, read, inventory_snapshot
from scripts import provision_equipment_scanner as cli


def apply(case, proposal):
    with case.admin.connect() as connection:
        case.h.verify_engine_identity(connection)
        connection.rollback()
        with connection.begin():
            return cli.apply_provisioning(connection, case.h.settings, proposal)


def row(case, scanner_id):
    with case.admin.connect() as c:
        case.h.verify_engine_identity(c)
        return dict(c.execute(text("SELECT * FROM equipment_scanners WHERE id=:id"),
                              {"id": scanner_id}).mappings().one())


def events(case):
    with case.admin.connect() as c:
        case.h.verify_engine_identity(c)
        return c.execute(text("SELECT to_jsonb(e)::text FROM scanner_scan_events e ORDER BY scanner_id,sequence")).scalars().all()


def test_registration_and_sessions_use_existing_ingestion_and_preserve_inventory(case):
    before = inventory_snapshot(case)
    registration = apply(case, cli.Request("register"))
    stored = row(case, registration.scanner_id)
    assert stored["secret_sha256"] == token_digest(registration.secret)
    assert stored["enabled"] and stored["last_sequence"] == stored["request_count"] == 0
    assert stored["capture_session_id"] is None
    capture = apply(case, cli.Request("start-session", registration.scanner_id, case.owner.id))
    stored = row(case, registration.scanner_id)
    assert stored["capture_secret_sha256"] == token_digest(capture.secret)
    assert stored["secret_sha256"] != stored["capture_secret_sha256"]
    assert stored["capture_expires_at"] - stored["capture_started_at"] == timedelta(minutes=15)
    device = ScannerCredential(registration.scanner_id, token_digest(registration.secret))
    receipt = send(case, data(case, session_id=capture.session_id), device)
    assert receipt["status"] == 200
    page = read(case, session_id=capture.session_id, secret=capture.secret)
    assert page["status"] == 200 and page["data"].items[0].asset_id == case.asset.id
    assert inventory_snapshot(case) == before


def test_renewal_invalidates_old_context_preserves_counters_events_and_revoke(case):
    assert send(case, data(case))["status"] == 200
    before = row(case, case.scanner.id)
    historical_events = events(case)
    inventory = inventory_snapshot(case)
    renewed = apply(case, cli.Request("renew-session", case.scanner.id, case.other.id,
                                      case.scanner.capture_session_id))
    current = row(case, case.scanner.id)
    for key in ("last_sequence", "request_count", "window_started_at", "secret_sha256"):
        assert current[key] == before[key]
    assert current["capture_session_id"] != before["capture_session_id"]
    assert current["capture_secret_sha256"] != before["capture_secret_sha256"]
    assert events(case) == historical_events
    assert read(case)["status"] == 404
    assert send(case, data(case, sequence=2))["status"] == 409
    # Rejected old-session payloads intentionally consume their sequence too.
    assert send(case, data(case, sequence=3, uid="11223344", session_id=renewed.session_id))["status"] == 200
    assert read(case, actor=case.other, session_id=renewed.session_id, secret=renewed.secret)["status"] == 200
    historical_events = events(case)
    before_revoke = row(case, case.scanner.id)
    apply(case, cli.Request("revoke", case.scanner.id))
    revoked = row(case, case.scanner.id)
    assert not revoked["enabled"]
    assert all(revoked[key] is None for key in ("capture_session_id", "capture_user_id", "capture_secret_sha256",
                                                "capture_started_at", "capture_expires_at"))
    for key in ("last_sequence", "request_count", "window_started_at"):
        assert revoked[key] == before_revoke[key]
    assert send(case, data(case, sequence=4, session_id=renewed.session_id))["status"] == 401
    assert read(case, actor=case.other, session_id=renewed.session_id, secret=renewed.secret)["status"] == 404
    assert events(case) == historical_events and inventory_snapshot(case) == inventory


@pytest.mark.parametrize("role,active", [("FACULTY", True), ("LAB_ASSISTANT", True), ("STUDENT", True),
                                         ("ADMIN", True), ("FACULTY", False)])
def test_existing_active_staff_only(case, role, active):
    with case.admin.begin() as c:
        case.h.verify_engine_identity(c)
        c.execute(text("UPDATE users SET role=:role,is_active=:active WHERE id=:id"),
                  {"role": role, "active": active, "id": case.other.id})
    request = cli.Request("renew-session", case.scanner.id, case.other.id, case.scanner.capture_session_id)
    before = row(case, case.scanner.id)
    if active and role in {"FACULTY", "LAB_ASSISTANT"}:
        assert apply(case, request).staff_user_id == case.other.id
    else:
        with pytest.raises(cli.ProvisioningError):
            apply(case, request)
        assert row(case, case.scanner.id) == before


def test_missing_staff_scanner_stale_renewal_and_revoked_states_rollback(case):
    before = row(case, case.scanner.id)
    proposals = [cli.Request("revoke", uuid4()),
                 cli.Request("renew-session", case.scanner.id, uuid4(), case.scanner.capture_session_id),
                 cli.Request("renew-session", case.scanner.id, case.owner.id, uuid4()),
                 cli.Request("start-session", case.scanner.id, case.owner.id)]
    for proposal in proposals:
        with pytest.raises(cli.ProvisioningError):
            apply(case, proposal)
        assert row(case, case.scanner.id) == before
    apply(case, cli.Request("revoke", case.scanner.id))
    with pytest.raises(cli.ProvisioningError):
        apply(case, cli.Request("start-session", case.scanner.id, case.owner.id))


def test_transaction_abort_discards_registration(case):
    class Abort(RuntimeError):
        pass
    with pytest.raises(Abort):
        with case.admin.begin() as c:
            case.h.verify_engine_identity(c)
            pending = cli.apply_provisioning(c, case.h.settings, cli.Request("register"))
            raise Abort()
    with case.admin.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM equipment_scanners WHERE id=:id"), {"id": pending.scanner_id}) == 0


def test_concurrent_provisioning_fails_fast_and_stale_operator_cannot_overwrite(case):
    proposal = cli.Request("renew-session", case.scanner.id, case.owner.id, case.scanner.capture_session_id)
    with case.admin.begin() as first:
        case.h.verify_engine_identity(first)
        pending = cli.apply_provisioning(first, case.h.settings, proposal)
        # A separate live transaction contends for the same deployment lock.
        with pytest.raises(cli.baseline.DeploymentError):
            apply(case, proposal)
    with pytest.raises(cli.ProvisioningError):
        apply(case, proposal)
    assert row(case, case.scanner.id)["capture_session_id"] == pending.session_id


def test_dry_run_preserves_binding_and_all_privilege_guards(case):
    before = row(case, case.scanner.id)
    result = apply(case, cli.Request("renew-session", case.scanner.id, case.owner.id,
                                    case.scanner.capture_session_id, dry_run=True))
    assert result.secret is None and result.dry_run
    assert row(case, case.scanner.id) == before
    with case.admin.connect() as c:
        verify_application(c, case.h.settings)
    for statement in ("SELECT * FROM equipment_scanners", "SELECT * FROM scanner_scan_events",
                      "UPDATE equipment_scanners SET enabled=false", "DELETE FROM scanner_scan_events",
                      "INSERT INTO equipment_scanners(id,secret_sha256) VALUES (:id,:digest)"):
        with pytest.raises(DBAPIError):
            with case.runtime.begin() as c:
                case.h.verify_engine_identity(c)
                c.execute(text(statement), {"id": uuid4(), "digest": "a" * 64})
    with pytest.raises(cli.baseline.DeploymentError):
        with case.runtime.begin() as c:
            cli.apply_provisioning(c, replace(case.h.settings, administrator="inventory_app"), cli.Request("register"))
