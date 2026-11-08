from datetime import datetime
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    @field_validator("*", check_fields=False)
    @classmethod
    def valid_text(cls,value):
        if isinstance(value,str) and (not value or "\x00" in value):
            raise ValueError("Text must be nonblank and contain no NUL")
        return value


class QuantityLoanCreate(OperationInput):
    idempotency_key: UUID
    equipment_id: UUID
    student_id: UUID
    quantity: int = Field(strict=True,ge=1,le=2**31-1)
    due_at: datetime
    destination: str | None = Field(None,min_length=1,max_length=150)


class QuantityReturnCreate(OperationInput):
    idempotency_key: UUID
    condition_status: Literal["WORKING"]


class QuantityLoanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    equipment_id: UUID
    student_id: UUID
    issued_by: UUID
    quantity: int
    equipment_name: str
    destination: str
    due_at: datetime
    checked_out_at: datetime
    returned_at: datetime | None
    returned_to: UUID | None
    return_key: UUID | None
    is_currently_overdue: bool = False
    was_returned_late: bool = False


class LoanPolicyInput(OperationInput):
    allow_offsite: bool = Field(strict=True)


class LedgerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    operation_key: UUID
    revision: int
    equipment_id: UUID
    actor_id: UUID | None
    loan_id: UUID | None
    kind: str
    reason: str
    previous_quantity: int
    change: int
    quantity: int
    occurred_at: datetime


class QuantityLoanPage(BaseModel):
    items: list[QuantityLoanResponse]
    limit: int
    offset: int
    has_more: bool
