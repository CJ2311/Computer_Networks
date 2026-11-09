from backend.app.schemas.asset import AssetBulkCreate
from backend.app.services.asset import create_asset_bulk
from backend.app.schemas.missing_equipment import (
    AssetMissingReportCreate,
    AssetMissingReconcileCreate,
    MissingIncidentResponse,
    MissingEventResponse,
)
from backend.app.services.missing_equipment import (
    report_asset_missing,
    reconcile_missing_asset,
    list_missing_incidents,
    list_incident_events,
)
from fastapi import APIRouter, Depends, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.app.models.equipment import EquipmentAsset
from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db, get_inventory_write_db
from backend.app.models.user import UserRole
from backend.app.schemas.asset import AssetCreate, AssetResponse, AssetUpdate
from backend.app.services.asset import create_asset, update_asset, lookup_asset

from uuid import UUID
from fastapi import HTTPException

router = APIRouter(
    prefix="/assets",
    tags=["Assets"],
)


@router.post(
    "",
    response_model=AssetResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_asset(
    asset_data: AssetCreate,
    db: Session = Depends(get_inventory_write_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return create_asset(db, asset_data, actor=_authorized_user)

@router.get(
    "",
    response_model=list[AssetResponse],
)
def list_assets(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return db.scalars(
        select(EquipmentAsset)
        .order_by(EquipmentAsset.asset_code, EquipmentAsset.id)
        .limit(limit).offset(offset)
    ).all()

@router.get("/lookup", response_model=AssetResponse)
def identify_asset(
    nfc_tag_id: str = Query(min_length=1, max_length=100),
    db: Session = Depends(get_db),
    actor=Depends(require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return lookup_asset(db, nfc_tag_id)

@router.get(
    "/{asset_id}",
    response_model=AssetResponse,
)
def get_asset(
    asset_id: UUID,
    db: Session = Depends(get_db),
    _authorized_user=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    asset = db.get(EquipmentAsset, asset_id)

    if asset is None:
        raise HTTPException(
            status_code=404,
            detail="Asset not found",
        )

    return asset

@router.patch("/{asset_id}", response_model=AssetResponse)
def patch_asset(
    asset_id: UUID,
    update_data: AssetUpdate,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN, UserRole.FACULTY, UserRole.LAB_ASSISTANT)),
):
    return update_asset(db, asset_id, update_data, actor)


@router.post("/bulk", response_model=list[AssetResponse], status_code=201)
def register_asset_bulk(
    data: AssetBulkCreate, db: Session = Depends(get_inventory_write_db),
    actor=Depends(require_roles(UserRole.ADMIN,UserRole.FACULTY,UserRole.LAB_ASSISTANT)),
):
    return create_asset_bulk(db,data.items,actor)


@router.post(
    "/{asset_id}/report-missing",
    response_model=MissingIncidentResponse,
    status_code=status.HTTP_200_OK,
)
def report_missing(
    asset_id: UUID,
    data: AssetMissingReportCreate,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return report_asset_missing(db, asset_id, data, actor=actor)


@router.post(
    "/{asset_id}/reconcile-missing",
    response_model=MissingIncidentResponse,
    status_code=status.HTTP_200_OK,
)
def reconcile_missing(
    asset_id: UUID,
    data: AssetMissingReconcileCreate,
    db: Session = Depends(get_inventory_write_db),
    actor=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return reconcile_missing_asset(db, asset_id, data, actor=actor)


@router.get(
    "/{asset_id}/missing-incidents",
    response_model=list[MissingIncidentResponse],
)
def get_missing_incidents(
    asset_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    actor=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return list_missing_incidents(db, asset_id, actor=actor, limit=limit, offset=offset)


@router.get(
    "/{asset_id}/missing-incidents/{incident_id}/events",
    response_model=list[MissingEventResponse],
)
def get_incident_events(
    asset_id: UUID,
    incident_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    actor=Depends(
        require_roles(
            UserRole.ADMIN,
            UserRole.FACULTY,
            UserRole.LAB_ASSISTANT,
        )
    ),
):
    return list_incident_events(
        db, asset_id, incident_id, actor=actor, limit=limit, offset=offset
    )

