"""Request and response schemas for equipment maintenance."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from typing import Literal

class MaintenanceResponse(BaseModel):
    """A maintenance request and its current equipment state."""

    model_config = ConfigDict(from_attributes=True)

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


class MaintenanceEventResponse(BaseModel):
    """An individual maintenance audit event."""

    model_config = ConfigDict(from_attributes=True)

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


class MaintenanceEventPageResponse(BaseModel):
    """Paginated maintenance event history."""

    model_config = ConfigDict(from_attributes=True)

    items: tuple[MaintenanceEventResponse, ...]
    limit: int
    offset: int
    has_more: bool

class _MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise ValueError("Text must be nonblank and contain no NUL characters")
        return value


class MaintenanceIssueCreate(_MaintenanceInput):
    asset_id: UUID
    issue_type: Literal["DAMAGE", "MALFUNCTION", "INSPECTION"]
    issue_description: str = Field(min_length=1, max_length=4000)


class MaintenanceRecoveryCreate(_MaintenanceInput):
    issue_description: str = Field(min_length=1, max_length=4000)


class MaintenanceAssignmentCreate(_MaintenanceInput):
    assignee_id: UUID
    expected_version: int = Field(strict=True, ge=1, lt=2**31 - 1)


class MaintenanceVersionRequest(_MaintenanceInput):
    expected_version: int = Field(strict=True, ge=1, lt=2**31 - 1)


class MaintenanceRepairSubmission(_MaintenanceInput):
    expected_version: int = Field(strict=True, ge=1, lt=2**31 - 1)
    repair_notes: str = Field(min_length=1, max_length=5000)


class MaintenanceVerificationCreate(_MaintenanceInput):
    expected_version: int = Field(strict=True, ge=1, lt=2**31 - 1)
    outcome: Literal["PASS", "FAIL"]
    notes: str = Field(min_length=1, max_length=5000)


class MaintenanceCancellationCreate(_MaintenanceInput):
    expected_version: int = Field(strict=True, ge=1, lt=2**31 - 1)
    reason: str = Field(min_length=1, max_length=4000)


class MaintenanceNoteCreate(MaintenanceVersionRequest):
    note: str = Field(min_length=1, max_length=5000)


class MaintenancePageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    items: tuple[MaintenanceResponse, ...]
    limit: int
    offset: int
    has_more: bool
