"""The simulator owns these tables in its separate database."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PartnerBase(DeclarativeBase):
    pass


class PartnerOrder(PartnerBase):
    __tablename__ = "partner_orders"
    client_order_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider_order_id: Mapped[str] = mapped_column(String(100), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(100), index=True)
    result: Mapped[dict] = mapped_column(JSON)
    deliveries: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PartnerReportRecord(PartnerBase):
    __tablename__ = "partner_reports"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)
