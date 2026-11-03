from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CheckoutCreate(BaseModel):
    asset_id: UUID
    student_id: UUID
    due_at: datetime
    notes: str | None = Field(default=None, max_length=1000)


class CheckoutResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    student_id: UUID
    issued_by: UUID
    checked_out_at: datetime
    due_at: datetime
    returned_at: datetime | None
    returned_to: UUID | None
    status: str
    notes: str | None


ReturnCondition = Literal["WORKING", "DAMAGED", "UNDER_REPAIR"]


class ReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_status: ReturnCondition
    recovery_of_request_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ReturnResponse(CheckoutResponse):
    returned_at: datetime
    returned_to: UUID
    status: Literal["RETURNED"]
    condition_status: ReturnCondition
    availability_status: Literal["AVAILABLE", "RESERVED", "MAINTENANCE"]
    maintenance_hold: bool
    maintenance_request_id: UUID | None
    is_overdue: bool
