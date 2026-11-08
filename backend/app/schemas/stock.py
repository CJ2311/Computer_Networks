"""Optimistic, bounded quantity adjustments; operational asset loans are separate."""
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StockAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_quantity: int = Field(strict=True, ge=0, le=2**31-1)
    idempotency_key: UUID | None = None
    reason: str = Field(default="Manual adjustment", min_length=1, max_length=4000)
    change: int = Field(strict=True, ge=-(2**31-1), le=2**31-1)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Reason must be nonblank and contain no NUL")
        return value

    @field_validator("change")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("Change must be nonzero")
        return value


class StockResponse(BaseModel):
    equipment_id: UUID
    quantity: int = Field(ge=0, le=2**31-1)
    operation_key: UUID | None = None
