from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


class TrackingType(str, Enum):
    INDIVIDUAL = "INDIVIDUAL"
    QUANTITY = "QUANTITY"


Name = Annotated[str, Field(min_length=1, max_length=150)]
Category = Annotated[str, Field(min_length=1, max_length=100)]
DefaultLocation = Annotated[str, Field(min_length=1, max_length=150)]


class _EquipmentMetadata(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    manufacturer: str | None = Field(default=None, max_length=100)
    model_number: str | None = Field(default=None, max_length=100)
    description: str | None = None

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


class EquipmentCreate(_EquipmentMetadata):
    name: Name
    category: Category
    default_location: DefaultLocation
    tracking_type: TrackingType


class EquipmentUpdate(_EquipmentMetadata):
    name: Name | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    category: Category | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    default_location: DefaultLocation | SkipJsonSchema[None] = Field(default_factory=lambda: None)

    @field_validator("name", "category", "default_location")
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