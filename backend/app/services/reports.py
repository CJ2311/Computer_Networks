"""Read-only reporting and analytics services for laboratory equipment inventory."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.models.checkout import EquipmentCheckout
from backend.app.models.equipment import Equipment, EquipmentAsset, EquipmentStock
from backend.app.models.maintenance import MaintenanceRequest
from backend.app.models.missing_equipment import MissingEquipmentIncident
from backend.app.models.operations import QuantityLoan
from backend.app.models.user import User
from backend.app.schemas.reports import (
    AssetAvailabilitySummary,
    AssetConditionSummary,
    CategoryReportResponse,
    CategoryRollup,
    InventoryOverviewReport,
    MaintenanceReportItem,
    MaintenanceReportPage,
    MissingReportItem,
    MissingReportPage,
    OperationalAlertsSummary,
    QuantityStockSummary,
    StockReportItem,
    StockReportPage,
)


def _current_timestamp() -> datetime:
    return datetime.now(timezone.utc)


def get_inventory_overview(
    db: Session,
    as_of: datetime | None = None,
) -> InventoryOverviewReport:
    """Generate high-level inventory overview using a single consolidated SQL query."""
    if as_of is None:
        as_of = _current_timestamp()
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    # Consolidated single SQL query using CASE-based aggregations and subqueries
    overview_query = text("""
        WITH asset_counts AS (
            SELECT
                COUNT(*) AS total_assets,
                COUNT(CASE WHEN availability_status = 'AVAILABLE' THEN 1 END) AS avail_count,
                COUNT(CASE WHEN availability_status = 'BORROWED' THEN 1 END) AS borrowed_count,
                COUNT(CASE WHEN availability_status = 'RESERVED' THEN 1 END) AS reserved_count,
                COUNT(CASE WHEN availability_status = 'MAINTENANCE' THEN 1 END) AS maintenance_count,
                COUNT(CASE WHEN availability_status = 'RETIRED' THEN 1 END) AS retired_count,
                COUNT(CASE WHEN condition_status = 'WORKING' THEN 1 END) AS working_count,
                COUNT(CASE WHEN condition_status = 'DAMAGED' THEN 1 END) AS damaged_count,
                COUNT(CASE WHEN condition_status = 'UNDER_REPAIR' THEN 1 END) AS repair_count,
                COUNT(CASE WHEN condition_status = 'MISSING' THEN 1 END) AS missing_count,
                COUNT(CASE WHEN maintenance_hold = TRUE THEN 1 END) AS hold_count
            FROM equipment_assets
        ),
        stock_counts AS (
            SELECT
                COALESCE(SUM(quantity), 0) AS on_hand_units
            FROM equipment_stock
        ),
        loaned_stock AS (
            SELECT
                COALESCE(SUM(quantity), 0) AS loaned_units,
                COUNT(*) AS active_quantity_loans,
                COUNT(CASE WHEN due_at < :as_of THEN 1 END) AS overdue_quantity_loans
            FROM quantity_loans
            WHERE returned_at IS NULL
        ),
        catalog_counts AS (
            SELECT
                COUNT(*) AS total_catalog,
                COUNT(CASE WHEN tracking_type = 'INDIVIDUAL' THEN 1 END) AS ind_catalog,
                COUNT(CASE WHEN tracking_type = 'QUANTITY' THEN 1 END) AS qty_catalog
            FROM equipment
        ),
        checkout_alerts AS (
            SELECT
                COUNT(*) AS active_checkouts,
                COUNT(CASE WHEN due_at < :as_of THEN 1 END) AS overdue_checkouts
            FROM equipment_checkouts
            WHERE status = 'BORROWED'
        ),
        incident_alerts AS (
            SELECT
                COUNT(*) AS open_incidents
            FROM missing_equipment_incidents
            WHERE status = 'OPEN'
        ),
        maintenance_alerts AS (
            SELECT
                COUNT(*) AS active_maintenance
            FROM maintenance_requests
            WHERE status IN ('OPEN', 'IN_PROGRESS', 'AWAITING_VERIFICATION')
        )
        SELECT
            c.total_catalog, c.ind_catalog, c.qty_catalog,
            a.total_assets, a.avail_count, a.borrowed_count, a.reserved_count, a.maintenance_count, a.retired_count,
            a.working_count, a.damaged_count, a.repair_count, a.missing_count, a.hold_count,
            s.on_hand_units,
            l.loaned_units, l.active_quantity_loans, l.overdue_quantity_loans,
            co.active_checkouts, co.overdue_checkouts,
            ia.open_incidents,
            ma.active_maintenance
        FROM catalog_counts c, asset_counts a, stock_counts s, loaned_stock l, checkout_alerts co, incident_alerts ia, maintenance_alerts ma;
    """)

    row = db.execute(overview_query, {"as_of": as_of}).mappings().one()

    on_hand = int(row["on_hand_units"] or 0)
    loaned = int(row["loaned_units"] or 0)
    accounted = on_hand + loaned

    return InventoryOverviewReport(
        as_of=as_of,
        total_catalog_items=int(row["total_catalog"] or 0),
        individual_catalog_items=int(row["ind_catalog"] or 0),
        quantity_catalog_items=int(row["qty_catalog"] or 0),
        assets=AssetAvailabilitySummary(
            available=int(row["avail_count"] or 0),
            borrowed=int(row["borrowed_count"] or 0),
            reserved=int(row["reserved_count"] or 0),
            maintenance=int(row["maintenance_count"] or 0),
            retired=int(row["retired_count"] or 0),
            total_physical_assets=int(row["total_assets"] or 0),
        ),
        conditions=AssetConditionSummary(
            working=int(row["working_count"] or 0),
            damaged=int(row["damaged_count"] or 0),
            under_repair=int(row["repair_count"] or 0),
            missing=int(row["missing_count"] or 0),
            total_physical_assets=int(row["total_assets"] or 0),
        ),
        quantity_stock=QuantityStockSummary(
            catalog_items=int(row["qty_catalog"] or 0),
            on_hand_units=on_hand,
            active_loaned_units=loaned,
            accounted_units=accounted,
        ),
        alerts=OperationalAlertsSummary(
            active_individual_checkouts=int(row["active_checkouts"] or 0),
            overdue_individual_checkouts=int(row["overdue_checkouts"] or 0),
            active_quantity_loans=int(row["active_quantity_loans"] or 0),
            overdue_quantity_loans=int(row["overdue_quantity_loans"] or 0),
            open_missing_incidents=int(row["open_incidents"] or 0),
            active_maintenance_requests=int(row["active_maintenance"] or 0),
            assets_on_maintenance_hold=int(row["hold_count"] or 0),
        ),
    )


def get_category_report(
    db: Session,
    as_of: datetime | None = None,
    category_filter: str | None = None,
) -> CategoryReportResponse:
    """Rollup metrics grouped by equipment category."""
    if as_of is None:
        as_of = _current_timestamp()
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    filter_clause = "WHERE c.category = :category" if category_filter else ""

    category_sql = text(f"""
        WITH cat_equip AS (
            SELECT
                category,
                COUNT(*) AS catalog_items,
                COUNT(CASE WHEN tracking_type = 'INDIVIDUAL' THEN 1 END) AS ind_catalog,
                COUNT(CASE WHEN tracking_type = 'QUANTITY' THEN 1 END) AS qty_catalog
            FROM equipment
            GROUP BY category
        ),
        cat_assets AS (
            SELECT
                e.category,
                COUNT(a.id) AS total_physical_assets,
                COUNT(CASE WHEN a.availability_status = 'AVAILABLE' THEN 1 END) AS available_assets,
                COUNT(CASE WHEN a.availability_status = 'BORROWED' THEN 1 END) AS borrowed_assets,
                COUNT(CASE WHEN a.availability_status = 'RESERVED' THEN 1 END) AS reserved_assets,
                COUNT(CASE WHEN a.availability_status = 'MAINTENANCE' THEN 1 END) AS maintenance_assets,
                COUNT(CASE WHEN a.availability_status = 'RETIRED' THEN 1 END) AS retired_assets,
                COUNT(CASE WHEN a.condition_status = 'WORKING' THEN 1 END) AS working_assets,
                COUNT(CASE WHEN a.condition_status = 'DAMAGED' THEN 1 END) AS damaged_assets,
                COUNT(CASE WHEN a.condition_status = 'UNDER_REPAIR' THEN 1 END) AS under_repair_assets,
                COUNT(CASE WHEN a.condition_status = 'MISSING' THEN 1 END) AS missing_assets
            FROM equipment e
            JOIN equipment_assets a ON a.equipment_id = e.id
            GROUP BY e.category
        ),
        cat_stock AS (
            SELECT
                e.category,
                COALESCE(SUM(s.quantity), 0) AS quantity_on_hand
            FROM equipment e
            JOIN equipment_stock s ON s.equipment_id = e.id
            GROUP BY e.category
        ),
        cat_loans AS (
            SELECT
                e.category,
                COALESCE(SUM(l.quantity), 0) AS quantity_on_loan
            FROM equipment e
            JOIN quantity_loans l ON l.equipment_id = e.id
            WHERE l.returned_at IS NULL
            GROUP BY e.category
        )
        SELECT
            c.category,
            c.catalog_items,
            c.ind_catalog,
            c.qty_catalog,
            COALESCE(a.total_physical_assets, 0) AS total_physical_assets,
            COALESCE(a.available_assets, 0) AS available_assets,
            COALESCE(a.borrowed_assets, 0) AS borrowed_assets,
            COALESCE(a.reserved_assets, 0) AS reserved_assets,
            COALESCE(a.maintenance_assets, 0) AS maintenance_assets,
            COALESCE(a.retired_assets, 0) AS retired_assets,
            COALESCE(a.working_assets, 0) AS working_assets,
            COALESCE(a.damaged_assets, 0) AS damaged_assets,
            COALESCE(a.under_repair_assets, 0) AS under_repair_assets,
            COALESCE(a.missing_assets, 0) AS missing_assets,
            COALESCE(s.quantity_on_hand, 0) AS quantity_on_hand,
            COALESCE(l.quantity_on_loan, 0) AS quantity_on_loan
        FROM cat_equip c
        LEFT JOIN cat_assets a ON a.category = c.category
        LEFT JOIN cat_stock s ON s.category = c.category
        LEFT JOIN cat_loans l ON l.category = c.category
        {filter_clause}
        ORDER BY c.category;
    """)

    params = {"category": category_filter} if category_filter else {}
    rows = db.execute(category_sql, params).mappings().all()

    categories = [
        CategoryRollup(
            category=row["category"],
            catalog_items=int(row["catalog_items"] or 0),
            individual_catalog_items=int(row["ind_catalog"] or 0),
            quantity_catalog_items=int(row["qty_catalog"] or 0),
            total_physical_assets=int(row["total_physical_assets"] or 0),
            available_assets=int(row["available_assets"] or 0),
            borrowed_assets=int(row["borrowed_assets"] or 0),
            reserved_assets=int(row["reserved_assets"] or 0),
            maintenance_assets=int(row["maintenance_assets"] or 0),
            retired_assets=int(row["retired_assets"] or 0),
            working_assets=int(row["working_assets"] or 0),
            damaged_assets=int(row["damaged_assets"] or 0),
            under_repair_assets=int(row["under_repair_assets"] or 0),
            missing_assets=int(row["missing_assets"] or 0),
            quantity_on_hand=int(row["quantity_on_hand"] or 0),
            quantity_on_loan=int(row["quantity_on_loan"] or 0),
            accounted_quantity_units=int(row["quantity_on_hand"] or 0) + int(row["quantity_on_loan"] or 0),
        )
        for row in rows
    ]

    return CategoryReportResponse(as_of=as_of, categories=categories)


def get_stock_report(
    db: Session,
    as_of: datetime | None = None,
    out_of_stock_only: bool = False,
    category: str | None = None,
    search: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> StockReportPage:
    """Detailed stock report for quantity-tracked equipment."""
    if as_of is None:
        as_of = _current_timestamp()
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    # Subquery for active loans per quantity equipment
    loans_subq = (
        select(
            QuantityLoan.equipment_id,
            func.coalesce(func.sum(QuantityLoan.quantity), 0).label("loaned_qty"),
            func.count(QuantityLoan.id).label("loan_count"),
            func.count(
                func.nullif(QuantityLoan.due_at < as_of, False)
            ).label("overdue_count"),
        )
        .where(QuantityLoan.returned_at.is_(None))
        .group_by(QuantityLoan.equipment_id)
        .subquery()
    )

    base_query = (
        select(
            Equipment.id.label("equipment_id"),
            Equipment.name,
            Equipment.category,
            Equipment.default_location,
            func.coalesce(EquipmentStock.quantity, 0).label("on_hand_quantity"),
            func.coalesce(loans_subq.c.loaned_qty, 0).label("active_loaned_quantity"),
            func.coalesce(loans_subq.c.loan_count, 0).label("active_loan_count"),
            func.coalesce(loans_subq.c.overdue_count, 0).label("overdue_loan_count"),
        )
        .outerjoin(EquipmentStock, EquipmentStock.equipment_id == Equipment.id)
        .outerjoin(loans_subq, loans_subq.c.equipment_id == Equipment.id)
        .where(Equipment.tracking_type == "QUANTITY")
    )

    if category:
        base_query = base_query.where(Equipment.category == category)
    if search:
        term = f"%{search.strip()}%"
        base_query = base_query.where(
            (Equipment.name.ilike(term)) | (Equipment.model_number.ilike(term))
        )
    if out_of_stock_only:
        base_query = base_query.where(
            func.coalesce(EquipmentStock.quantity, 0) == 0
        )

    # Count total matching rows
    count_query = select(func.count()).select_from(base_query.subquery())
    total = db.scalar(count_query) or 0

    # Paginate with deterministic sorting
    paginated_query = (
        base_query
        .order_by(Equipment.name.asc(), Equipment.id.asc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(paginated_query).all()

    items = [
        StockReportItem(
            equipment_id=r.equipment_id,
            name=r.name,
            category=r.category,
            default_location=r.default_location,
            on_hand_quantity=int(r.on_hand_quantity),
            active_loaned_quantity=int(r.active_loaned_quantity),
            accounted_quantity=int(r.on_hand_quantity) + int(r.active_loaned_quantity),
            active_loan_count=int(r.active_loan_count),
            overdue_loan_count=int(r.overdue_loan_count),
        )
        for r in rows
    ]

    has_more = (offset + len(items)) < total

    return StockReportPage(
        as_of=as_of,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


def get_missing_report(
    db: Session,
    as_of: datetime | None = None,
    status_filter: Literal["OPEN", "RECONCILED", "ALL"] = "OPEN",
    category: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> MissingReportPage:
    """Operational report for missing equipment incidents."""
    if as_of is None:
        as_of = _current_timestamp()
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    # Global summary counts for missing incidents
    open_count = db.scalar(
        select(func.count(MissingEquipmentIncident.id)).where(
            MissingEquipmentIncident.status == "OPEN"
        )
    ) or 0
    reconciled_count = db.scalar(
        select(func.count(MissingEquipmentIncident.id)).where(
            MissingEquipmentIncident.status == "RECONCILED"
        )
    ) or 0

    reporter_user = User
    # Aliased for reconciler
    from sqlalchemy.orm import aliased
    reconciler_user = aliased(User)

    base_query = (
        select(
            MissingEquipmentIncident.id.label("incident_id"),
            MissingEquipmentIncident.asset_id,
            EquipmentAsset.asset_code,
            Equipment.id.label("equipment_id"),
            Equipment.name.label("equipment_name"),
            Equipment.category,
            MissingEquipmentIncident.status.label("incident_status"),
            MissingEquipmentIncident.reported_at,
            MissingEquipmentIncident.reported_by.label("reported_by_id"),
            reporter_user.full_name.label("reported_by_name"),
            MissingEquipmentIncident.report_reason,
            MissingEquipmentIncident.police_report_number,
            MissingEquipmentIncident.previous_availability_status,
            EquipmentAsset.availability_status.label("current_asset_availability"),
            EquipmentAsset.condition_status.label("current_asset_condition"),
            EquipmentAsset.maintenance_hold.label("current_maintenance_hold"),
            MissingEquipmentIncident.reconciled_at,
            MissingEquipmentIncident.reconciled_by.label("reconciled_by_id"),
            reconciler_user.full_name.label("reconciled_by_name"),
            MissingEquipmentIncident.reconcile_reason,
            MissingEquipmentIncident.recovered_condition,
        )
        .join(EquipmentAsset, EquipmentAsset.id == MissingEquipmentIncident.asset_id)
        .join(Equipment, Equipment.id == EquipmentAsset.equipment_id)
        .join(reporter_user, reporter_user.id == MissingEquipmentIncident.reported_by)
        .outerjoin(reconciler_user, reconciler_user.id == MissingEquipmentIncident.reconciled_by)
    )

    if status_filter != "ALL":
        base_query = base_query.where(MissingEquipmentIncident.status == status_filter)
    if category:
        base_query = base_query.where(Equipment.category == category)

    count_query = select(func.count()).select_from(base_query.subquery())
    total = db.scalar(count_query) or 0

    paginated_query = (
        base_query
        .order_by(MissingEquipmentIncident.reported_at.desc(), MissingEquipmentIncident.id.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(paginated_query).all()

    items = []
    for r in rows:
        # Nonnegative duration calculation (Correction 4)
        if r.incident_status == "OPEN":
            duration = as_of - r.reported_at
        else:
            end_time = r.reconciled_at or as_of
            duration = end_time - r.reported_at

        days = max(0, int(duration.total_seconds() // 86400))

        items.append(
            MissingReportItem(
                incident_id=r.incident_id,
                asset_id=r.asset_id,
                asset_code=r.asset_code,
                equipment_id=r.equipment_id,
                equipment_name=r.equipment_name,
                category=r.category,
                incident_status=r.incident_status,
                reported_at=r.reported_at,
                reported_by_id=r.reported_by_id,
                reported_by_name=r.reported_by_name,
                report_reason=r.report_reason,
                police_report_number=r.police_report_number,
                days_missing=days,
                was_on_loan_when_reported=(r.previous_availability_status == "BORROWED"),
                current_asset_availability=r.current_asset_availability,
                current_asset_condition=r.current_asset_condition,
                current_maintenance_hold=r.current_maintenance_hold,
                reconciled_at=r.reconciled_at,
                reconciled_by_id=r.reconciled_by_id,
                reconciled_by_name=r.reconciled_by_name,
                reconcile_reason=r.reconcile_reason,
                recovered_condition=r.recovered_condition,
            )
        )

    has_more = (offset + len(items)) < total

    return MissingReportPage(
        as_of=as_of,
        open_incident_count=open_count,
        reconciled_incident_count=reconciled_count,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


def get_maintenance_report(
    db: Session,
    as_of: datetime | None = None,
    status_filter: Literal[
        "ACTIVE", "OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION", "COMPLETED", "CANCELLED", "ALL"
    ] = "ACTIVE",
    issue_type: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> MaintenanceReportPage:
    """Operational report for equipment maintenance requests."""
    if as_of is None:
        as_of = _current_timestamp()
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    # Active status counters
    open_count = db.scalar(
        select(func.count(MaintenanceRequest.id)).where(MaintenanceRequest.status == "OPEN")
    ) or 0
    in_progress_count = db.scalar(
        select(func.count(MaintenanceRequest.id)).where(MaintenanceRequest.status == "IN_PROGRESS")
    ) or 0
    awaiting_count = db.scalar(
        select(func.count(MaintenanceRequest.id)).where(MaintenanceRequest.status == "AWAITING_VERIFICATION")
    ) or 0
    total_active = open_count + in_progress_count + awaiting_count

    reporter_user = User
    from sqlalchemy.orm import aliased
    assignee_user = aliased(User)

    base_query = (
        select(
            MaintenanceRequest.id.label("request_id"),
            MaintenanceRequest.asset_id,
            EquipmentAsset.asset_code,
            Equipment.id.label("equipment_id"),
            Equipment.name.label("equipment_name"),
            Equipment.category,
            MaintenanceRequest.issue_type,
            MaintenanceRequest.issue_description,
            MaintenanceRequest.status,
            MaintenanceRequest.reported_at,
            MaintenanceRequest.reported_by.label("reported_by_id"),
            reporter_user.full_name.label("reported_by_name"),
            MaintenanceRequest.assigned_to.label("assigned_to_id"),
            assignee_user.full_name.label("assigned_to_name"),
            EquipmentAsset.availability_status.label("current_asset_availability"),
            EquipmentAsset.condition_status.label("current_asset_condition"),
            EquipmentAsset.maintenance_hold.label("current_maintenance_hold"),
        )
        .join(EquipmentAsset, EquipmentAsset.id == MaintenanceRequest.asset_id)
        .join(Equipment, Equipment.id == EquipmentAsset.equipment_id)
        .join(reporter_user, reporter_user.id == MaintenanceRequest.reported_by)
        .outerjoin(assignee_user, assignee_user.id == MaintenanceRequest.assigned_to)
    )

    if status_filter == "ACTIVE":
        base_query = base_query.where(
            MaintenanceRequest.status.in_(["OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION"])
        )
    elif status_filter != "ALL":
        base_query = base_query.where(MaintenanceRequest.status == status_filter)

    if issue_type:
        base_query = base_query.where(MaintenanceRequest.issue_type == issue_type)

    count_query = select(func.count()).select_from(base_query.subquery())
    total = db.scalar(count_query) or 0

    paginated_query = (
        base_query
        .order_by(MaintenanceRequest.reported_at.desc(), MaintenanceRequest.id.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(paginated_query).all()

    items = []
    for r in rows:
        duration = as_of - r.reported_at
        days = max(0, int(duration.total_seconds() // 86400))

        items.append(
            MaintenanceReportItem(
                request_id=r.request_id,
                asset_id=r.asset_id,
                asset_code=r.asset_code,
                equipment_id=r.equipment_id,
                equipment_name=r.equipment_name,
                category=r.category,
                issue_type=r.issue_type,
                issue_description=r.issue_description,
                status=r.status,
                reported_at=r.reported_at,
                reported_by_id=r.reported_by_id,
                reported_by_name=r.reported_by_name,
                assigned_to_id=r.assigned_to_id,
                assigned_to_name=r.assigned_to_name,
                days_open=days,
                is_under_repair=(r.status == "IN_PROGRESS"),
                current_asset_availability=r.current_asset_availability,
                current_asset_condition=r.current_asset_condition,
                current_maintenance_hold=r.current_maintenance_hold,
            )
        )

    has_more = (offset + len(items)) < total

    return MaintenanceReportPage(
        as_of=as_of,
        open_count=open_count,
        in_progress_count=in_progress_count,
        awaiting_verification_count=awaiting_count,
        total_active_count=total_active,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )

