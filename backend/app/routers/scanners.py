from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from backend.app.auth.dependencies import require_roles
from backend.app.auth.scanners import require_scanner, require_capture_secret
from backend.app.database import get_inventory_write_db
from backend.app.models.user import UserRole
from backend.app.schemas.scanners import ScanSubmit, ScanQuery, ScanReceipt, ScanPage
from backend.app.services.scanners import ingest_scan, read_scans, ERRORS

router = APIRouter(tags=["Equipment scanners"])
staff = require_roles(UserRole.FACULTY, UserRole.LAB_ASSISTANT)


def _response(outcome):
    if outcome["status"] != 200:
        status, detail = ERRORS[outcome["error"]]
        raise HTTPException(status, detail, headers={"Retry-After": "60"} if status == 429 else None)
    return outcome["data"]


@router.post("/device-scans", response_model=ScanReceipt)
def submit_scan(data: ScanSubmit, credential=Depends(require_scanner), db=Depends(get_inventory_write_db)):
    return _response(ingest_scan(db, data, credential))


@router.post("/scan-sessions/{session_id}/events", response_model=ScanPage)
def query_scans(session_id: UUID, data: ScanQuery, actor=Depends(staff),
                digest=Depends(require_capture_secret), db=Depends(get_inventory_write_db)):
    return _response(read_scans(db, session_id, data, actor, digest))
