"""Student NFC verification only in the opt-in disposable PostgreSQL harness."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.alembic.application_safety import verify_application
from backend.app.auth.student_nfc import StudentScanner
from backend.app.schemas.student_nfc import RegistrationStart, ScannerEvidence, CardUID
from backend.app.services.student_nfc import initiate_registration, verify_registration, registration_status, lookup_card
from infra.nfc_postgres.tests.test_scanner_ingestion import case, inventory_snapshot


@pytest.fixture
def nfc(case, monkeypatch):
    key = base64.urlsafe_b64encode(b'p'*32).rstrip(b'=').decode()
    device = base64.urlsafe_b64encode(b'd'*32).rstrip(b'=').decode()
    scanner = StudentScanner(uuid4(), hashlib.sha256(device.encode()).hexdigest())
    monkeypatch.setenv('STUDENT_NFC_PAYLOAD_KEY', key)
    monkeypatch.setenv('STUDENT_NFC_WRITE_PROFILE', 'SYNTHETIC-TEST-AREA')
    monkeypatch.setenv('STUDENT_NFC_SCANNER_DIGESTS', json.dumps({str(scanner.id): scanner.credential_digest}))
    case.student_scanner = scanner
    return case


def start(nfc, **changes):
    values = dict(student_name='NFC Borrower', registration_number='NFC-REG-1', card_uid='04aabbcc',
                  scanner_id=nfc.student_scanner.id, request_id=uuid4())
    values.update(changes)
    proposal = RegistrationStart(**values)
    with Session(nfc.runtime) as db:
        return proposal, initiate_registration(db, proposal, nfc.owner)


def verify(nfc, registration, **changes):
    values = dict(card_uid=registration.card_uid, payload=registration.payload, write_profile=registration.write_profile)
    values.update(changes)
    with Session(nfc.runtime) as db:
        return verify_registration(db, registration.registration_id, ScannerEvidence(**values), nfc.student_scanner)


def status(nfc, identifier):
    with Session(nfc.runtime) as db:
        return registration_status(db, identifier, nfc.owner)


def test_success_and_duplicate_registration_preserve_borrower_and_history(nfc):
    _, pending = start(nfc)
    after_borrower = inventory_snapshot(nfc)
    assert pending.status == 'PENDING_WRITE' and pending.verified_at is None
    assert verify(nfc, pending)['status'] == 200
    _, duplicate = start(nfc)
    assert duplicate.registration_id == pending.registration_id and duplicate.student_id == pending.student_id
    assert duplicate.status == 'ACTIVE' and duplicate.payload is None
    assert status(nfc, pending.registration_id)['data'].status == 'ACTIVE'
    with Session(nfc.runtime) as db:
        found = lookup_card(db, CardUID(card_uid='04aabbcc'), nfc.other)
    assert found['data'].student_id == pending.student_id
    assert inventory_snapshot(nfc) == after_borrower
    with nfc.admin.connect() as c:
        verify_application(c, nfc.h.settings)


def test_conflicts_roll_back_new_borrower_and_keep_original_identity(nfc):
    _, pending = start(nfc)
    before = inventory_snapshot(nfc)
    for changes in [dict(registration_number='OTHER-REG'), dict(card_uid='11223344'), dict(student_name='Different Name')]:
        with pytest.raises(HTTPException) as error:
            start(nfc, **changes)
        assert error.value.status_code == 409
        assert inventory_snapshot(nfc) == before
    assert status(nfc, pending.registration_id)['data'].status == 'PENDING_WRITE'


def test_pending_retry_preserves_challenge_and_verification_replay_is_rejected(nfc):
    proposal, first = start(nfc)
    _, retried = start(nfc, request_id=proposal.request_id)
    assert first.payload == retried.payload and first.registration_id == retried.registration_id
    assert verify(nfc, first)['status'] == 200
    assert verify(nfc, first)['status'] == 409


@pytest.mark.parametrize('problem', ['uid', 'payload', 'profile', 'scanner'])
def test_mismatched_evidence_never_activates(nfc, problem, monkeypatch):
    _, pending = start(nfc)
    values = {}
    if problem == 'uid': values['card_uid'] = '11223344'
    if problem == 'payload': values['payload'] = 'CNINV1:' + str(uuid4()) + ':' + 'A'*43
    if problem == 'profile': values['write_profile'] = 'OTHER-AREA'
    if problem == 'scanner':
        scanner = StudentScanner(uuid4(), nfc.student_scanner.credential_digest)
        monkeypatch.setenv('STUDENT_NFC_SCANNER_DIGESTS', json.dumps({str(scanner.id): scanner.credential_digest}))
        nfc.student_scanner = scanner
    try:
        assert verify(nfc, pending, **values)['status'] in {404, 409}
    except HTTPException as error:
        assert error.status_code == 409
    assert status(nfc, pending.registration_id)['data'].status == 'PENDING_WRITE'


def test_expired_interrupted_registration_retries_in_place_with_new_challenge(nfc):
    proposal, old = start(nfc)
    with nfc.admin.begin() as c:
        nfc.h.verify_engine_identity(c)
        c.execute(text("""UPDATE student_nfc_cards SET created_at=clock_timestamp()-interval '10 minutes',
            attempt_started_at=clock_timestamp()-interval '6 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE id=:id"""), {'id': old.registration_id})
    assert verify(nfc, old)['status'] == 409
    assert status(nfc, old.registration_id)['data'].status == 'FAILED'
    _, renewed = start(nfc, request_id=proposal.request_id)
    assert renewed.registration_id == old.registration_id and renewed.student_id == old.student_id
    assert renewed.payload != old.payload
    assert verify(nfc, old)['status'] == 409
    assert verify(nfc, renewed)['status'] == 200


