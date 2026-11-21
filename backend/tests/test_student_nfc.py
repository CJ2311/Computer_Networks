"""Student NFC contracts and services: fake sessions only, never host databases."""
import base64
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import import_module
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.orm import Session
from starlette.requests import Request

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.student_nfc import StudentScanner, require_student_scanner, prepare_payload, payload_digest
from backend.app.database import get_inventory_write_db
from backend.app.models.user import User
from backend.app.routers import student_nfc as router
from backend.app.schemas.student_nfc import CardUID, RegistrationStart, ScannerEvidence
from backend.app.services import student_nfc as service
from backend.tests.inventory_session_stub import configure_session
from backend.tests.test_request_limits import request

KEY = base64.urlsafe_b64encode(b's' * 32).rstrip(b'=').decode()
DEVICE = base64.urlsafe_b64encode(b'd' * 32).rstrip(b'=').decode()
DIGEST = hashlib.sha256(DEVICE.encode()).hexdigest()


@pytest.fixture
def case(monkeypatch):
    scanner = StudentScanner(uuid4(), DIGEST)
    monkeypatch.setenv('STUDENT_NFC_PAYLOAD_KEY', KEY)
    monkeypatch.setenv('STUDENT_NFC_WRITE_PROFILE', 'SYNTHETIC-TEST-AREA')
    monkeypatch.setenv('STUDENT_NFC_SCANNER_DIGESTS', json.dumps({str(scanner.id): DIGEST}))
    db = Mock(spec=Session)
    db.info = {}
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.in_nested_transaction.return_value = False
    db.no_autoflush = nullcontext()
    configure_session(db, monkeypatch)
    db.get_bind.return_value.dialect = SimpleNamespace(identifier_preparer=SimpleNamespace(quote_schema=lambda value: value))
    actor = User(id=uuid4(), role='FACULTY', is_active=True)
    db.get.return_value = actor
    student = uuid4()
    db.execute.return_value.mappings.return_value.one.return_value = dict(id=student, full_name='Jane Doe', college_id='REG-1', created=False)
    at = datetime.now(timezone.utc)
    row = dict(id=uuid4(), student_id=student, card_uid='04A1B2C3', status='PENDING_WRITE',
               created_at=at, verified_at=None, expires_at=at+timedelta(minutes=5),
               attempt_nonce=str(uuid4()), write_profile='SYNTHETIC-TEST-AREA')
    outcome = dict(status=200, data=row)
    def scalar(statement, *args):
        return outcome if 'student_nfc_manage' in str(statement) else 'public'
    db.scalar.side_effect = scalar
    data = RegistrationStart(student_name=' Jane Doe ', registration_number=' reg-1 ', card_uid='04a1b2c3',
                             request_id=uuid4(), scanner_id=scanner.id)
    return SimpleNamespace(db=db, actor=actor, scanner=scanner, row=row, outcome=outcome, data=data)


@pytest.mark.parametrize('uid', ['04a1b2c3', ' 04A1B2C3 ', '04A1B2C3D4E5F6', '04A1B2C3D4E5F6070809'])
def test_uid_normalization(uid):
    assert CardUID(card_uid=uid).card_uid == uid.strip().upper()


@pytest.mark.parametrize('uid', ['', '04:AA:BB:CC', '123', 'GG000000', 'A'*22, '0000000\n0'])
def test_invalid_uids(uid):
    with pytest.raises(ValidationError):
        CardUID(card_uid=uid)


def test_borrower_validation_and_extra_fields(case):
    assert case.data.student_name == 'Jane Doe' and case.data.registration_number == 'REG-1'
    for change in ({'role': 'FACULTY'}, {'student_name': '123'}, {'registration_number': 'A B'}, {'success': True}):
        with pytest.raises(ValidationError):
            RegistrationStart.model_validate({**case.data.model_dump(), **change})


