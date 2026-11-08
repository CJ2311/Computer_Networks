"""API endpoints for equipment maintenance."""

from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import User, UserRole

from backend.app.schemas.maintenance import (
    MaintenanceResponse,
    MaintenanceEventPageResponse,
    MaintenanceIssueCreate,
    MaintenanceRecoveryCreate,
    MaintenanceAssignmentCreate,
    MaintenanceVersionRequest,
    MaintenanceRepairSubmission,
    MaintenanceVerificationCreate,
    MaintenanceCancellationCreate,
    MaintenanceNoteCreate,
    MaintenancePageResponse,
)

from backend.app.services.maintenance import (
    report_maintenance_issue,
    recover_cancelled_request,
    assign_maintenance_request,
    start_repair,
    submit_repair_for_verification,
    verify_repair,
    cancel_maintenance_request,
    get_maintenance_request,
    list_maintenance_events,
    add_maintenance_note,
    list_maintenance_requests,
)


router = APIRouter(
    prefix="/maintenance",
    tags=["Maintenance"],
)


@router.post("/{request_id}/notes", response_model=MaintenanceResponse)
def append_note(
    request_id: UUID,
    data: MaintenanceNoteCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(require_roles(UserRole.FACULTY, UserRole.LAB_ASSISTANT, UserRole.ADMIN)),
):
    return add_maintenance_note(db, request_id, actor, data.expected_version, data.note)


@router.post(
    "",
    response_model=MaintenanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def report_issue(
    data: MaintenanceIssueCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
            UserRole.ADMIN,
        )
    ),
):
    return report_maintenance_issue(
        db=db,
        asset_id=data.asset_id,
        actor=actor,
        issue_type=data.issue_type,
        issue_description=data.issue_description,
    )


@router.post(
    "/{request_id}/recover",
    response_model=MaintenanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def recover_request(
    request_id: UUID,
    data: MaintenanceRecoveryCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
            UserRole.ADMIN,
        )
    ),
):
    return recover_cancelled_request(
        db=db,
        request_id=request_id,
        actor=actor,
        issue_description=data.issue_description,
    )


@router.post(
    "/{request_id}/assign",
    response_model=MaintenanceResponse,
)
def assign_request(
    request_id: UUID,
    data: MaintenanceAssignmentCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.ADMIN,
        )
    ),
):
    return assign_maintenance_request(
        db=db,
        request_id=request_id,
        actor=actor,
        assignee_id=data.assignee_id,
        expected_version=data.expected_version,
    )


@router.post(
    "/{request_id}/start",
    response_model=MaintenanceResponse,
)
def begin_repair(
    request_id: UUID,
    data: MaintenanceVersionRequest,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return start_repair(
        db=db,
        request_id=request_id,
        actor=actor,
        expected_version=data.expected_version,
    )


@router.post(
    "/{request_id}/submit",
    response_model=MaintenanceResponse,
)
def submit_repair(
    request_id: UUID,
    data: MaintenanceRepairSubmission,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return submit_repair_for_verification(
        db=db,
        request_id=request_id,
        actor=actor,
        expected_version=data.expected_version,
        repair_notes=data.repair_notes,
    )


@router.post(
    "/{request_id}/verify",
    response_model=MaintenanceResponse,
)
def verify_completed_repair(
    request_id: UUID,
    data: MaintenanceVerificationCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.ADMIN,
        )
    ),
):
    return verify_repair(
        db=db,
        request_id=request_id,
        actor=actor,
        expected_version=data.expected_version,
        outcome=data.outcome,
        notes=data.notes,
    )


@router.post(
    "/{request_id}/cancel",
    response_model=MaintenanceResponse,
)
def cancel_request(
    request_id: UUID,
    data: MaintenanceCancellationCreate,
    db: Session = Depends(get_inventory_write_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.ADMIN,
        )
    ),
):
    return cancel_maintenance_request(
        db=db,
        request_id=request_id,
        actor=actor,
        expected_version=data.expected_version,
        reason=data.reason,
    )


@router.get(
    "/{request_id}",
    response_model=MaintenanceResponse,
)
def get_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
            UserRole.ADMIN,
        )
    ),
):
    return get_maintenance_request(
        db=db,
        request_id=request_id,
        actor=actor,
    )


@router.get(
    "/{request_id}/events",
    response_model=MaintenanceEventPageResponse,
)
def get_request_events(
    request_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    actor: User = Depends(
        require_roles(
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
            UserRole.ADMIN,
        )
    ),
):
    return list_maintenance_events(
        db=db,
        request_id=request_id,
        actor=actor,
        limit=limit,
        offset=offset,
    )


@router.get("", response_model=MaintenancePageResponse)
def list_requests(
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.ADMIN,UserRole.FACULTY,UserRole.LAB_ASSISTANT)),
    limit: int = Query(25,ge=1,le=100), offset: int = Query(0,ge=0,lt=2**63),
    status: Literal["OPEN","IN_PROGRESS","AWAITING_VERIFICATION","COMPLETED","CANCELLED"] | None = None,
    asset_id: UUID | None = None,
):
    return list_maintenance_requests(db,actor,limit,offset,status,asset_id)
