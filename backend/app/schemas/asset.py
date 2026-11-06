from enum import Enum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


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


AssetCode = Annotated[str, Field(min_length=1, max_length=50)]
Location = Annotated[str, Field(min_length=1, max_length=150)]


class _AssetMetadata(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    nfc_tag_id: str | None = Field(default=None, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)

    @field_validator("nfc_tag_id", "serial_number")
    @classmethod
    def normalize_optional_identifiers(cls, value: str | None) -> str | None:
        return None if value == "" else value


class AssetCreate(_AssetMetadata):
    asset_code: AssetCode
    location: Location
    equipment_id: UUID


class AssetUpdate(_AssetMetadata):
    asset_code: AssetCode | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    location: Location | SkipJsonSchema[None] = Field(default_factory=lambda: None)

    @field_validator("asset_code", "location")
    @classmethod
    def reject_explicit_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @field_validator("*")
    @classmethod
    def reject_nul_in_text(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Text cannot contain NUL characters")
        return value

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one metadata field is required")
        return self


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