@pytest.mark.parametrize('role', ['FACULTY', 'LAB_ASSISTANT'])
def test_initiation_composes_real_borrower_boundary_and_preserves_uuid(case, role):
    case.actor.role = role
    result = service.initiate_registration(case.db, case.data, case.actor)
    assert result.student_id == case.row['student_id'] and result.status == 'PENDING_WRITE'
    assert result.payload == prepare_payload(case.row['attempt_nonce'], case.row['write_profile'])
    assert 'Jane' not in result.payload and 'REG-1' not in result.payload
    assert 'borrower_resolve_or_create' in str(case.db.execute.call_args.args[0])
    sql, parameters = case.db.scalar.call_args.args
    assert 'student_nfc_manage' in str(sql)
    values = json.loads(parameters['data'])
    assert values['student_id'] == str(case.row['student_id'])
    assert 'payload' not in values and len(values['digest']) == 64
    case.db.commit.assert_called_once(); case.db.add.assert_not_called()


@pytest.mark.parametrize('role', ['ADMIN', 'STUDENT'])
def test_unauthorized_staff_cannot_resolve_or_register(case, role):
    case.db.get.return_value = User(id=case.actor.id, role=role, is_active=True)
    with pytest.raises(HTTPException) as error:
        service.initiate_registration(case.db, case.data, case.actor)
    assert error.value.status_code == 403
    case.db.execute.assert_not_called(); case.db.rollback.assert_called_once()


@pytest.mark.parametrize('code', [409, 422])
def test_card_identity_conflict_rolls_back_borrower_transaction(case, code):
    case.outcome['status'] = code
    with pytest.raises(HTTPException) as error:
        service.initiate_registration(case.db, case.data, case.actor)
    assert error.value.status_code == code
    case.db.commit.assert_not_called(); case.db.rollback.assert_called_once()


def test_repeated_pending_request_reconstructs_same_challenge(case):
    first = service.initiate_registration(case.db, case.data, case.actor)
    second = service.initiate_registration(case.db, case.data, case.actor)
    assert first.payload == second.payload and first.registration_id == second.registration_id


def test_active_duplicate_has_no_write_payload(case):
    case.row.update(status='ACTIVE', verified_at=case.row['created_at'])
    assert service.initiate_registration(case.db, case.data, case.actor).payload is None


def test_status_and_active_card_lookup(case):
    result = service.registration_status(case.db, case.row['id'], case.actor)
    assert result['data'].student_id == case.row['student_id']
    # Restore raw SQL result, rather than returning a response model to the next SQL call.
    case.outcome['data'] = case.row
    result = service.lookup_card(case.db, CardUID(card_uid='04a1b2c3'), case.actor)
    assert result['data'].card_uid == '04A1B2C3'


def evidence(case):
    return ScannerEvidence(card_uid=case.row['card_uid'], write_profile=case.row['write_profile'],
                           payload=prepare_payload(case.row['attempt_nonce'], case.row['write_profile']))


def test_verification_uses_digest_and_bound_scanner_not_browser_flag(case):
    result = service.verify_registration(case.db, case.row['id'], evidence(case), case.scanner)
    assert result['data'] == {'registration_id': case.row['id'], 'status': 'ACTIVE'}
    _, parameters = case.db.scalar.call_args.args
    data = json.loads(parameters['data'])
    assert data['scanner_id'] == str(case.scanner.id)
    assert data['digest'] == payload_digest(evidence(case).payload) and 'payload' not in data
    with pytest.raises(ValidationError):
        ScannerEvidence.model_validate({**evidence(case).model_dump(), 'success': True})


@pytest.mark.parametrize('code', [404, 409])
def test_invalid_or_replayed_evidence_does_not_report_activation(case, code):
    case.outcome['status'] = code
    result = service.verify_registration(case.db, case.row['id'], evidence(case), case.scanner)
    assert result['status'] == code
    case.db.commit.assert_called_once()  # database expiry -> FAILED must persist


