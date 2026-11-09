"""Pydantic schemas for read-only inventory reporting and analytics."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AssetAvailabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: int = Field(ge=0)
    borrowed: int = Field(ge=0)
    reserved: int = Field(ge=0)
    maintenance: int = Field(ge=0)
    retired: int = Field(ge=0)
    total_physical_assets: int = Field(ge=0)


class AssetConditionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    working: int = Field(ge=0)
    damaged: int = Field(ge=0)
    under_repair: int = Field(ge=0)
    missing: int = Field(ge=0)
    total_physical_assets: int = Field(ge=0)


class QuantityStockSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_items: int = Field(ge=0)
    on_hand_units: int = Field(ge=0)
    active_loaned_units: int = Field(ge=0)
    accounted_units: int = Field(ge=0)


class OperationalAlertsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_individual_checkouts: int = Field(ge=0)
    overdue_individual_checkouts: int = Field(ge=0)
    active_quantity_loans: int = Field(ge=0)
    overdue_quantity_loans: int = Field(ge=0)
    open_missing_incidents: int = Field(ge=0)
    active_maintenance_requests: int = Field(ge=0)
    assets_on_maintenance_hold: int = Field(ge=0)


class InventoryOverviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    total_catalog_items: int = Field(ge=0)
    individual_catalog_items: int = Field(ge=0)
    quantity_catalog_items: int = Field(ge=0)
    assets: AssetAvailabilitySummary
    conditions: AssetConditionSummary
    quantity_stock: QuantityStockSummary
    alerts: OperationalAlertsSummary


class CategoryRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    catalog_items: int = Field(ge=0)
    individual_catalog_items: int = Field(ge=0)
    quantity_catalog_items: int = Field(ge=0)
    total_physical_assets: int = Field(ge=0)
    available_assets: int = Field(ge=0)
    borrowed_assets: int = Field(ge=0)
    reserved_assets: int = Field(ge=0)
    maintenance_assets: int = Field(ge=0)
    retired_assets: int = Field(ge=0)
    working_assets: int = Field(ge=0)
    damaged_assets: int = Field(ge=0)
    under_repair_assets: int = Field(ge=0)
    missing_assets: int = Field(ge=0)
    quantity_on_hand: int = Field(ge=0)
    quantity_on_loan: int = Field(ge=0)
    accounted_quantity_units: int = Field(ge=0)


class CategoryReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    categories: list[CategoryRollup]


class StockReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipment_id: UUID
    name: str
    category: str
    default_location: str
    on_hand_quantity: int = Field(ge=0)
    active_loaned_quantity: int = Field(ge=0)
    accounted_quantity: int = Field(ge=0)
    active_loan_count: int = Field(ge=0)
    overdue_loan_count: int = Field(ge=0)


class StockReportPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    items: list[StockReportItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool


class MissingReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    asset_id: UUID
    asset_code: str
    equipment_id: UUID
    equipment_name: str
    category: str
    incident_status: Literal["OPEN", "RECONCILED"]
    reported_at: datetime
    reported_by_id: UUID
    reported_by_name: str
    report_reason: str
    police_report_number: str | None
    days_missing: int = Field(ge=0)
    was_on_loan_when_reported: bool
    current_asset_availability: str
    current_asset_condition: str
    current_maintenance_hold: bool
    reconciled_at: datetime | None
    reconciled_by_id: UUID | None
    reconciled_by_name: str | None
    reconcile_reason: str | None
    recovered_condition: str | None


class MissingReportPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    open_incident_count: int = Field(ge=0)
    reconciled_incident_count: int = Field(ge=0)
    items: list[MissingReportItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool


class MaintenanceReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    asset_id: UUID
    asset_code: str
    equipment_id: UUID
    equipment_name: str
    category: str
    issue_type: str
    issue_description: str
    status: str
    reported_at: datetime
    reported_by_id: UUID
    reported_by_name: str
    assigned_to_id: UUID | None
    assigned_to_name: str | None
    days_open: int = Field(ge=0)
    is_under_repair: bool
    current_asset_availability: str
    current_asset_condition: str
    current_maintenance_hold: bool


class MaintenanceReportPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    open_count: int = Field(ge=0)
    in_progress_count: int = Field(ge=0)
    awaiting_verification_count: int = Field(ge=0)
    total_active_count: int = Field(ge=0)
    items: list[MaintenanceReportItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool

