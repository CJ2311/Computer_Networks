"""Inventory auditing with frozen identities and serialized observations.

Only inventory audit tables are mutated. Each live inventory read is one MVCC
statement, so checkout/maintenance operations keep their existing lock order.
"""
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset
from backend.app.models.inventory_audit import (
    InventoryAudit, InventoryAuditExpectedAsset, InventoryAuditScan, InventoryAuditDiscrepancy,
)
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.user import User
from backend.app.schemas.inventory_audit import (
    InventoryAuditCreate, InventoryAuditScanCreate, InventoryAuditFinalize, InventoryAuditCancel,
    InventoryAuditResponse, InventoryAuditScanResponse, InventoryAuditFinding, InventoryAuditReport,
    InventoryAuditExpectedResponse, InventoryAuditPage, InventoryAuditScanPage, InventoryAuditExpectedPage,
)
from backend.app.services.inventory_transactions import clean_session, flush_stage, transaction
from backend.app.services.maintenance import ACTIVE, _authorize, _id, _now


UNCERTAIN = "Inventory audit outcome could not be confirmed. Check the audit and idempotency key before retrying."
FACULTY = frozenset({"FACULTY"})
CANCELLERS = frozenset({"ADMIN", "FACULTY"})


def _inventory_statement():
    # Active-record partial unique indexes prevent join fanout.
    return select(
        EquipmentAsset.id.label("asset_id"), Equipment.name.label("equipment_name"),
        EquipmentAsset.asset_code, EquipmentAsset.nfc_tag_id, EquipmentAsset.location,
        EquipmentAsset.availability_status, EquipmentAsset.condition_status, EquipmentAsset.maintenance_hold,
        EquipmentCheckout.id.label("checkout_id"),
        MaintenanceRequest.id.label("maintenance_id"), MaintenanceRequest.version.label("maintenance_version"),
        func.statement_timestamp().label("observed_at"),
    ).select_from(EquipmentAsset).join(
        Equipment, Equipment.id == EquipmentAsset.equipment_id,
    ).outerjoin(
        EquipmentCheckout, and_(EquipmentCheckout.asset_id == EquipmentAsset.id, EquipmentCheckout.status == "BORROWED"),
    ).outerjoin(
        MaintenanceRequest, and_(MaintenanceRequest.asset_id == EquipmentAsset.id, MaintenanceRequest.status.in_(ACTIVE)),
    ).where(Equipment.tracking_type == "INDIVIDUAL").order_by(EquipmentAsset.id)


def _inventory(db: Session):
    rows = db.execute(_inventory_statement()).mappings().all()
    observed_at = rows[0]["observed_at"] if rows else _now(db)
    return rows, observed_at


