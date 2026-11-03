import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)

    category: Mapped[str] = mapped_column(String(100), nullable=False)

    tracking_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    manufacturer: Mapped[str | None] = mapped_column(String(100))
    model_number: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)

    default_location: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    purchase_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "tracking_type IN ('INDIVIDUAL', 'QUANTITY')",
            name="ck_equipment_tracking_type",
        ),
        CheckConstraint(
            "purchase_price IS NULL OR purchase_price >= 0",
            name="ck_equipment_purchase_price",
        ),
    )

    assets: Mapped[list["EquipmentAsset"]] = relationship(
        back_populates="equipment",
    )


class EquipmentAsset(Base):
    __tablename__ = "equipment_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("equipment.id"),
        nullable=False,
    )

    asset_code: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    nfc_tag_id: Mapped[str | None] = mapped_column(
        String(100),
        unique=True,
    )

    serial_number: Mapped[str | None] = mapped_column(String(100))

    location: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    availability_status: Mapped[str] = mapped_column(
        String(30),
        default="AVAILABLE",
        server_default="AVAILABLE",
        nullable=False,
    )

    condition_status: Mapped[str] = mapped_column(
        String(30),
        default="WORKING",
        server_default="WORKING",
        nullable=False,
    )

    maintenance_hold: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    equipment: Mapped["Equipment"] = relationship(
        back_populates="assets",
    )

    __table_args__ = (
        UniqueConstraint(
            "equipment_id",
            "serial_number",
            name="uq_equipment_asset_serial",
        ),
        CheckConstraint(
            "availability_status IN "
            "('AVAILABLE', 'BORROWED', 'RESERVED', 'MAINTENANCE', 'RETIRED')",
            name="ck_asset_availability",
        ),
        CheckConstraint(
            "condition_status IN "
            "('WORKING', 'DAMAGED', 'UNDER_REPAIR', 'MISSING')",
            name="ck_asset_condition",
        ),
        CheckConstraint(
            "availability_status != 'AVAILABLE' OR "
            "(condition_status = 'WORKING' AND NOT maintenance_hold)",
            name="ck_asset_available_serviceable",
        ),
        CheckConstraint(
            "availability_status != 'MAINTENANCE' OR maintenance_hold",
            name="ck_asset_maintenance_requires_hold",
        ),
    )


class EquipmentStock(Base):
    __tablename__ = "equipment_stock"

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("equipment.id"),
        primary_key=True,
    )

    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    __table_args__ = (
        CheckConstraint(
            "quantity >= 0",
            name="ck_equipment_stock_nonnegative",
        ),
    )
