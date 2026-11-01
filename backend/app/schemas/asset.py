from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BORROWED = "BORROWED"
    RESERVED = "RESERVED"
    RETIRED = "RETIRED"


class ConditionStatus(str, Enum):
    WORKING = "WORKING"
    DAMAGED = "DAMAGED"
    UNDER_REPAIR = "UNDER_REPAIR"
    MISSING = "MISSING"


class AssetCreate(BaseModel):
    equipment_id: UUID
    asset_code: str = Field(min_length=1, max_length=50)
    location: str = Field(min_length=1, max_length=150)
    nfc_tag_id: str | None = None
    serial_number: str | None = None


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