def _audit(db: Session, audit_id: UUID, *, shared=False):
    audit = db.scalar(
        select(InventoryAudit).where(InventoryAudit.id == _id(audit_id, "audit ID"))
        .with_for_update(read=shared).execution_options(populate_existing=True)
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Inventory audit not found")
    return audit


def _open(audit):
    if audit.status != "IN_PROGRESS" or not audit.snapshot_sealed:
        raise HTTPException(status_code=409, detail="Inventory audit is not open for changes")


def _version(audit, expected):
    _open(audit)
    if audit.version != expected:
        raise HTTPException(status_code=409, detail="Inventory audit changed; reload the report before continuing")


def _save(db, obj):
    db.add(obj)
    flush_stage(db, obj)


@transaction(uncertain_commit_detail=UNCERTAIN)
def create_inventory_audit(db: Session, data: InventoryAuditCreate, actor: User) -> InventoryAuditResponse:
    _authorize(db, actor)
    rows, started_at = _inventory(db)
    staff = _authorize(db, actor)
    audit = InventoryAudit(
        id=uuid4(), name=data.name, location=data.location, notes=data.notes,
        status="IN_PROGRESS", created_by=staff.id, started_at=started_at, snapshot_sealed=False, version=1,
    )
    _save(db, audit)
    for row in rows:
        _save(db, InventoryAuditExpectedAsset(
            id=uuid4(), audit_id=audit.id, asset_id=row["asset_id"], in_scope=row["location"] == data.location,
            equipment_name_at_start=row["equipment_name"], expected_location=row["location"],
            availability_at_start=row["availability_status"], condition_at_start=row["condition_status"],
            maintenance_hold_at_start=row["maintenance_hold"], asset_code_at_start=row["asset_code"],
            nfc_tag_id_at_start=row["nfc_tag_id"], checkout_id_at_start=row["checkout_id"],
            maintenance_id_at_start=row["maintenance_id"], maintenance_version_at_start=row["maintenance_version"],
        ))
    audit.snapshot_sealed = True
    audit.version += 1
    flush_stage(db, audit)
    return InventoryAuditResponse.model_validate(audit)


def _classify(expected, first, observed_location):
    if first is not None:
        return "DUPLICATE"
    if expected is None:
        return "UNKNOWN_TAG"
    if not expected.in_scope:
        return "UNEXPECTED"
    if observed_location != expected.expected_location:
        return "MISPLACED"
    return "MATCHED"


@transaction(uncertain_commit_detail=UNCERTAIN)
def record_inventory_scan(
    db: Session, audit_id: UUID, data: InventoryAuditScanCreate, actor: User,
) -> InventoryAuditScanResponse:
    _authorize(db, actor)
    audit = _audit(db, audit_id)
    staff = _authorize(db, actor)
    previous = db.scalar(select(InventoryAuditScan).where(
        InventoryAuditScan.audit_id == audit.id, InventoryAuditScan.idempotency_key == data.idempotency_key,
    ))
    # Replays remain available after closure and perform no UPDATE.
    if previous is not None:
        if (previous.nfc_tag_id, previous.observed_location, previous.scanned_by) != (
            data.nfc_tag_id, data.observed_location, staff.id,
        ):
            raise HTTPException(status_code=409, detail="Idempotency key was already used for a different request")
        return InventoryAuditScanResponse.model_validate(previous)
    _open(audit)
    expected = db.scalar(select(InventoryAuditExpectedAsset).where(
        InventoryAuditExpectedAsset.audit_id == audit.id,
        InventoryAuditExpectedAsset.nfc_tag_id_at_start == data.nfc_tag_id,
    ))
    first = db.scalar(select(InventoryAuditScan).where(
        InventoryAuditScan.audit_id == audit.id, InventoryAuditScan.nfc_tag_id == data.nfc_tag_id,
        InventoryAuditScan.duplicate_of_scan_id.is_(None),
    ))
    scan = InventoryAuditScan(
        id=uuid4(), audit_id=audit.id, idempotency_key=data.idempotency_key,
        asset_id=expected.asset_id if expected is not None else None,
        expected_asset_id=expected.id if expected is not None else None,
        duplicate_of_scan_id=first.id if first is not None else None,
        nfc_tag_id=data.nfc_tag_id, scanned_by=staff.id, scanned_at=_now(db),
        observed_location=data.observed_location, result=_classify(expected, first, data.observed_location),
    )
    _save(db, scan)
    audit.version += 1
    flush_stage(db, audit)
    return InventoryAuditScanResponse.model_validate(scan)


def _records(db, audit):
    expected = db.scalars(select(InventoryAuditExpectedAsset).where(
        InventoryAuditExpectedAsset.audit_id == audit.id,
    ).order_by(InventoryAuditExpectedAsset.asset_id)).all()
    scans = db.scalars(select(InventoryAuditScan).where(
        InventoryAuditScan.audit_id == audit.id,
    ).order_by(InventoryAuditScan.scanned_at, InventoryAuditScan.id)).all()
    return expected, scans


def _state_at_start(expected):
    return {
        "equipment_name": expected.equipment_name_at_start, "asset_code": expected.asset_code_at_start,
        "nfc_tag_id": expected.nfc_tag_id_at_start, "location": expected.expected_location,
        "availability_status": expected.availability_at_start, "condition_status": expected.condition_at_start,
        "maintenance_hold": expected.maintenance_hold_at_start,
        "checkout_id": str(expected.checkout_id_at_start) if expected.checkout_id_at_start else None,
        "maintenance_id": str(expected.maintenance_id_at_start) if expected.maintenance_id_at_start else None,
        "maintenance_version": expected.maintenance_version_at_start,
    }


def _state_now(row):
    return {
        key: str(row[key]) if isinstance(row[key], UUID) else row[key]
        for key in ("equipment_name", "asset_code", "nfc_tag_id", "location", "availability_status",
                    "condition_status", "maintenance_hold", "checkout_id", "maintenance_id", "maintenance_version")
    }


def _findings(expected, scans, current):
    findings = []
    snapshots = {item.id: item for item in expected}
    seen = {scan.expected_asset_id for scan in scans if scan.expected_asset_id is not None}
    for scan in scans:
        if scan.result in {"MISPLACED", "UNEXPECTED", "UNKNOWN_TAG"}:
            details = {"nfc_tag_id": scan.nfc_tag_id, "observed_location": scan.observed_location}
            item = snapshots.get(scan.expected_asset_id)
            if item is not None:
                details.update(
                    equipment_name_at_start=item.equipment_name_at_start,
                    asset_code_at_start=item.asset_code_at_start, expected_location=item.expected_location,
                )
            findings.append(InventoryAuditFinding(
                asset_id=scan.asset_id, expected_asset_id=scan.expected_asset_id, scan_id=scan.id,
                discrepancy_type=scan.result, description="Physical observation requires staff review.",
                details=details,
            ))
    for item in expected:
        if item.in_scope and item.id not in seen:
            if item.checkout_id_at_start is not None:
                kind = "EXPECTED_ON_LOAN"
            elif item.maintenance_id_at_start is not None or item.maintenance_hold_at_start:
                kind = "EXPECTED_MAINTENANCE"
            else:
                kind = "NOT_VERIFIED"
            findings.append(InventoryAuditFinding(
                asset_id=item.asset_id, expected_asset_id=item.id, scan_id=None, discrepancy_type=kind,
                description="Asset was not physically verified; snapshot custody is retained for review.",
                details={"availability_at_start": item.availability_at_start, "condition_at_start": item.condition_at_start},
            ))
        if item.in_scope or item.id in seen:
            start = _state_at_start(item)
            row = current.get(item.asset_id)
            now = _state_now(row) if row is not None else None
            if now != start:
                findings.append(InventoryAuditFinding(
                    asset_id=item.asset_id, expected_asset_id=item.id, scan_id=None, discrepancy_type="STATE_CHANGED",
                    description="Inventory, checkout, or maintenance state changed after the snapshot; review required.",
                    details={"snapshot": start, "observed": now},
                ))
            if item.id in seen and now is not None and (
                now["checkout_id"] is not None or now["maintenance_id"] is not None or now["maintenance_hold"]
            ):
                findings.append(InventoryAuditFinding(
                    asset_id=item.asset_id, expected_asset_id=item.id, scan_id=None, discrepancy_type="CUSTODY_REVIEW",
                    description="An observed asset has active custody or maintenance restrictions; review required.",
                    details={"observed": now},
                ))
    return sorted(findings, key=lambda finding: (
        finding.discrepancy_type, str(finding.asset_id or ""), str(finding.scan_id or ""),
    ))


@transaction(uncertain_commit_detail=UNCERTAIN)
def finalize_inventory_audit(
    db: Session, audit_id: UUID, data: InventoryAuditFinalize, actor: User,
) -> InventoryAuditResponse:
    _authorize(db, actor, FACULTY)
    audit = _audit(db, audit_id)
    _authorize(db, actor, FACULTY)
    _version(audit, data.expected_version)
    expected, scans = _records(db, audit)
    rows, observed_at = _inventory(db)
    staff = _authorize(db, actor, FACULTY)
    findings = _findings(expected, scans, {row["asset_id"]: row for row in rows})
    for finding in findings:
        _save(db, InventoryAuditDiscrepancy(
            id=uuid4(), audit_id=audit.id, detected_at=observed_at, **finding.model_dump(),
        ))
    audit.status = "COMPLETED"
    audit.reconciled_at = observed_at
    audit.completed_at = _now(db)
    audit.completed_by = staff.id
    audit.final_notes = data.notes
    audit.version += 1
    flush_stage(db, audit)
    return InventoryAuditResponse.model_validate(audit)


@transaction(uncertain_commit_detail=UNCERTAIN)
def cancel_inventory_audit(
    db: Session, audit_id: UUID, data: InventoryAuditCancel, actor: User,
) -> InventoryAuditResponse:
    _authorize(db, actor, CANCELLERS)
    audit = _audit(db, audit_id)
    staff = _authorize(db, actor, CANCELLERS)
    _version(audit, data.expected_version)
    audit.status = "CANCELLED"
    audit.cancelled_at = _now(db)
    audit.cancelled_by = staff.id
    audit.cancellation_reason = data.reason
    audit.version += 1
    flush_stage(db, audit)
    return InventoryAuditResponse.model_validate(audit)


def _pagination(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="Invalid pagination")


def _read_audit(db, audit_id, actor):
    clean_session(db)
    _authorize(db, actor)
    audit = _audit(db, audit_id, shared=True)
    _authorize(db, actor)
    return audit


def get_inventory_audit(db: Session, audit_id: UUID, actor: User) -> InventoryAuditResponse:
    with db.no_autoflush:
        audit = _read_audit(db, audit_id, actor)
        return InventoryAuditResponse.model_validate(audit)


def list_inventory_audits(db: Session, actor: User, limit=25, offset=0, status=None, location=None) -> InventoryAuditPage:
    _pagination(limit, offset)
    if status is not None and status not in {"IN_PROGRESS", "COMPLETED", "CANCELLED"}:
        raise HTTPException(status_code=422, detail="Invalid inventory audit status")
    with db.no_autoflush:
        clean_session(db)
        _authorize(db, actor)
        statement = select(InventoryAudit).order_by(InventoryAudit.started_at.desc(), InventoryAudit.id.desc())
        if status is not None:
            statement = statement.where(InventoryAudit.status == status)
        if location is not None:
            statement = statement.where(InventoryAudit.location == location)
        rows = db.scalars(statement.offset(offset).limit(limit + 1)).all()
        return InventoryAuditPage(items=[InventoryAuditResponse.model_validate(row) for row in rows[:limit]],
                                  limit=limit, offset=offset, has_more=len(rows) > limit)


def list_inventory_observations(db: Session, audit_id: UUID, actor: User, *, scans: bool, limit=25, offset=0):
    _pagination(limit, offset)
    with db.no_autoflush:
        audit = _read_audit(db, audit_id, actor)
        model = InventoryAuditScan if scans else InventoryAuditExpectedAsset
        statement = select(model).where(model.audit_id == audit.id)
        if scans:
            statement = statement.order_by(model.scanned_at, model.id)
            response, page = InventoryAuditScanResponse, InventoryAuditScanPage
        else:
            statement = statement.where(model.in_scope.is_(True)).order_by(model.asset_id)
            response, page = InventoryAuditExpectedResponse, InventoryAuditExpectedPage
        rows = db.scalars(statement.offset(offset).limit(limit + 1)).all()
        return page(items=[response.model_validate(row) for row in rows[:limit]],
                    limit=limit, offset=offset, has_more=len(rows) > limit)


def get_inventory_audit_report(db: Session, audit_id: UUID, actor: User, limit=25, offset=0) -> InventoryAuditReport:
    _pagination(limit, offset)
    with db.no_autoflush:
        # A shared parent lock prevents scans/closure changing between reads.
        audit = _read_audit(db, audit_id, actor)
        expected, scans = _records(db, audit)
        if audit.status == "IN_PROGRESS":
            rows, observed_at = _inventory(db)
            findings = _findings(expected, scans, {row["asset_id"]: row for row in rows})
        else:
            observed_at = audit.reconciled_at
            findings = [InventoryAuditFinding.model_validate(row) for row in db.scalars(
                select(InventoryAuditDiscrepancy).where(InventoryAuditDiscrepancy.audit_id == audit.id)
                .order_by(InventoryAuditDiscrepancy.discrepancy_type, InventoryAuditDiscrepancy.asset_id,
                          InventoryAuditDiscrepancy.scan_id, InventoryAuditDiscrepancy.id),
            ).all()]
        scope = {item.id for item in expected if item.in_scope}
        seen = {scan.expected_asset_id for scan in scans}
        return InventoryAuditReport(
            audit=InventoryAuditResponse.model_validate(audit),
            preview=audit.status == "IN_PROGRESS", reconciliation_complete=audit.status == "COMPLETED",
            observed_at=observed_at, expected_count=len(scope), verified_count=len(scope & seen),
            scan_count=len(scans), duplicate_count=sum(scan.result == "DUPLICATE" for scan in scans),
            finding_count=len(findings), findings=findings[offset:offset + limit],
            limit=limit, offset=offset, has_more=len(findings) > offset + limit,
        )
