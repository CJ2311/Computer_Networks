"""Staff inventory auditing endpoints with UUID scan retry keys."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import User, UserRole
from backend.app.schemas.inventory_audit import (
    AuditStatus, InventoryAuditCreate, InventoryAuditScanCreate, InventoryAuditFinalize, InventoryAuditCancel,
    InventoryAuditResponse, InventoryAuditScanResponse, InventoryAuditPage, InventoryAuditScanPage,
    InventoryAuditExpectedPage, InventoryAuditReport,
)
from backend.app.services.inventory_audit import (
    create_inventory_audit, record_inventory_scan, finalize_inventory_audit, cancel_inventory_audit,
    get_inventory_audit, list_inventory_audits, list_inventory_observations, get_inventory_audit_report,
)


router = APIRouter(prefix="/inventory-audits", tags=["Inventory audits"])
staff = require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)
faculty = require_roles(UserRole.FACULTY)
managers = require_roles(UserRole.ADMIN, UserRole.FACULTY)


@router.post("", response_model=InventoryAuditResponse, status_code=status.HTTP_201_CREATED)
def create_audit(
    data: InventoryAuditCreate, db: Session = Depends(get_inventory_write_db), actor: User = Depends(staff),
):
    """Freeze individually tracked identities and custody in one snapshot.

    Location matching is exact after request whitespace normalization. Out-of-location
    identities are retained to distinguish unexpected assets from unknown tags.
    """
    return create_inventory_audit(db, data, actor)


@router.get("", response_model=InventoryAuditPage)
def list_audits(
    db: Session = Depends(get_db), actor: User = Depends(staff),
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1),
    status: AuditStatus | None = None,
    location: str | None = Query(None, min_length=1, max_length=150, pattern=r"^[^\x00]+$"),
):
    """Staff access is laboratory-wide until assigned-lab authorization exists."""
    return list_inventory_audits(db, actor, limit, offset, status, location)


@router.get("/{audit_id}", response_model=InventoryAuditResponse)
def get_audit(audit_id: UUID, db: Session = Depends(get_db), actor: User = Depends(staff)):
    return get_inventory_audit(db, audit_id, actor)


@router.post("/{audit_id}/scans", response_model=InventoryAuditScanResponse)
def record_scan(
    audit_id: UUID, data: InventoryAuditScanCreate,
    db: Session = Depends(get_inventory_write_db), actor: User = Depends(staff),
):
    """Submit a physical scan with a fresh UUID key; reuse that key for network retries.

    The same actor, tag, and observed location replay the original immutable result,
    including after closure. Reusing a key with a different payload or actor returns
    409. A fresh key for an already scanned tag is a physical DUPLICATE. Client scan
    classifications are forbidden. NFC tags provide identification only.
    """
    return record_inventory_scan(db, audit_id, data, actor)


@router.get("/{audit_id}/scans", response_model=InventoryAuditScanPage)
def list_scans(
    audit_id: UUID, db: Session = Depends(get_db), actor: User = Depends(staff),
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1),
):
    return list_inventory_observations(db, audit_id, actor, scans=True, limit=limit, offset=offset)


@router.get("/{audit_id}/expected-assets", response_model=InventoryAuditExpectedPage)
def list_expected_assets(
    audit_id: UUID, db: Session = Depends(get_db), actor: User = Depends(staff),
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1),
):
    return list_inventory_observations(db, audit_id, actor, scans=False, limit=limit, offset=offset)


@router.get("/{audit_id}/report", response_model=InventoryAuditReport)
def report(
    audit_id: UUID, db: Session = Depends(get_db), actor: User = Depends(staff),
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1),
):
    """Preview reconciliation while open; return persisted findings after completion.

    NOT_VERIFIED never sets an asset to MISSING. STATE_CHANGED flags inventory,
    checkout, identifier, or maintenance drift at the reconciliation observation.
    Cancellation reports retain observations but do not claim completed reconciliation.
    Findings are paginated; totals cover the audit.
    """
    return get_inventory_audit_report(db, audit_id, actor, limit, offset)


@router.post("/{audit_id}/finalize", response_model=InventoryAuditResponse)
def finalize(
    audit_id: UUID, data: InventoryAuditFinalize,
    db: Session = Depends(get_inventory_write_db), actor: User = Depends(faculty),
):
    """Faculty accept the current version and freeze reconciliation and findings.

    Reload the report after a 409 version conflict. Finalization preserves all
    observations and never changes operational inventory or borrowing state.
    """
    return finalize_inventory_audit(db, audit_id, data, actor)


@router.post("/{audit_id}/cancel", response_model=InventoryAuditResponse)
def cancel(
    audit_id: UUID, data: InventoryAuditCancel,
    db: Session = Depends(get_inventory_write_db), actor: User = Depends(managers),
):
    """Cancel at the expected version, retaining the snapshot and scan history."""
    return cancel_inventory_audit(db, audit_id, data, actor)
