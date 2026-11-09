"""FastAPI router for read-only inventory reporting and analytics."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import require_roles
from backend.app.database import get_db
from backend.app.models.user import UserRole
from backend.app.schemas.reports import (
    CategoryReportResponse,
    InventoryOverviewReport,
    MaintenanceReportPage,
    MissingReportPage,
    StockReportPage,
)
from backend.app.services.reports import (
    get_category_report,
    get_inventory_overview,
    get_maintenance_report,
    get_missing_report,
    get_stock_report,
)

router = APIRouter(
    prefix="/reports/inventory",
    tags=["Inventory Reporting & Analytics"],
)

staff_only = require_roles(
    UserRole.ADMIN,
    UserRole.FACULTY,
    UserRole.LAB_ASSISTANT,
)


@router.get(
    "/overview",
    response_model=InventoryOverviewReport,
)
def overview_report(
    db: Session = Depends(get_db),
    _actor=Depends(staff_only),
):
    """Retrieve comprehensive macro-level inventory status and operational alerts."""
    as_of = datetime.now(timezone.utc)
    return get_inventory_overview(db, as_of=as_of)


@router.get(
    "/categories",
    response_model=CategoryReportResponse,
)
def categories_report(
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _actor=Depends(staff_only),
):
    """Retrieve category-level breakdown of catalog items, physical assets, and quantity stock."""
    as_of = datetime.now(timezone.utc)
    return get_category_report(db, as_of=as_of, category_filter=category)


@router.get(
    "/stock",
    response_model=StockReportPage,
)
def stock_report(
    out_of_stock_only: bool = Query(default=False),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    _actor=Depends(staff_only),
):
    """Retrieve detailed stock balances and loan activity for quantity-tracked equipment."""
    as_of = datetime.now(timezone.utc)
    return get_stock_report(
        db,
        as_of=as_of,
        out_of_stock_only=out_of_stock_only,
        category=category,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/missing",
    response_model=MissingReportPage,
)
def missing_report(
    status: Literal["OPEN", "RECONCILED", "ALL"] = Query(default="OPEN"),
    category: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    _actor=Depends(staff_only),
):
    """Retrieve operational report of missing equipment incidents and durations."""
    as_of = datetime.now(timezone.utc)
    return get_missing_report(
        db,
        as_of=as_of,
        status_filter=status,
        category=category,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/maintenance",
    response_model=MaintenanceReportPage,
)
def maintenance_report(
    status: Literal[
        "ACTIVE", "OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION", "COMPLETED", "CANCELLED", "ALL"
    ] = Query(default="ACTIVE"),
    issue_type: Literal["DAMAGE", "MALFUNCTION", "INSPECTION"] | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, lt=2**63),
    db: Session = Depends(get_db),
    _actor=Depends(staff_only),
):
    """Retrieve operational report of equipment maintenance and repair workloads."""
    as_of = datetime.now(timezone.utc)
    return get_maintenance_report(
        db,
        as_of=as_of,
        status_filter=status,
        issue_type=issue_type,
        limit=limit,
        offset=offset,
    )

