"""Inventory audit contracts. NFC identifiers carry no authentication authority."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


AuditStatus = Literal["IN_PROGRESS", "COMPLETED", "CANCELLED"]
ScanResult = Literal["MATCHED", "MISPLACED", "UNEXPECTED", "UNKNOWN_TAG", "DUPLICATE"]
FindingType = Literal[
    "NOT_VERIFIED", "EXPECTED_ON_LOAN", "EXPECTED_MAINTENANCE", "MISPLACED",
    "UNEXPECTED", "UNKNOWN_TAG", "STATE_CHANGED", "CUSTODY_REVIEW",
]
Label = Annotated[str, Field(min_length=1, max_length=150)]
Tag = Annotated[str, Field(min_length=1, max_length=100)]
Notes = Annotated[str, Field(max_length=4000)]


class AuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*")
    @classmethod
    def reject_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Text cannot contain NUL characters")
        return value


class InventoryAuditCreate(AuditInput):
    name: Label
    location: Label
    notes: Notes | None = None


class InventoryAuditScanCreate(AuditInput):
    idempotency_key: UUID
    nfc_tag_id: Tag
    observed_location: Label


class InventoryAuditFinalize(AuditInput):
    expected_version: int = Field(ge=1, lt=2**31 - 1, strict=True)
    notes: Notes | None = None


class InventoryAuditCancel(AuditInput):
    expected_version: int = Field(ge=1, lt=2**31 - 1, strict=True)
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


class InventoryAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    location: str
    status: AuditStatus
    created_by: UUID
    started_at: datetime
    snapshot_sealed: bool
    completed_at: datetime | None
    completed_by: UUID | None
    cancelled_at: datetime | None
    cancelled_by: UUID | None
    cancellation_reason: str | None
    reconciled_at: datetime | None
    notes: str | None
    final_notes: str | None
    version: int


class InventoryAuditScanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    audit_id: UUID
    idempotency_key: UUID
    asset_id: UUID | None
    expected_asset_id: UUID | None
    duplicate_of_scan_id: UUID | None
    nfc_tag_id: str
    scanned_by: UUID
    scanned_at: datetime
    observed_location: str
    result: ScanResult


class InventoryAuditExpectedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    audit_id: UUID
    asset_id: UUID
    in_scope: bool
    equipment_name_at_start: str
    asset_code_at_start: str
    nfc_tag_id_at_start: str | None
    expected_location: str
    availability_at_start: str
    condition_at_start: str
    maintenance_hold_at_start: bool
    checkout_id_at_start: UUID | None
    maintenance_id_at_start: UUID | None
    maintenance_version_at_start: int | None


class InventoryAuditFinding(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: UUID | None
    expected_asset_id: UUID | None
    scan_id: UUID | None
    discrepancy_type: FindingType
    description: str
    details: dict


class InventoryAuditReport(BaseModel):
    audit: InventoryAuditResponse
    preview: bool
    reconciliation_complete: bool
    observed_at: datetime | None
    expected_count: int
    verified_count: int
    scan_count: int
    duplicate_count: int
    finding_count: int
    findings: list[InventoryAuditFinding]
    limit: int
    offset: int
    has_more: bool


class InventoryAuditPage(BaseModel):
    items: list[InventoryAuditResponse]
    limit: int
    offset: int
    has_more: bool


class InventoryAuditScanPage(BaseModel):
    items: list[InventoryAuditScanResponse]
    limit: int
    offset: int
    has_more: bool


class InventoryAuditExpectedPage(BaseModel):
    items: list[InventoryAuditExpectedResponse]
    limit: int
    offset: int
    has_more: bool
