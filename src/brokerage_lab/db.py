"""SQLAlchemy persistence models and explicit domain mapping."""

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from brokerage_lab.domain import Account, BusinessStatus, CommunicationStatus, Money, Order, Reservation


class Base(DeclarativeBase):
    pass


class AccountRow(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("posted_cash >= 0", name="ck_accounts_cash_nonnegative"),
        CheckConstraint("currency = 'EUR'", name="ck_accounts_currency"),
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    posted_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class InstrumentRow(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            "id = 'SYNTH-100' AND unit_price = 100.00 AND currency = 'EUR'", name="ck_instruments_demo"
        ),
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_orders_id_account"),
        CheckConstraint("quantity > 0 AND quantity <= 9999999999999999", name="ck_orders_quantity"),
        CheckConstraint(
            "side = 'BUY' AND currency = 'EUR' AND unit_price = 100.00", name="ck_orders_demo_terms"
        ),
        CheckConstraint("reservation_amount = quantity * unit_price", name="ck_orders_value"),
        CheckConstraint(
            "business_status IN ('PENDING', 'ACCEPTED', 'FILLED', 'REJECTED')",
            name="ck_orders_business_status",
        ),
        CheckConstraint(
            "communication_status IN ('NOT_SENT', 'IN_FLIGHT', 'UNKNOWN', 'CONFIRMED')",
            name="ck_orders_communication_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    instrument: Mapped[str] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    reservation_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    business_status: Mapped[str] = mapped_column(String(16), nullable=False)
    communication_status: Mapped[str] = mapped_column(String(16), nullable=False)


class ReservationRow(Base):
    __tablename__ = "cash_reservations"
    __table_args__ = (
        ForeignKeyConstraint(["order_id", "account_id"], ["orders.id", "orders.account_id"]),
        CheckConstraint("amount > 0 AND currency = 'EUR'", name="ck_reservations_amount_currency"),
    )
    order_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


def make_engine(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("DATABASE_URL must use postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})


def load_account(session: Session, account_id: str) -> Account | None:
    row = session.get(AccountRow, account_id)
    if row is None:
        return None
    reservations = session.scalars(
        select(ReservationRow).where(ReservationRow.account_id == account_id)
    ).all()
    return Account(
        id=row.id,
        posted_cash=Money(amount=row.posted_cash, currency=row.currency),
        reservations=tuple(
            Reservation(order_id=item.order_id, amount=Money(amount=item.amount, currency=item.currency))
            for item in reservations
        ),
    )


def order_to_row(order: Order) -> OrderRow:
    return OrderRow(
        id=order.id,
        account_id=order.account_id,
        instrument=order.instrument,
        side=order.side,
        quantity=order.quantity,
        unit_price=order.unit_price.amount,
        reservation_amount=order.reservation_amount.amount,
        currency=order.unit_price.currency,
        business_status=order.business_status.value,
        communication_status=order.communication_status.value,
    )


def order_from_row(row: OrderRow) -> Order:
    order = Order(
        id=row.id,
        account_id=row.account_id,
        instrument=row.instrument,
        side=row.side,
        quantity=row.quantity,
        unit_price=Money(amount=row.unit_price, currency=row.currency),
        business_status=BusinessStatus(row.business_status),
        communication_status=CommunicationStatus(row.communication_status),
    )
    if order.reservation_amount.amount != row.reservation_amount:
        raise ValueError("Stored reservation differs from order value")
    return order
