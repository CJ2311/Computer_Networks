"""Only protected PostgreSQL staging routines may write scanner state."""
import re

from fastapi import HTTPException
from sqlalchemy import func, select, text

from pydantic import ValidationError
from backend.app.schemas.scanners import ScanSubmit, ScanQuery, ScanReceipt, ScanPage
from backend.app.services.inventory_transactions import transaction
from backend.app.services.maintenance import _authorize


ERRORS = {
    "invalid_scanner": (401, "Invalid scanner credentials"),
    "invalid_input": (422, "Invalid scan request"),
    "rate_limited": (429, "Scanner request limit exceeded"),
    "capture_unavailable": (409, "Scanner has no active capture session"),
    "scan_conflict": (409, "Scan identifier or sequence conflicts with prior scans"),
    "duplicate_uid": (409, "RFID UID was scanned too recently"),
    "unknown_uid": (404, "Registered equipment asset not found"),
    "ambiguous_uid": (409, "RFID UID matches multiple registered assets"),
    "capture_not_found": (404, "Scan capture session not found"),
}


def _call(db, routine, parameters, response_model):
    schema = db.scalar(select(func.current_schema()))
    if not isinstance(schema, str) or not re.fullmatch(r"[a-z_][a-z_0-9]*", schema) or schema.startswith("pg_"):
        raise HTTPException(503, "Scanner service is unavailable")
    q = db.get_bind().dialect.identifier_preparer.quote_schema(schema)
    arguments = ",".join(":" + key for key in parameters)
    result = db.scalar(text(f"SELECT {q}.{routine}({arguments})"), parameters)
    if not isinstance(result, dict):
        raise HTTPException(503, "Scanner service returned an invalid result")
    if result.get("status") == 200:
        try:
            return {"status": 200, "data": response_model.model_validate(result.get("data"))}
        except ValidationError:
            raise HTTPException(503, "Scanner service returned an invalid result") from None
    code = result.get("error")
    if not isinstance(code, str) or code not in ERRORS or result.get("status") != ERRORS[code][0]:
        raise HTTPException(503, "Scanner service returned an invalid result")
    # Return errors to the router AFTER the owned transaction commits rate/sequence
    # bookkeeping. Raising here would roll it back and permit unknown-tag flooding.
    return {"status": ERRORS[code][0], "error": code}


@transaction(uncertain_commit_detail="Scan outcome is uncertain; retry the same scan ID, sequence and UID")
def ingest_scan(db, data, credential):
    data = ScanSubmit.model_validate(data)
    return _call(db, "scanner_ingest", {"scanner": credential.scanner_id, "digest": credential.digest,
                 "session": data.capture_session_id, "scan": data.scan_id, "sequence": data.sequence, "uid": data.uid}, ScanReceipt)


@transaction
def read_scans(db, session_id, data, actor, session_digest):
    data = ScanQuery.model_validate(data)
    user = _authorize(db, actor, {"FACULTY", "LAB_ASSISTANT"})
    return _call(db, "scanner_read_events", {"staff": user.id, "session": session_id,
                 "digest": session_digest, "after": data.after_sequence, "limit": data.limit}, ScanPage)
