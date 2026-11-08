"""Application extensions. No changes to the seven-revision NFC baseline tables."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.base import Base


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_identity_subject"),)


class EquipmentLoanPolicy(Base):
    __tablename__ = "equipment_loan_policies"
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("equipment.id"), primary_key=True)
    allow_offsite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LoanDestination(Base):
    __tablename__ = "loan_destinations"
    id: Mapped[UUID] = mapped_column(ForeignKey("equipment_checkouts.id"), primary_key=True)
    destination: Mapped[str] = mapped_column(String(150), nullable=False)


class QuantityLoan(Base):
    __tablename__ = "quantity_loans"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("equipment.id"), nullable=False)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    issued_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    equipment_name: Mapped[str] = mapped_column(String(150), nullable=False)
    destination: Mapped[str] = mapped_column(String(150), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_out_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_to: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    return_key: Mapped[UUID | None] = mapped_column(unique=True)
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_quantity_loan_positive"),
        CheckConstraint("due_at > checked_out_at", name="ck_quantity_loan_due"),
        CheckConstraint("(returned_at IS NULL AND returned_to IS NULL AND return_key IS NULL) OR (returned_at IS NOT NULL AND returned_at >= checked_out_at AND returned_to IS NOT NULL AND return_key IS NOT NULL)", name="ck_quantity_loan_return"),
    )


class StockLedger(Base):
    __tablename__ = "stock_ledger"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_key: Mapped[UUID] = mapped_column(unique=True, nullable=False)
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("equipment.id"), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    loan_id: Mapped[UUID | None] = mapped_column(ForeignKey("quantity_loans.id"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    change: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("equipment_id","revision",name="uq_stock_ledger_revision"),
        CheckConstraint("revision > 0",name="ck_stock_ledger_revision"),
        CheckConstraint("quantity >= 0 AND previous_quantity >= 0 AND quantity::bigint = previous_quantity::bigint + change::bigint", name="ck_stock_ledger_balance").ddl_if(dialect="postgresql"),
        CheckConstraint("kind IN ('OPENING','ADJUSTMENT','ISSUE','RETURN')", name="ck_stock_ledger_kind"),
    )


class OverdueReminder(Base):
    __tablename__ = "overdue_reminders"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    checkout_id: Mapped[UUID | None] = mapped_column(ForeignKey("equipment_checkouts.id"), unique=True)
    quantity_loan_id: Mapped[UUID | None] = mapped_column(ForeignKey("quantity_loans.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("(checkout_id IS NULL) <> (quantity_loan_id IS NULL)", name="ck_reminder_one_loan"),
        CheckConstraint("state IN ('PENDING','SENT','UNCERTAIN','CANCELLED')", name="ck_reminder_state"),
    )