def test_wrong_scanner_and_missing_profile_fail_closed(case, monkeypatch):
    with pytest.raises(HTTPException) as error:
        service.verify_registration(case.db, case.row['id'], evidence(case), StudentScanner(uuid4(), DIGEST))
    assert error.value.status_code == 401
    monkeypatch.delenv('STUDENT_NFC_WRITE_PROFILE')
    with pytest.raises(HTTPException) as error:
        service.initiate_registration(case.db, case.data, case.actor)
    assert error.value.status_code == 503


def scanner_request(headers):
    return Request({'type': 'http', 'headers': headers})


def test_dedicated_scanner_authentication(case):
    value = f'StudentNFC {case.scanner.id}.{DEVICE}'.encode()
    assert require_student_scanner(scanner_request([(b'authorization', value)])).id == case.scanner.id
    for bad in [b'DEV-FACULTY', b'DEV-LAB_ASSISTANT', b'Bearer test',
                f'Scanner {case.scanner.id}.{DEVICE}'.encode(), f'StudentNFC {uuid4()}.{DEVICE}'.encode()]:
        with pytest.raises(HTTPException) as error:
            require_student_scanner(scanner_request([(b'authorization', bad)]))
        assert error.value.status_code == 401
    for headers in [[(b'authorization', value), (b'authorization', value)],
                    [(b'authorization', value), (b'x-dev-user', b'DEV-FACULTY')]]:
        with pytest.raises(HTTPException):
            require_student_scanner(scanner_request(headers))


@pytest.mark.parametrize('role', ['STUDENT', 'ADMIN'])
def test_staff_http_routes_deny_ineligible_identities(case, role):
    app = FastAPI(); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: User(id=case.actor.id, role=role, is_active=True)
    app.dependency_overrides[get_inventory_write_db] = lambda: case.db
    endpoints = [('/student-nfc/registrations', 'POST', case.data.model_dump(mode='json')),
                 (f'/student-nfc/registrations/{case.row["id"]}', 'GET', {}),
                 ('/student-nfc/cards/lookup', 'POST', {'card_uid': '04A1B2C3'})]
    try:
        for path, method, body in endpoints:
            assert request(app, [json.dumps(body).encode()], path=path, method=method)[0] == 403
    finally:
        app.dependency_overrides.clear()


def test_migration_and_openapi_contract():
    migration = import_module('backend.alembic.versions.e4b7c9d210af_student_nfc')
    assert migration.down_revision == 'd6f2b8a931c0'
    body = migration.routine_body('public')
    assert 'FOR UPDATE' in body and "r.status<>'PENDING_WRITE'" in body
    assert "r.expires_at<=at_time" in body and "SET status='FAILED'" in body
    assert "role='STUDENT'" in body and 'r.payload_sha256 IS DISTINCT FROM' in body
    app = FastAPI(); app.include_router(router.router)
    schema = app.openapi()
    assert schema['components']['schemas']['ScannerEvidence']['additionalProperties'] is False
    assert set(schema['paths']) == {'/student-nfc/registrations', '/student-nfc/cards/lookup',
        '/student-nfc/registrations/{registration_id}', '/student-nfc/registrations/{registration_id}/verify'}


def test_main_and_application_revision_graphs_are_joined_without_rewriting_history():
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from backend.alembic.application_safety import HEAD
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / 'alembic_nfc_application.ini'))
    graph = ScriptDirectory.from_config(config)
    assert graph.get_heads() == [HEAD]
    assert HEAD == 'f6c1a8d420be'
    assert graph.get_revision(HEAD).down_revision == 'a9c2e4f610bd'
    assert set(graph.get_revision('a9c2e4f610bd').down_revision) == {'c3a7e9b120df', 'e4b7c9d210af'}
    assert graph.get_revision('e4b7c9d210af').down_revision == 'd6f2b8a931c0'
    revisions = {item.revision for item in graph.walk_revisions()}
    assert {'f1a6b8c920de', 'b2d4e6f810ac', 'c3a7e9b120df', 'e4b7c9d210af'} <= revisions
