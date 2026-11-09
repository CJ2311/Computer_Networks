"""Request and response schemas for missing equipment incidents."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _TextBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("*")
    @classmethod
    def validate_text(cls, value):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("Text cannot contain NUL characters")
        return value


class AssetMissingReportCreate(_TextBase):
    """Payload to report an individual asset as missing.

    Idempotency Contract:
    `idempotency_key` is operation-scoped to missing reports. Retrying with the same
    key and matching payload returns the existing incident record without repeating mutations.
    """

    idempotency_key: UUID
    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    police_report_number: str | None = Field(default=None, max_length=100)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Reason cannot be blank or whitespace-only")
        return trimmed

    @field_validator("police_report_number")
    @classmethod
    def normalize_police_report(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return None if trimmed == "" else trimmed


class AssetMissingReconcileCreate(_TextBase):
    """Payload to reconcile a missing equipment incident upon recovery.

    Idempotency Contract:
    `idempotency_key` is operation-scoped to missing reconciliations. Retrying with
    the same key and matching payload returns the reconciled incident record without
    repeating mutations.
    """

    idempotency_key: UUID
    recovered_condition: Literal["WORKING", "DAMAGED"]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Reason cannot be blank or whitespace-only")
        return trimmed


class MissingIncidentResponse(BaseModel):
    """Lifecycle view of a missing equipment incident.

    Field Semantics:
    - Incident fields (id, status, version, reported_*, reconciled_*, previous_*, resolved_*)
      represent the immutable audit history and lifecycle record of the specific incident.
    - Live asset fields (asset_availability_status, asset_condition_status, asset_maintenance_hold)
      represent the current live inventory state of the equipment asset at the time of the query.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    operation_key: UUID
    reconcile_key: UUID | None = None
    status: str
    version: int
    reported_by: UUID
    reported_at: datetime
    report_reason: str
    police_report_number: str | None = None
    reconciled_by: UUID | None = None
    reconciled_at: datetime | None = None
    reconcile_reason: str | None = None
    recovered_condition: str | None = None
    previous_availability_status: str
    previous_condition_status: str
    resolved_availability_status: str | None = None
    resolved_condition_status: str | None = None

    asset_availability_status: str
    asset_condition_status: str
    asset_maintenance_hold: bool


class MissingEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    incident_id: UUID
    asset_id: UUID
    sequence: int
    event_type: str
    actor_id: UUID
    occurred_at: datetime
    reason: str
    police_report_number: str | None = None
    from_availability_status: str
    to_availability_status: str
    from_condition_status: str
    to_condition_status: str
    from_maintenance_hold: bool
    to_maintenance_hold: bool

