import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.base import Base


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    FACULTY = "FACULTY"
    LAB_ASSISTANT = "LAB_ASSISTANT"
    STUDENT = "STUDENT"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    college_id: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    full_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=UserRole.STUDENT.value,
        server_default=UserRole.STUDENT.value,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('ADMIN', 'FACULTY', 'LAB_ASSISTANT', 'STUDENT')",
            name="ck_users_role",
        ),
    )
