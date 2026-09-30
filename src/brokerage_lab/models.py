"""Durable workflow and accounting tables, separate from API schemas."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    client_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    response: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer, default=201)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        CheckConstraint(
            "status IN ('PENDING','CLAIMED','DONE','ERROR')",
            name="ck_outbox_status",
        ),
        Index(
            "ix_outbox_pending_claim",
            "run_id",
            "available_at",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_outbox_expired_claim",
            "run_id",
            "lease_until",
            "created_at",
            postgresql_where=text("status = 'CLAIMED'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(250))
    mode: Mapped[str] = mapped_column(String(32), default="normal")
    run_id: Mapped[str | None] = mapped_column(String(100), index=True)


class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JournalTransaction(Base):
    __tablename__ = "journal_transactions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(200), unique=True)
    correction_of: Mapped[str | None] = mapped_column(
        ForeignKey("journal_transactions.id"), unique=True
    )
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint("currency = 'EUR'", name="ck_journal_currency"),
        CheckConstraint("amount != 0", name="ck_journal_nonzero"),
        UniqueConstraint("transaction_id", "bucket", name="uq_journal_bucket"),
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("journal_transactions.id"), index=True
    )
    bucket: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class ProcessedExecution(Base):
    __tablename__ = "processed_executions"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_execution_full_order"),
    )
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("journal_transactions.id"), unique=True
    )
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstrumentMovement(Base):
    __tablename__ = "instrument_movements"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("journal_transactions.id"), unique=True
    )
    instrument: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    report_id: Mapped[str] = mapped_column(String(100))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconciliationBreak(Base):
    __tablename__ = "reconciliation_breaks"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_runs.id"))
    category: Mapped[str] = mapped_column(String(30))
    order_id: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")


class BreakHistory(Base):
    __tablename__ = "break_history"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    break_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_breaks.id")
    )
    status: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScenarioRun(Base):
    __tablename__ = "scenario_runs"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(40))
    seed: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
