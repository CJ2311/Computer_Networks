from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from backend.app.schemas.borrowers import BorrowerResolve


class CardUID(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_uid: str = Field(pattern=r"^(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{14}|[0-9A-Fa-f]{20})$", max_length=20)

    @field_validator("card_uid", mode="before")
    @classmethod
    def normalize_uid(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class RegistrationStart(BorrowerResolve, CardUID):
    request_id: UUID  # stable client idempotency key; use a new key for a new attempt
    scanner_id: UUID


class ScannerEvidence(CardUID):
    model_config = ConfigDict(extra="forbid")
    payload: str = Field(min_length=1, max_length=128, pattern=r"^CNINV1:[0-9a-f-]{36}:[A-Za-z0-9_-]{43}$")
    write_profile: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class RegistrationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registration_id: UUID
    student_id: UUID
    card_uid: str
    status: Literal["PENDING_WRITE", "ACTIVE", "FAILED"]
    created_at: datetime
    verified_at: datetime | None
    expires_at: datetime


class RegistrationPrepared(RegistrationStatus):
    payload: str | None = None
    write_profile: str


class ScannerAcknowledgement(BaseModel):
    registration_id: UUID
    status: Literal["ACTIVE"]
