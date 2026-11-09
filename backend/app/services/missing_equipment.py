"""Missing equipment management service.

Handles auditable workflows for declaring equipment MISSING and subsequently
reconciling it when recovered. Follows strict canonical lock ordering,
idempotency, and transaction isolation.
"""

from datetime import datetime
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.missing_equipment import (
    MissingEquipmentEvent,
    MissingEquipmentIncident,
)
from backend.app.models.user import User, UserRole
from backend.app.schemas.missing_equipment import (
    AssetMissingReconcileCreate,
    AssetMissingReportCreate,
    MissingEventResponse,
    MissingIncidentResponse,
)
from backend.app.services.inventory_transactions import flush_stage, transaction


STAFF = frozenset({UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value, UserRole.ADMIN.value})
ACTIVE_MAINTENANCE_STATUSES = ("OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION")


def _id(value, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Invalid {label}") from None


def _authorize(db: Session, actor: User, roles=STAFF) -> User:
    if actor is None or actor.id is None:
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    user = db.get(User, _id(actor.id, "actor ID"), populate_existing=True)
    if user is None or not user.is_active or user.role not in roles:
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    return user


def _locked(db: Session, model, *predicates):
    return db.scalar(
        select(model)
        .where(*predicates)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _now(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def _incident_view(
    incident: MissingEquipmentIncident,
    asset: EquipmentAsset,
) -> MissingIncidentResponse:
    return MissingIncidentResponse(
        id=incident.id,
        asset_id=incident.asset_id,
        operation_key=incident.operation_key,
        reconcile_key=incident.reconcile_key,
        status=incident.status,
        version=incident.version,
        reported_by=incident.reported_by,
        reported_at=incident.reported_at,
        report_reason=incident.report_reason,
        police_report_number=incident.police_report_number,
        reconciled_by=incident.reconciled_by,
        reconciled_at=incident.reconciled_at,
        reconcile_reason=incident.reconcile_reason,
        recovered_condition=incident.recovered_condition,
        previous_availability_status=incident.previous_availability_status,
        previous_condition_status=incident.previous_condition_status,
        resolved_availability_status=incident.resolved_availability_status,
        resolved_condition_status=incident.resolved_condition_status,
        asset_availability_status=asset.availability_status,
        asset_condition_status=asset.condition_status,
        asset_maintenance_hold=asset.maintenance_hold,
    )


def _event_view(event: MissingEquipmentEvent) -> MissingEventResponse:
    return MissingEventResponse(
        id=event.id,
        incident_id=event.incident_id,
        asset_id=event.asset_id,
        sequence=event.sequence,
        event_type=event.event_type,
        actor_id=event.actor_id,
        occurred_at=event.occurred_at,
        reason=event.reason,
        police_report_number=event.police_report_number,
        from_availability_status=event.from_availability_status,
        to_availability_status=event.to_availability_status,
        from_condition_status=event.from_condition_status,
        to_condition_status=event.to_condition_status,
        from_maintenance_hold=event.from_maintenance_hold,
        to_maintenance_hold=event.to_maintenance_hold,
    )


@transaction(uncertain_commit_detail="Missing report outcome is uncertain. Retry only with the same operation key.")
def report_asset_missing(
    db: Session,
    asset_id: UUID,
    data: AssetMissingReportCreate,
    actor: User,
) -> MissingIncidentResponse:
    """Report an asset as missing.

    Idempotency Contract:
    - data.idempotency_key is operation-scoped to missing reports (operation_key).
    - An identical retry with the same key and fingerprint returns the incident record
      reflecting its current lifecycle state and live asset state without re-executing
      mutations or creating duplicate events.
    - Historical incident fields (reported_at, report_reason, previous_*) remain immutable.
    """
    asset_id = _id(asset_id, "asset ID")
    actor_user = _authorize(db, actor, roles=STAFF)

    # 1. Canonical lock ordering: EquipmentAsset
    asset = _locked(db, EquipmentAsset, EquipmentAsset.id == asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Re-verify actor after acquiring lock
    actor_user = _authorize(db, actor, roles=STAFF)

    # 2. Canonical lock ordering: EquipmentCheckout
    checkout = _locked(
        db,
        EquipmentCheckout,
        EquipmentCheckout.asset_id == asset_id,
        EquipmentCheckout.status == "BORROWED",
    )

    # 3. Canonical lock ordering: MaintenanceRequest
    _maintenance = _locked(
        db,
        MaintenanceRequest,
        MaintenanceRequest.asset_id == asset_id,
        MaintenanceRequest.status.in_(ACTIVE_MAINTENANCE_STATUSES),
    )

    # Fingerprint for idempotency verification
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                str(asset_id),
                str(data.idempotency_key),
                str(actor_user.id),
                data.reason,
                data.police_report_number or "",
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    # Check for existing incident by operation_key
    existing_by_key = db.scalar(
        select(MissingEquipmentIncident).where(
            MissingEquipmentIncident.operation_key == data.idempotency_key
        )
    )
    if existing_by_key is not None:
        if existing_by_key.report_fingerprint == fingerprint:
            return _incident_view(existing_by_key, asset)
        raise HTTPException(
            status_code=409,
            detail="Missing report idempotency key was already used with different parameters",
        )

    # Check if asset is already reported missing
    open_incident = db.scalar(
        select(MissingEquipmentIncident).where(
            MissingEquipmentIncident.asset_id == asset_id,
            MissingEquipmentIncident.status == "OPEN",
        )
    )
    if open_incident is not None or asset.condition_status == "MISSING":
        raise HTTPException(
            status_code=409,
            detail="Asset is already reported missing",
        )

    # Cannot report retired assets missing
    if asset.availability_status == "RETIRED":
        raise HTTPException(
            status_code=409,
            detail="Cannot report retired equipment missing",
        )

    # Snapshot previous state
    before_avail = asset.availability_status
    before_cond = asset.condition_status
    before_hold = asset.maintenance_hold

    # Determine new asset state
    to_cond = "MISSING"
    to_hold = True
    if before_avail == "AVAILABLE":
        to_avail = "MAINTENANCE"
    elif before_avail == "BORROWED":
        to_avail = "BORROWED"
    elif before_avail == "MAINTENANCE":
        to_avail = "MAINTENANCE"
    elif before_avail == "RESERVED":
        to_avail = "RESERVED"
    else:
        to_avail = "MAINTENANCE"

    # Stage 1: Asset update
    asset.condition_status = to_cond
    asset.availability_status = to_avail
    asset.maintenance_hold = to_hold
    flush_stage(db, asset)

    now = _now(db)
    incident_id = uuid4()

    # Stage 2: Create MissingEquipmentIncident
    incident = MissingEquipmentIncident(
        id=incident_id,
        asset_id=asset_id,
        operation_key=data.idempotency_key,
        report_fingerprint=fingerprint,
        status="OPEN",
        version=1,
        reported_by=actor_user.id,
        reported_at=now,
        report_reason=data.reason,
        police_report_number=data.police_report_number,
        previous_availability_status=before_avail,
        previous_condition_status=before_cond,
    )
    db.add(incident)
    flush_stage(db, incident)

    # Stage 3: Create MissingEquipmentEvent
    event = MissingEquipmentEvent(
        id=uuid4(),
        incident_id=incident_id,
        asset_id=asset_id,
        sequence=1,
        event_type="REPORTED_MISSING",
        actor_id=actor_user.id,
        occurred_at=now,
        reason=data.reason,
        police_report_number=data.police_report_number,
        from_availability_status=before_avail,
        to_availability_status=to_avail,
        from_condition_status=before_cond,
        to_condition_status=to_cond,
        from_maintenance_hold=before_hold,
        to_maintenance_hold=to_hold,
    )
    db.add(event)
    flush_stage(db, event)

    return _incident_view(incident, asset)


@transaction(uncertain_commit_detail="Reconciliation outcome is uncertain. Retry only with the same operation key.")
def reconcile_missing_asset(
    db: Session,
    asset_id: UUID,
    data: AssetMissingReconcileCreate,
    actor: User,
) -> MissingIncidentResponse:
    """Reconcile a missing equipment incident upon physical recovery.

    Idempotency Contract:
    - data.idempotency_key is operation-scoped to missing reconciliations (reconcile_key).
    - An identical retry with the same key and fingerprint returns the incident record
      reflecting its current lifecycle state and live asset state without re-executing
      mutations or creating duplicate events.
    - Historical incident fields (reconciled_at, reconcile_reason, resolved_*) remain immutable.
    """
    asset_id = _id(asset_id, "asset ID")
    actor_user = _authorize(db, actor, roles=STAFF)

    # 1. Canonical lock ordering: EquipmentAsset
    asset = _locked(db, EquipmentAsset, EquipmentAsset.id == asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Re-verify actor after acquiring lock
    actor_user = _authorize(db, actor, roles=STAFF)

    # 2. Canonical lock ordering: EquipmentCheckout
    checkout = _locked(
        db,
        EquipmentCheckout,
        EquipmentCheckout.asset_id == asset_id,
        EquipmentCheckout.status == "BORROWED",
    )

    # 3. Canonical lock ordering: MaintenanceRequest
    active_maintenance = _locked(
        db,
        MaintenanceRequest,
        MaintenanceRequest.asset_id == asset_id,
        MaintenanceRequest.status.in_(ACTIVE_MAINTENANCE_STATUSES),
    )

    # 4. Canonical lock ordering: MissingEquipmentIncident
    incident = _locked(
        db,
        MissingEquipmentIncident,
        MissingEquipmentIncident.asset_id == asset_id,
        MissingEquipmentIncident.status == "OPEN",
    )

    # Fingerprint for reconciliation idempotency
    reconcile_fingerprint = hashlib.sha256(
        json.dumps(
            [
                str(asset_id),
                str(data.idempotency_key),
                str(actor_user.id),
                data.recovered_condition,
                data.reason,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    # Check for existing reconciliation by reconcile_key
    existing_by_reconcile_key = db.scalar(
        select(MissingEquipmentIncident).where(
            MissingEquipmentIncident.reconcile_key == data.idempotency_key
        )
    )
    if existing_by_reconcile_key is not None:
        if existing_by_reconcile_key.reconcile_fingerprint == reconcile_fingerprint:
            return _incident_view(existing_by_reconcile_key, asset)
        raise HTTPException(
            status_code=409,
            detail="Reconciliation idempotency key was already used with different parameters",
        )

    # Asset must be in MISSING state with an active OPEN incident
    if incident is None or asset.condition_status != "MISSING":
        raise HTTPException(
            status_code=409,
            detail="Asset does not have an active missing equipment incident",
        )

    before_avail = asset.availability_status
    before_cond = asset.condition_status
    before_hold = asset.maintenance_hold

    has_active_maintenance = active_maintenance is not None

    # Compute recovery availability and condition adhering to blocking conditions
    if checkout is not None:
        # Custody is preserved: recovery of physical equipment does not close loan
        to_avail = "BORROWED"
        to_cond = data.recovered_condition
        # Clear the missing-related maintenance hold when recovered as WORKING
        # and no independent maintenance request exists.
        to_hold = (data.recovered_condition == "DAMAGED") or has_active_maintenance
    elif active_maintenance is not None:
        # Maintenance hold preserved: repair/inspection remains active
        to_avail = "MAINTENANCE"
        to_cond = "UNDER_REPAIR" if active_maintenance.status == "IN_PROGRESS" else data.recovered_condition
        to_hold = True
    elif incident.previous_availability_status == "RESERVED":
        to_avail = "RESERVED"
        to_cond = data.recovered_condition
        to_hold = (data.recovered_condition == "DAMAGED")
    else:
        # Clean recovery
        if data.recovered_condition == "WORKING":
            to_avail = "AVAILABLE"
            to_cond = "WORKING"
            to_hold = False
        else:
            to_avail = "MAINTENANCE"
            to_cond = "DAMAGED"
            to_hold = True

    # Stage 1: Asset update
    asset.condition_status = to_cond
    asset.availability_status = to_avail
    asset.maintenance_hold = to_hold
    flush_stage(db, asset)

    now = _now(db)

    # Stage 2: Update MissingEquipmentIncident
    incident.status = "RECONCILED"
    incident.version = incident.version + 1
    incident.reconciled_by = actor_user.id
    incident.reconciled_at = now
    incident.reconcile_reason = data.reason
    incident.recovered_condition = data.recovered_condition
    incident.resolved_availability_status = to_avail
    incident.resolved_condition_status = to_cond
    incident.reconcile_key = data.idempotency_key
    incident.reconcile_fingerprint = reconcile_fingerprint
    flush_stage(db, incident)

    # Stage 3: Create MissingEquipmentEvent
    event = MissingEquipmentEvent(
        id=uuid4(),
        incident_id=incident.id,
        asset_id=asset_id,
        sequence=2,
        event_type="RECONCILED",
        actor_id=actor_user.id,
        occurred_at=now,
        reason=data.reason,
        police_report_number=None,
        from_availability_status=before_avail,
        to_availability_status=to_avail,
        from_condition_status=before_cond,
        to_condition_status=to_cond,
        from_maintenance_hold=before_hold,
        to_maintenance_hold=to_hold,
    )
    db.add(event)
    flush_stage(db, event)

    return _incident_view(incident, asset)


def list_missing_incidents(
    db: Session,
    asset_id: UUID,
    actor: User,
    limit: int = 50,
    offset: int = 0,
) -> list[MissingIncidentResponse]:
    asset_id = _id(asset_id, "asset ID")
    _authorize(db, actor, roles=STAFF)

    asset = db.get(EquipmentAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    incidents = db.scalars(
        select(MissingEquipmentIncident)
        .where(MissingEquipmentIncident.asset_id == asset_id)
        .order_by(MissingEquipmentIncident.reported_at.desc(), MissingEquipmentIncident.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return [_incident_view(inc, asset) for inc in incidents]


def list_incident_events(
    db: Session,
    asset_id: UUID,
    incident_id: UUID,
    actor: User,
    limit: int = 50,
    offset: int = 0,
) -> list[MissingEventResponse]:
    asset_id = _id(asset_id, "asset ID")
    incident_id = _id(incident_id, "incident ID")
    _authorize(db, actor, roles=STAFF)

    asset = db.get(EquipmentAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    incident = db.get(MissingEquipmentIncident, incident_id)
    if incident is None or incident.asset_id != asset_id:
        raise HTTPException(status_code=404, detail="Missing equipment incident not found")

    events = db.scalars(
        select(MissingEquipmentEvent)
        .where(MissingEquipmentEvent.incident_id == incident_id)
        .order_by(MissingEquipmentEvent.sequence.asc(), MissingEquipmentEvent.occurred_at.asc())
        .limit(limit)
        .offset(offset)
    ).all()

    return [_event_view(ev) for ev in events]

