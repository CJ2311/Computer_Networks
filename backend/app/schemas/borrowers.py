"""Borrower records have no application login capability."""
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BorrowerResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_name: str = Field(min_length=1, max_length=150)
    registration_number: str = Field(min_length=1, max_length=50)

    @field_validator("student_name", "registration_number", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("student_name")
    @classmethod
    def validate_name(cls, value):
        if not any(character.isalpha() for character in value) or any(
            ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
        ):
            raise ValueError("Student name must contain letters and no control characters")
        return value

    @field_validator("registration_number")
    @classmethod
    def normalize_registration(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,49}", value):
            raise ValueError("Registration number must use ASCII letters, digits, dots, underscores, slashes or hyphens")
        return value.upper()


class BorrowerResolveResponse(BaseModel):
    student_id: UUID
    student_name: str
    registration_number: str
    created: bool
