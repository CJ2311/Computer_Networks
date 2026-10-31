from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TrackingType(str, Enum):
    INDIVIDUAL = "INDIVIDUAL"
    QUANTITY = "QUANTITY"


class EquipmentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    category: str = Field(min_length=1, max_length=100)
    tracking_type: TrackingType

    manufacturer: str | None = Field(default=None, max_length=100)
    model_number: str | None = Field(default=None, max_length=100)
    description: str | None = None

    default_location: str = Field(min_length=1, max_length=150)

    purchase_price: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    @field_validator("manufacturer", "model_number")
    @classmethod
    def empty_optional_strings_to_none(cls, value: str | None):
        if value == "":
            return None
        return value


class EquipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    category: str
    tracking_type: TrackingType

    manufacturer: str | None
    model_number: str | None
    description: str | None

    default_location: str
    purchase_price: Decimal | None

    created_at: datetime