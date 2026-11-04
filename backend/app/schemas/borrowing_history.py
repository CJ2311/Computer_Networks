from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class HistoryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipment_id: UUID | None = None
    asset_id: UUID | None = None
    student_id: UUID | None = None
    status: Literal["BORROWED", "RETURNED"] | None = None
    overdue: bool | None = None
    checked_out_from: AwareDatetime | None = None
    checked_out_before: AwareDatetime | None = None
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=2**63 - 1)

    @field_validator("checked_out_from", "checked_out_before")
    @classmethod
    def normalize_timezone(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_date_range(self):
        if (
            self.checked_out_from is not None
            and self.checked_out_before is not None
            and self.checked_out_from >= self.checked_out_before
        ):
            raise ValueError("checked_out_from must precede checked_out_before")
        return self


class HistoryItem(BaseModel):
    """Explicit projection: no user profiles or free-form staff notes."""

    id: UUID
    asset_id: UUID
    equipment_id: UUID
    asset_code: str | None
    equipment_name: str | None
    identifier_snapshot_status: Literal["CAPTURED", "LEGACY_UNKNOWN"]
    student_id: UUID
    issued_by: UUID
    checked_out_at: datetime
    due_at: datetime
    returned_at: datetime | None
    returned_to: UUID | None
    status: Literal["BORROWED", "RETURNED"]
    is_currently_overdue: bool
    was_returned_late: bool


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    limit: int
    offset: int
    has_more: bool
    as_of: datetime


class HistoryDetail(HistoryItem):
    as_of: datetime
