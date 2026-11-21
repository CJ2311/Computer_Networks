"""Prepared integration checks; run only through the opt-in disposable worker."""
from datetime import datetime, timedelta, timezone
from importlib import import_module

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session
import pytest

from backend.alembic.application_safety import HEAD, apply_application, verify_application
from backend.app.schemas.checkout import ReturnCreate
from backend.app.services.checkout import create_checkout, record_return
from infra.nfc_postgres.harness import ADMIN
from infra.nfc_postgres.tests.test_scanner_ingestion import case, seed_case, inventory_snapshot
from infra.nfc_postgres.tests.test_student_nfc import start, verify
from scripts import deploy_nfc_demo as baseline


def test_populated_merge_head_upgrade_preserves_borrower_checkout_and_pending_nfc(database, monkeypatch):
    import base64
    import hashlib
    import json
    from uuid import uuid4
    from backend.app.auth.student_nfc import StudentScanner

    database.deploy()
    admin = database.engine(ADMIN)
    with admin.connect() as connection:
        database.verify_engine_identity(connection)
        connection.rollback()
        config = Config('alembic_nfc_application.ini')
        config.attributes.update(connection=connection,
                                 nfc_demo_identity=baseline.identity(database.settings, ADMIN))
        command.upgrade(config, 'a9c2e4f610bd')
        connection.commit()
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all() == ['a9c2e4f610bd']
    case = seed_case(database, admin)
    case.student_scanner = StudentScanner(uuid4(), hashlib.sha256(b'synthetic-device').hexdigest())
    monkeypatch.setenv('STUDENT_NFC_PAYLOAD_KEY', base64.urlsafe_b64encode(b'p'*32).rstrip(b'=').decode())
    monkeypatch.setenv('STUDENT_NFC_WRITE_PROFILE', 'SYNTHETIC-TEST-AREA')
    monkeypatch.setenv('STUDENT_NFC_SCANNER_DIGESTS', json.dumps({str(case.student_scanner.id): case.student_scanner.credential_digest}))
    _, pending = start(case)
    with Session(case.runtime) as db:
        loan = create_checkout(db, case.asset.id, pending.student_id, case.owner,
                               datetime.now(timezone.utc)+timedelta(days=1), notes='Before timing migration')
    before = inventory_snapshot(case)
    with admin.connect() as connection:
        cards = connection.execute(text('SELECT to_jsonb(r)::text FROM student_nfc_cards r ORDER BY id')).scalars().all()
        connection.rollback()
        apply_application(connection, database.settings)
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all() == [HEAD]
        assert connection.execute(text('SELECT to_jsonb(r)::text FROM student_nfc_cards r ORDER BY id')).scalars().all() == cards
        verify_application(connection, database.settings)
    assert inventory_snapshot(case) == before
    assert verify(case, pending)['status'] == 200
    assert verify(case, pending)['status'] == 409
    with Session(case.runtime) as db:
        returned = record_return(db, loan.id, case.owner, ReturnCreate(condition_status='WORKING'))
    assert returned.status == 'RETURNED'
    assert returned.student_id == pending.student_id


def test_runtime_maintenance_lifecycle_and_reporting(case):
    from backend.app.services.maintenance import (
        report_maintenance_issue, assign_maintenance_request, start_repair,
        submit_repair_for_verification, verify_repair,
    )
    from backend.app.services.reports import get_maintenance_report

    with Session(case.runtime) as db:
        request = report_maintenance_issue(db, case.asset.id, case.owner, 'DAMAGE', 'Synthetic damaged connector')
    with Session(case.runtime) as db:
        request = assign_maintenance_request(db, request.id, case.owner, case.other.id, request.version)
    with Session(case.runtime) as db:
        request = start_repair(db, request.id, case.other, request.version)
    with Session(case.runtime) as db:
        request = submit_repair_for_verification(db, request.id, case.other, request.version, 'Connector repaired')
    with Session(case.runtime) as db:
        active = get_maintenance_report(db)
        assert active.total_active_count == 1
        assert active.items[0].request_id == request.id
        assert active.items[0].status == 'AWAITING_VERIFICATION'
    with Session(case.runtime) as db:
        completed = verify_repair(db, request.id, case.owner, request.version, 'PASS', 'Independent check passed')
    assert completed.status == 'COMPLETED' and not completed.maintenance_hold
    with Session(case.runtime) as db:
        report = get_maintenance_report(db, status_filter='COMPLETED')
        assert report.total_active_count == 0 and report.total == 1
        assert report.items[0].request_id == request.id
        assert report.items[0].current_asset_availability == 'AVAILABLE'


def test_deployment_verifier_rejects_restored_pre_lock_timing_body(case):
    previous = import_module('backend.alembic.versions.e4b7c9d210af_student_nfc')
    with case.admin.begin() as connection:
        case.h.verify_engine_identity(connection)
        q = connection.dialect.identifier_preparer.quote_schema(case.h.settings.schema)
        connection.exec_driver_sql(f'''CREATE OR REPLACE FUNCTION {q}.student_nfc_manage(
            p_action text,p_actor uuid,p_id uuid,p_data jsonb)
            RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
            AS $studentnfc${previous.routine_body(q)}$studentnfc$''')
    with case.admin.connect() as connection:
        with pytest.raises(RuntimeError):
            verify_application(connection, case.h.settings)
