from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BORROWED = "BORROWED"
    RESERVED = "RESERVED"
    MAINTENANCE = "MAINTENANCE"
    RETIRED = "RETIRED"


class ConditionStatus(str, Enum):
    WORKING = "WORKING"
    DAMAGED = "DAMAGED"
    UNDER_REPAIR = "UNDER_REPAIR"
    MISSING = "MISSING"


class AssetCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    equipment_id: UUID
    asset_code: str = Field(min_length=1, max_length=50)
    location: str = Field(min_length=1, max_length=150)
    nfc_tag_id: str | None = Field(default=None, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)

    @field_validator("nfc_tag_id", "serial_number")
    @classmethod
    def normalize_optional_identifiers(cls, value: str | None) -> str | None:
        return None if value == "" else value


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    equipment_id: UUID
    asset_code: str
    nfc_tag_id: str | None
    serial_number: str | None
    location: str
    availability_status: str
    condition_status: str
    maintenance_hold: bool
