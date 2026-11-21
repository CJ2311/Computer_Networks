from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScanSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scan_id: UUID
    capture_session_id: UUID
    sequence: int = Field(strict=True, ge=1, le=2**63 - 1)
    uid: str = Field(pattern=r"^(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{14}|[0-9A-Fa-f]{20})$", max_length=20)

    @field_validator("uid")
    @classmethod
    def uppercase_uid(cls, value):
        return value.upper()


class ScanQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after_sequence: int = Field(default=0, strict=True, ge=0, le=2**63 - 1)
    limit: int = Field(default=25, strict=True, ge=1, le=50)


class ScanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_id: UUID
    sequence: int
    uid: str
    asset_id: UUID
    received_at: datetime
    expires_at: datetime


class ScanReceipt(ScanEvent):
    duplicate: bool


class ScanPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ScanEvent]
    after_sequence: int
    limit: int
    has_more: bool
