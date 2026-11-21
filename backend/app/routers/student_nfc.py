from uuid import UUID
from fastapi import APIRouter, Depends

from backend.app.auth.dependencies import require_roles
from backend.app.auth.student_nfc import require_student_scanner
from backend.app.database import get_inventory_write_db
from backend.app.models.user import UserRole
from backend.app.schemas.student_nfc import (
    RegistrationStart, RegistrationPrepared, RegistrationStatus, CardUID,
    ScannerEvidence, ScannerAcknowledgement,
)
from backend.app.services.student_nfc import (
    initiate_registration, registration_status, lookup_card, verify_registration, raise_error,
)

router = APIRouter(prefix="/student-nfc", tags=["Student NFC registration"])
staff = require_roles(UserRole.FACULTY, UserRole.LAB_ASSISTANT)


def response(outcome):
    raise_error(outcome)
    return outcome["data"]


@router.post("/registrations", response_model=RegistrationPrepared)
def start(data: RegistrationStart, actor=Depends(staff), db=Depends(get_inventory_write_db)):
    return initiate_registration(db, data, actor)


@router.get("/registrations/{registration_id}", response_model=RegistrationStatus)
def status(registration_id: UUID, actor=Depends(staff), db=Depends(get_inventory_write_db)):
    return response(registration_status(db, registration_id, actor))


@router.post("/cards/lookup", response_model=RegistrationStatus)
def lookup(data: CardUID, actor=Depends(staff), db=Depends(get_inventory_write_db)):
    return response(lookup_card(db, data, actor))


@router.post("/registrations/{registration_id}/verify", response_model=ScannerAcknowledgement)
def verify(registration_id: UUID, data: ScannerEvidence, scanner=Depends(require_student_scanner),
           db=Depends(get_inventory_write_db)):
    return response(verify_registration(db, registration_id, data, scanner))
