"""Immutable domain state and validated business operations."""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    InvalidOperation,
    localcontext,
)
from enum import Enum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CENT = Decimal("0.01")
MAX_EUR_AMOUNT = Decimal("999999999999999999.99")
MAX_QUANTITY = 9_999_999_999_999_999
DEMO_INSTRUMENT = "SYNTH-100"
DEMO_UNIT_PRICE = Decimal("100.00")
Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Quantity = Annotated[int, Field(strict=True, gt=0, le=MAX_QUANTITY)]


class DomainError(ValueError):
    """An operation violates a business rule."""


class InsufficientFundsError(DomainError):
    """The account cannot cover an additional reservation."""


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    def _evolve(self, **changes: Any) -> Self:
        values = {
            name: getattr(self, name) for name in type(self).model_fields
        }
        return type(self).model_validate(values | changes)

    def model_copy(
        self, *, update: dict[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update:
            raise DomainError("Use domain operations to change state")
        return super().model_copy(deep=deep)


class Money(DomainModel):
    amount: Decimal
    currency: Literal["EUR"] = "EUR"

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Money must be finite")
        if value.copy_abs() > MAX_EUR_AMOUNT:
            raise ValueError("EUR amount is outside the supported range")
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            cents = value.quantize(CENT)
        if value != cents:
            raise ValueError("EUR amount must be an exact number of cents")
        return cents.copy_abs() if cents.is_zero() else cents

    @classmethod
    def from_text(cls, amount: str, currency: str = "EUR") -> Self:
        if not isinstance(amount, str):
            raise DomainError("Amount must be a decimal string")
        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise DomainError("Invalid decimal amount") from exc
        return cls(amount=value, currency=currency)

    @classmethod
    def round_calculated(cls, amount: Decimal) -> Self:
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise DomainError("Calculated amount must be a finite Decimal")
        if amount.copy_abs() > MAX_EUR_AMOUNT:
            raise DomainError(
                "Calculated amount is outside the supported range"
            )
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            rounded = amount.quantize(CENT)
        return cls(amount=rounded)

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        with localcontext(Context(prec=40)):
            return Money(amount=self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        with localcontext(Context(prec=40)):
            return Money(amount=self.amount - other.amount)


class BusinessStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class CommunicationStatus(str, Enum):
    NOT_SENT = "NOT_SENT"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"


class Order(DomainModel):
    id: Identifier
    account_id: Identifier
    instrument: Literal["SYNTH-100"] = DEMO_INSTRUMENT
    side: Literal["BUY"] = "BUY"
    quantity: Quantity
    unit_price: Money = Field(
        default_factory=lambda: Money(amount=DEMO_UNIT_PRICE)
    )
    business_status: BusinessStatus = BusinessStatus.PENDING
    communication_status: CommunicationStatus = CommunicationStatus.NOT_SENT

    @field_validator("unit_price")
    @classmethod
    def validate_demo_price(cls, value: Money) -> Money:
        if value.amount != DEMO_UNIT_PRICE:
            raise ValueError("Demo unit price is fixed at 100.00 EUR")
        return value

    @property
    def reservation_amount(self) -> Money:
        with localcontext(Context(prec=40)):
            return Money(amount=self.unit_price.amount * self.quantity)

    @classmethod
    def demo_buy(cls, order_id: str, account_id: str, quantity: int) -> Self:
        return cls(id=order_id, account_id=account_id, quantity=quantity)

    def _transition_business(self, target: BusinessStatus) -> Self:
        allowed = {
            BusinessStatus.PENDING: {
                BusinessStatus.ACCEPTED,
                BusinessStatus.FILLED,
                BusinessStatus.REJECTED,
            },
            BusinessStatus.ACCEPTED: {
                BusinessStatus.FILLED,
                BusinessStatus.REJECTED,
            },
            BusinessStatus.FILLED: set(),
            BusinessStatus.REJECTED: set(),
        }
        if target not in allowed[self.business_status]:
            raise DomainError(
                f"Invalid business transition: {self.business_status.value}"
                f" -> {target.value}"
            )
        return self._evolve(business_status=target)

    def accept(self) -> Self:
        return self._transition_business(BusinessStatus.ACCEPTED)

    def fill(self) -> Self:
        return self._transition_business(BusinessStatus.FILLED)

    def reject(self) -> Self:
        return self._transition_business(BusinessStatus.REJECTED)

    def start_send(self) -> Self:
        if self.communication_status not in {
            CommunicationStatus.NOT_SENT,
            CommunicationStatus.UNKNOWN,
        }:
            raise DomainError(
                "Cannot start a new send in this communication state"
            )
        if self.business_status in {
            BusinessStatus.FILLED,
            BusinessStatus.REJECTED,
        }:
            raise DomainError(
                "Cannot send an order after a definitive outcome"
            )
        return self._evolve(communication_status=CommunicationStatus.IN_FLIGHT)

    def mark_timeout(self) -> Self:
        if self.communication_status != CommunicationStatus.IN_FLIGHT:
            raise DomainError("Timeout requires an in-flight request")
        return self._evolve(communication_status=CommunicationStatus.UNKNOWN)

    def confirm_partner_response(self) -> Self:
        if self.communication_status not in {
            CommunicationStatus.IN_FLIGHT,
            CommunicationStatus.UNKNOWN,
        }:
            raise DomainError("No pending partner result to confirm")
        return self._evolve(communication_status=CommunicationStatus.CONFIRMED)


class Reservation(DomainModel):
    order_id: Identifier
    amount: Money

    @field_validator("amount")
    @classmethod
    def validate_positive_amount(cls, value: Money) -> Money:
        if value.amount <= 0:
            raise ValueError("Reservation amount must be positive")
        return value


class Account(DomainModel):
    id: Identifier
    posted_cash: Money
    reservations: tuple[Reservation, ...] = ()

    @model_validator(mode="after")
    def validate_cash_state(self) -> Self:
        if self.posted_cash.amount < 0:
            raise ValueError("Demo cash cannot be negative")
        ids = [reservation.order_id for reservation in self.reservations]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate order reservation")
        if self.active_reservations.amount > self.posted_cash.amount:
            raise ValueError("Reservations exceed posted cash")
        return self

    @property
    def active_reservations(self) -> Money:
        with localcontext(Context(prec=40)):
            total = sum(
                (item.amount.amount for item in self.reservations),
                Decimal("0.00"),
            )
        return Money(amount=total)

    @property
    def available_cash(self) -> Money:
        return self.posted_cash - self.active_reservations

    def reserve(self, order: Order) -> Self:
        if order.account_id != self.id:
            raise DomainError("Order belongs to another account")
        if order.business_status != BusinessStatus.PENDING:
            raise DomainError("Only a pending order can be reserved")
        if any(item.order_id == order.id for item in self.reservations):
            raise DomainError("Order already has a reservation")
        if order.reservation_amount.amount > self.available_cash.amount:
            raise InsufficientFundsError("Insufficient available cash")
        reservation = Reservation(
            order_id=order.id, amount=order.reservation_amount
        )
        return self._evolve(reservations=(*self.reservations, reservation))

    def _matching_reservation(self, order: Order) -> Reservation:
        if order.account_id == self.id:
            for reservation in self.reservations:
                if (
                    reservation.order_id == order.id
                    and reservation.amount == order.reservation_amount
                ):
                    return reservation
        raise DomainError("No matching active reservation")

    def release_rejected(self, order: Order) -> Self:
        if order.business_status != BusinessStatus.REJECTED:
            raise DomainError(
                "Only a definitive rejection releases reserved cash"
            )
        reservation = self._matching_reservation(order)
        return self._evolve(
            reservations=tuple(
                item for item in self.reservations if item != reservation
            )
        )

    def book_demo_fill(self, order: Order) -> Self:
        """In-memory cash transition; executions.py persists accounting."""
        if order.business_status != BusinessStatus.FILLED:
            raise DomainError("Only a confirmed fill can book demo cash")
        reservation = self._matching_reservation(order)
        return self._evolve(
            posted_cash=self.posted_cash - reservation.amount,
            reservations=tuple(
                item for item in self.reservations if item != reservation
            ),
        )
