from datetime import datetime
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