"""Atomic borrower composition and private student-card registration routines."""
import hmac
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text

from backend.app.auth.student_nfc import configured_scanners, payload_settings, prepare_payload, payload_digest
from backend.app.schemas.student_nfc import RegistrationStart, RegistrationStatus, RegistrationPrepared, ScannerEvidence
from backend.app.services.borrowers import resolve_borrower_in_transaction
from backend.app.services.inventory_transactions import transaction
from backend.app.services.maintenance import _authorize

DETAILS = {403: "Active authorized staff required", 404: "Student NFC registration not found",
           409: "Student NFC association, attempt or verification conflict", 422: "Invalid student NFC registration"}


def _call(db, action, actor=None, identifier=None, **data):
    schema = db.scalar(select(func.current_schema()))
    if not isinstance(schema, str) or not re.fullmatch(r"[a-z_][a-z_0-9]*", schema) or schema.startswith("pg_"):
        raise HTTPException(503, "Student NFC registration unavailable")
    q = db.get_bind().dialect.identifier_preparer.quote_schema(schema)
    outcome = db.scalar(text(f"SELECT {q}.student_nfc_manage(:action,:actor,:id,CAST(:data AS jsonb))"),
                        {"action": action, "actor": actor, "id": identifier, "data": json.dumps(data, default=str)})
    if not isinstance(outcome, dict) or outcome.get("status") not in {200, *DETAILS}:
        raise HTTPException(503, "Student NFC registration unavailable")
    return outcome


def _status(row):
    return RegistrationStatus.model_validate({"registration_id": row["id"], **{
        key: row[key] for key in ("student_id", "card_uid", "status", "created_at", "verified_at", "expires_at")}})


def raise_error(outcome):
    if outcome["status"] != 200:
        raise HTTPException(outcome["status"], DETAILS[outcome["status"]])


@transaction(uncertain_commit_detail="Student NFC outcome uncertain; retry the same request ID and borrower details")
def initiate_registration(db, data, actor):
    data = RegistrationStart.model_validate(data)
    staff = _authorize(db, actor, {"FACULTY", "LAB_ASSISTANT"})
    _, profile = payload_settings()
    if data.scanner_id not in configured_scanners():
        raise HTTPException(422, "Registered student-write scanner required")
    # Same owned transaction: borrower creation rolls back on any NFC conflict.
    borrower = resolve_borrower_in_transaction(db, data.model_dump(include={"student_name", "registration_number"}), staff)
    nonce = uuid4()
    proposed_payload = prepare_payload(nonce, profile)
    result = _call(db, "init", staff.id, uuid4(), student_id=borrower.student_id,
                   card_uid=data.card_uid, scanner_id=data.scanner_id, request_id=data.request_id,
                   nonce=nonce, digest=payload_digest(proposed_payload), profile=profile)
    raise_error(result)
    row = result["data"]
    payload = prepare_payload(row["attempt_nonce"], row["write_profile"]) if row["status"] == "PENDING_WRITE" else None
    return RegistrationPrepared(**_status(row).model_dump(), payload=payload, write_profile=row["write_profile"])


@transaction
def registration_status(db, identifier, actor):
    staff = _authorize(db, actor, {"FACULTY", "LAB_ASSISTANT"})
    result = _call(db, "status", staff.id, identifier)
    if result["status"] == 200:
        result["data"] = _status(result["data"])
    return result


@transaction
def lookup_card(db, data, actor):
    from backend.app.schemas.student_nfc import CardUID
    data = CardUID.model_validate(data)
    staff = _authorize(db, actor, {"FACULTY", "LAB_ASSISTANT"})
    result = _call(db, "lookup", staff.id, card_uid=data.card_uid)
    if result["status"] == 200:
        result["data"] = _status(result["data"])
    return result


@transaction(uncertain_commit_detail="Verification outcome uncertain; staff must check registration status")
def verify_registration(db, identifier, evidence, scanner):
    evidence = ScannerEvidence.model_validate(evidence)
    expected = configured_scanners().get(scanner.id)
    if expected is None or not hmac.compare_digest(expected, scanner.credential_digest):
        raise HTTPException(401, "Student-write scanner credentials required")
    _, profile = payload_settings()
    if not hmac.compare_digest(profile, evidence.write_profile):
        raise HTTPException(409, "Approved write profile mismatch")
    # Errors are returned to the router after commit so expiry->FAILED persists.
    result = _call(db, "verify", identifier=identifier, scanner_id=scanner.id,
                   card_uid=evidence.card_uid, digest=payload_digest(evidence.payload), profile=profile)
    if result["status"] == 200:
        result["data"] = {"registration_id": result["data"]["id"], "status": "ACTIVE"}
    return result