def test_runtime_has_no_direct_student_card_access(nfc):
    for statement in ['SELECT * FROM student_nfc_cards', "UPDATE student_nfc_cards SET status='ACTIVE'",
                      'DELETE FROM student_nfc_cards', 'TRUNCATE student_nfc_cards']:
        with pytest.raises(DBAPIError):
            with nfc.runtime.begin() as c:
                nfc.h.verify_engine_identity(c)
                c.execute(text(statement))


def test_competing_registrations_cannot_create_duplicate_associations(nfc):
    def attempt(_):
        try:
            return start(nfc)[1]
        except HTTPException as error:
            assert error.status_code == 409
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert any(result is not None for result in results)
    with nfc.admin.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM student_nfc_cards')) == 1


def test_verification_waiting_across_expiry_is_rejected_after_lock(nfc):
    """Prepared only: real locks require the authorized disposable harness."""
    _, pending = start(nfc)
    with nfc.admin.begin() as c:
        nfc.h.verify_engine_identity(c)
        expires = c.scalar(text("""UPDATE student_nfc_cards
            SET expires_at=clock_timestamp()+interval '5 seconds'
            WHERE id=:id RETURNING expires_at"""), {"id": pending.registration_id})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with nfc.admin.begin() as blocker:
            nfc.h.verify_engine_identity(blocker)
            blocker.execute(text("SELECT id FROM student_nfc_cards WHERE id=:id FOR UPDATE"),
                            {"id": pending.registration_id}).one()
            blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(verify, nfc, pending)
            deadline = time.monotonic() + 8
            with nfc.admin.connect() as observer:
                # Prove verification actually entered before expiry and blocked on
                # THIS lock. A test merely submitted after expiry is insufficient.
                while True:
                    observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = observer.scalar(text("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database() AND wait_event_type='Lock'
                        AND :blocker=ANY(pg_blocking_pids(pid)))"""), {"blocker": blocker_pid})
                    if waiting:
                        break
                    assert time.monotonic() < deadline, "Verification did not reach the held row lock"
                    time.sleep(0.02)
                assert observer.scalar(text("SELECT clock_timestamp()")) < expires
                while observer.scalar(text("SELECT clock_timestamp()")) <= expires:
                    assert time.monotonic() < deadline, "Expiry boundary was not reached within the test budget"
                    time.sleep(0.02)
        # Lock release happens only after expiry. The original function would use
        # its pre-wait timestamp, activate the card, and fail this assertion.
        assert future.result(timeout=15)["status"] == 409
    current = status(nfc, pending.registration_id)["data"]
    assert current.status == "FAILED" and current.verified_at is None
