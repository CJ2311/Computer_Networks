"""Standalone maintenance transactions. No routes or checkout/return integration.

Write operations own the supplied clean Session's transaction. They must not be
nested inside another business operation; future integration needs an explicit
shared transaction boundary. Results are snapshots, never expiring ORM objects.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import EquipmentAsset
from backend.app.models.maintenance import MaintenanceEvent, MaintenanceRequest
from backend.app.models.user import User, UserRole


STAFF = frozenset({UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value, UserRole.ADMIN.value})
MANAGERS = frozenset({UserRole.FACULTY.value, UserRole.ADMIN.value})
REPAIRERS = frozenset({UserRole.FACULTY.value, UserRole.LAB_ASSISTANT.value})
ACTIVE = ("OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION")


@dataclass(frozen=True)
class MaintenanceRecord:
    id: UUID
    asset_id: UUID
    issue_type: str
    issue_description: str
    status: str
    reported_by: UUID
    reported_at: datetime
    assigned_to: UUID | None
    started_at: datetime | None
    repair_completed_at: datetime | None
    repair_completed_by: UUID | None
    verified_at: datetime | None
    verified_by: UUID | None
    returned_to_service_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by: UUID | None
    cancellation_reason: str | None
    recovery_of_request_id: UUID | None
    version: int
    condition_status: str
    availability_status: str
    maintenance_hold: bool


@dataclass(frozen=True)
class MaintenanceEventRecord:
    id: UUID
    request_id: UUID
    sequence: int
    event_type: str
    actor_id: UUID
    occurred_at: datetime
    note: str | None
    from_status: str | None
    to_status: str | None
    from_assigned_to: UUID | None
    to_assigned_to: UUID | None
    from_condition: str | None
    to_condition: str | None
    from_availability: str | None
    to_availability: str | None
    from_maintenance_hold: bool | None
    to_maintenance_hold: bool | None


@dataclass(frozen=True)
class MaintenanceEventPage:
    items: tuple[MaintenanceEventRecord, ...]
    limit: int
    offset: int
    has_more: bool


def _clean_session(db: Session) -> None:
    if db.new or db.dirty or db.deleted or db.in_nested_transaction():
        raise HTTPException(status_code=409, detail="Maintenance requires a clean, non-nested session")


def _transaction(operation):
    @wraps(operation)
    def run(db: Session, *args, **kwargs):
        _clean_session(db)
        try:
            with db.no_autoflush:
                result = operation(db, *args, **kwargs)
                _clean_session(db)  # commit() must not implicitly flush unexpected work.
                db.commit()
                return result
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Maintenance conflicts with current database state") from None
        except DBAPIError as error:
            db.rollback()
            if getattr(error.orig, "sqlstate", None) in {"40001", "40P01", "55P03"}:
                raise HTTPException(status_code=409, detail="Concurrent maintenance operation; reload and retry") from None
            raise
        except Exception:
            db.rollback()
            raise
    return run


def _id(value, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Invalid {label}") from None


def _text(value, label: str, maximum: int, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HTTPException(status_code=422, detail=f"{label} must be nonblank and at most {maximum} characters")
    return value  # Preserve the original report/notes, including intentional formatting.


def _authorize(db: Session, actor: User, roles=STAFF) -> User:
    # Re-read persisted roles/activation, not merely attributes supplied by a caller.
    if actor is None or actor.id is None:
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    user = db.get(User, _id(actor.id, "actor ID"), populate_existing=True)
    if user is None or not user.is_active or user.role not in roles:
        raise HTTPException(status_code=403, detail="Active authorized staff required")
    return user


def _locked(db: Session, model, *predicates):
    return db.scalar(select(model).where(*predicates).with_for_update().execution_options(populate_existing=True))


def _asset_and_checkout(db: Session, asset_id: UUID):
    asset = _locked(db, EquipmentAsset, EquipmentAsset.id == asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    checkout = _locked(db, EquipmentCheckout, EquipmentCheckout.asset_id == asset_id,
                       EquipmentCheckout.status == "BORROWED")
    if (checkout is not None) != (asset.availability_status == "BORROWED"):
        raise HTTPException(status_code=409, detail="Asset custody is inconsistent with checkout records")
    return asset, checkout


def _request_context(db: Session, request_id: UUID, actor: User, roles, expected_version: int):
    _authorize(db, actor, roles)
    request_id = _id(request_id, "request ID")
    if type(expected_version) is not int or not 1 <= expected_version < 2**31 - 1:
        raise HTTPException(status_code=422, detail="Expected version must be a positive, incrementable integer")
    asset_id = db.scalar(select(MaintenanceRequest.asset_id).where(MaintenanceRequest.id == request_id))
    if asset_id is None:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    asset, checkout = _asset_and_checkout(db, asset_id)
    request = _locked(db, MaintenanceRequest, MaintenanceRequest.id == request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    user = _authorize(db, actor, roles)  # Activation/role may have changed during the lock wait.
    if request.asset_id != asset.id or not asset.maintenance_hold:
        raise HTTPException(status_code=409, detail="Maintenance asset/hold state is inconsistent")
    if request.version != expected_version:
        raise HTTPException(status_code=409, detail="Maintenance version conflict; reload the request")
    if request.status not in ACTIVE:
        raise HTTPException(status_code=409, detail="Maintenance request is terminal")
    return asset, checkout, request, user


def _require_status(request: MaintenanceRequest, *statuses):
    if request.status not in statuses:
        raise HTTPException(status_code=409, detail="Invalid maintenance transition")


def _physical_custody(asset: EquipmentAsset, checkout):
    if checkout is not None or asset.availability_status not in {"MAINTENANCE", "RESERVED"} or asset.condition_status == "MISSING":
        raise HTTPException(status_code=409, detail="Equipment must be physically returned and held for maintenance")


def _assigned(request: MaintenanceRequest, user: User):
    if request.assigned_to != user.id:
        raise HTTPException(status_code=403, detail="Only the assigned repairer may perform this operation")


def _now(db: Session, request: MaintenanceRequest | None = None):
    now = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
    if request is not None and now < max(t for t in (
        request.reported_at, request.started_at, request.repair_completed_at,
        request.verified_at, request.cancelled_at,
    ) if t is not None):
        raise HTTPException(status_code=409, detail="Database clock precedes recorded maintenance work")
    return now


def _state(request: MaintenanceRequest | None, asset: EquipmentAsset):
    return dict(status=request.status if request else None,
                assigned_to=request.assigned_to if request else None,
                condition=asset.condition_status, availability=asset.availability_status,
                maintenance_hold=asset.maintenance_hold)


def _snapshot(request: MaintenanceRequest, asset: EquipmentAsset) -> MaintenanceRecord:
    values = {column.name: getattr(request, column.name) for column in MaintenanceRequest.__table__.columns}
    return MaintenanceRecord(**values, condition_status=asset.condition_status,
                             availability_status=asset.availability_status, maintenance_hold=asset.maintenance_hold)


def _flush_stage(db: Session, expected) -> None:
    # A full flush is safe only when this stage owns every pending change.
    # Reject unexpected work rather than expunging it or flushing it early.
    if db.deleted or any(obj is not expected for obj in (*db.new, *db.dirty)):
        raise HTTPException(status_code=409, detail="Unexpected pending ORM changes during maintenance")
    db.flush()


def _apply_changes(instance, changes):
    for name, value in changes.items():
        setattr(instance, name, value)


def _persist(db, request, asset, actor_id, now, before, event_type, note=None, *,
             request_changes=None, asset_changes=None, new=False, release=False):
    request_changes = dict(request_changes or {})
    asset_changes = dict(asset_changes or {})
    request_changes["version"] = request.version if new else request.version + 1
    # Plan the event's final state without making later-stage objects dirty.
    after = dict(status=request_changes.get("status", request.status),
                 assigned_to=request_changes.get("assigned_to", request.assigned_to),
                 condition=asset_changes.get("condition_status", asset.condition_status),
                 availability=asset_changes.get("availability_status", asset.availability_status),
                 maintenance_hold=asset_changes.get("maintenance_hold", asset.maintenance_hold))
    event = MaintenanceEvent(
        id=uuid4(), request_id=request.id, sequence=request_changes["version"],
        event_type=event_type, actor_id=actor_id, occurred_at=now, note=note,
        **{"from_" + key: value for key, value in before.items()},
        **{"to_" + key: value for key, value in after.items()},
    )
    # Only mutate/enroll each object when its stage begins. The circular audit
    # FK remains deferred until commit; each request flush precedes event INSERT.
    if not release:
        _apply_changes(asset, asset_changes)
        _flush_stage(db, asset)
    _apply_changes(request, request_changes)
    if new:
        db.add(request)
    _flush_stage(db, request)
    db.add(event)
    _flush_stage(db, event)
    if release:
        # The database must see COMPLETED and its audit event before the hold
        # changes, so even the in-memory asset mutation is delayed until here.
        _apply_changes(asset, asset_changes)
        _flush_stage(db, asset)
    return _snapshot(request, asset)


def _report(db, asset_id, actor, issue_type, issue_description, recovery_of_request_id=None):
    _authorize(db, actor)
    asset_id = _id(asset_id, "asset ID")
    if not isinstance(issue_type, str) or issue_type not in {"DAMAGE", "MALFUNCTION", "INSPECTION"}:
        raise HTTPException(status_code=422, detail="Invalid maintenance issue type")
    description = _text(issue_description, "Issue description", 4000)
    if recovery_of_request_id is not None:
        recovery_of_request_id = _id(recovery_of_request_id, "recovery request ID")
        if issue_type != "INSPECTION":
            raise HTTPException(status_code=422, detail="Recovery requires an INSPECTION request")
    asset, checkout = _asset_and_checkout(db, asset_id)
    if _locked(db, MaintenanceRequest, MaintenanceRequest.asset_id == asset_id,
               MaintenanceRequest.status.in_(ACTIVE)) is not None:
        raise HTTPException(status_code=409, detail="Asset already has active maintenance")
    parent = None
    if recovery_of_request_id is not None:
        parent = _locked(db, MaintenanceRequest, MaintenanceRequest.id == recovery_of_request_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Cancelled maintenance request not found")
        if parent.asset_id != asset.id or parent.status != "CANCELLED" or not asset.maintenance_hold:
            raise HTTPException(status_code=409, detail="Recovery requires the same held asset and a cancelled request")
    if asset.availability_status == "RETIRED" or asset.condition_status == "MISSING":
        raise HTTPException(status_code=409, detail="Retired or missing assets require separate reconciliation")
    user = _authorize(db, actor)
    now = _now(db, parent)
    before = _state(None, asset)
    asset_changes = {"maintenance_hold": True}
    if issue_type in {"DAMAGE", "MALFUNCTION"}:
        asset_changes["condition_status"] = "DAMAGED"
    if asset.availability_status not in {"BORROWED", "RESERVED"}:
        asset_changes["availability_status"] = "MAINTENANCE"
    request = MaintenanceRequest(id=uuid4(), asset_id=asset.id, issue_type=issue_type,
                                 issue_description=description, status="OPEN", reported_by=user.id,
                                 reported_at=now, recovery_of_request_id=recovery_of_request_id, version=1)
    return _persist(db, request, asset, user.id, now, before, "REPORTED", asset_changes=asset_changes, new=True)


@_transaction
def report_maintenance_issue(db: Session, asset_id: UUID, actor: User, issue_type: str,
                             issue_description: str, recovery_of_request_id: UUID | None = None) -> MaintenanceRecord:
    return _report(db, asset_id, actor, issue_type, issue_description, recovery_of_request_id)


@_transaction
def recover_cancelled_request(db: Session, request_id: UUID, actor: User, issue_description: str) -> MaintenanceRecord:
    _authorize(db, actor)
    request_id = _id(request_id, "request ID")
    asset_id = db.scalar(select(MaintenanceRequest.asset_id).where(MaintenanceRequest.id == request_id))
    if asset_id is None:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    return _report(db, asset_id, actor, "INSPECTION", issue_description, request_id)


@_transaction
def assign_maintenance_request(db: Session, request_id: UUID, actor: User,
                               assignee_id: UUID, expected_version: int) -> MaintenanceRecord:
    asset, checkout, request, user = _request_context(db, request_id, actor, MANAGERS, expected_version)
    _require_status(request, "OPEN", "IN_PROGRESS")
    assignee = db.get(User, _id(assignee_id, "assignee ID"), populate_existing=True)
    if assignee is None:
        raise HTTPException(status_code=404, detail="Assignee not found")
    if not assignee.is_active or assignee.role not in REPAIRERS:
        raise HTTPException(status_code=403, detail="Assignee must be active faculty or a lab assistant")
    if request.assigned_to == assignee.id:
        raise HTTPException(status_code=409, detail="Request is already assigned to that user")
    before = _state(request, asset)
    now = _now(db, request)
    return _persist(db, request, asset, user.id, now, before, "ASSIGNED",
                    request_changes={"assigned_to": assignee.id})


@_transaction
def start_repair(db: Session, request_id: UUID, actor: User, expected_version: int) -> MaintenanceRecord:
    asset, checkout, request, user = _request_context(db, request_id, actor, REPAIRERS, expected_version)
    _require_status(request, "OPEN")
    _assigned(request, user)
    _physical_custody(asset, checkout)
    before = _state(request, asset)
    now = _now(db, request)
    return _persist(db, request, asset, user.id, now, before, "STARTED",
                    request_changes={"status": "IN_PROGRESS", "started_at": now},
                    asset_changes={"condition_status": "UNDER_REPAIR"})


@_transaction
def submit_repair_for_verification(db: Session, request_id: UUID, actor: User,
                                    expected_version: int, repair_notes: str) -> MaintenanceRecord:
    asset, checkout, request, user = _request_context(db, request_id, actor, REPAIRERS, expected_version)
    _require_status(request, "IN_PROGRESS")
    _assigned(request, user)
    _physical_custody(asset, checkout)
    note = _text(repair_notes, "Repair notes", 5000)
    before = _state(request, asset)
    now = _now(db, request)
    return _persist(db, request, asset, user.id, now, before, "REPAIR_COMPLETED", note,
                    request_changes={"status": "AWAITING_VERIFICATION", "repair_completed_at": now,
                                     "repair_completed_by": user.id},
                    asset_changes={"condition_status": "WORKING"})  # Hold remains until verification.


@_transaction
def verify_repair(db: Session, request_id: UUID, actor: User, expected_version: int,
                  outcome: str, notes: str) -> MaintenanceRecord:
    asset, checkout, request, user = _request_context(db, request_id, actor, MANAGERS, expected_version)
    _require_status(request, "AWAITING_VERIFICATION")
    _physical_custody(asset, checkout)
    if request.repair_completed_by == user.id:
        raise HTTPException(status_code=403, detail="Independent verification is required")
    if not isinstance(outcome, str) or outcome not in {"PASS", "FAIL"}:
        raise HTTPException(status_code=422, detail="Verification outcome must be PASS or FAIL")
    note = _text(notes, "Verification notes", 5000)
    before = _state(request, asset)
    now = _now(db, request)
    if outcome == "FAIL":
        return _persist(db, request, asset, user.id, now, before, "VERIFICATION_FAILED", note,
                        request_changes={"status": "IN_PROGRESS"},
                        asset_changes={"condition_status": "UNDER_REPAIR"})
    return _persist(db, request, asset, user.id, now, before, "VERIFIED", note,
                    request_changes={"status": "COMPLETED", "verified_at": now, "verified_by": user.id,
                                     "returned_to_service_at": now},
                    asset_changes={"condition_status": "WORKING", "maintenance_hold": False,
                                   "availability_status": "AVAILABLE" if asset.availability_status == "MAINTENANCE" else asset.availability_status},
                    release=True)


@_transaction
def cancel_maintenance_request(db: Session, request_id: UUID, actor: User,
                               expected_version: int, reason: str) -> MaintenanceRecord:
    asset, checkout, request, user = _request_context(db, request_id, actor, MANAGERS, expected_version)
    reason = _text(reason, "Cancellation reason", 4000)
    before = _state(request, asset)
    now = _now(db, request)
    return _persist(db, request, asset, user.id, now, before, "CANCELLED", reason,
                    request_changes={"status": "CANCELLED", "cancelled_at": now,
                                     "cancelled_by": user.id, "cancellation_reason": reason})


def get_maintenance_request(db: Session, request_id: UUID, actor: User) -> MaintenanceRecord:
    _clean_session(db)
    with db.no_autoflush:
        _authorize(db, actor)
        row = db.execute(select(*MaintenanceRequest.__table__.c,
                                EquipmentAsset.condition_status, EquipmentAsset.availability_status,
                                EquipmentAsset.maintenance_hold).join(
                                    EquipmentAsset, EquipmentAsset.id == MaintenanceRequest.asset_id
                                ).where(MaintenanceRequest.id == _id(request_id, "request ID"))).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Maintenance request not found")
        return MaintenanceRecord(**row)


def list_maintenance_events(db: Session, request_id: UUID, actor: User,
                            limit: int = 25, offset: int = 0) -> MaintenanceEventPage:
    _clean_session(db)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset < 2**63:
        raise HTTPException(status_code=422, detail="Invalid maintenance history pagination")
    with db.no_autoflush:
        _authorize(db, actor)
        request_id = _id(request_id, "request ID")
        if db.scalar(select(MaintenanceRequest.id).where(MaintenanceRequest.id == request_id)) is None:
            raise HTTPException(status_code=404, detail="Maintenance request not found")
        rows = db.execute(select(*MaintenanceEvent.__table__.c).where(
            MaintenanceEvent.request_id == request_id
        ).order_by(MaintenanceEvent.sequence).limit(limit + 1).offset(offset)).mappings().all()
        return MaintenanceEventPage(tuple(MaintenanceEventRecord(**row) for row in rows[:limit]), limit, offset, len(rows) > limit)
