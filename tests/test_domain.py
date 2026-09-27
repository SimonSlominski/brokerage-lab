"""Domain behavior and regression tests for immutable Pydantic state."""

from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from brokerage_lab.domain import (
    Account,
    BusinessStatus,
    CommunicationStatus,
    DomainError,
    Money,
    Order,
    Reservation,
)


def account() -> Account:
    return Account(id="account-1", posted_cash=Money.from_text("1000.00"))


def order(quantity: int = 8) -> Order:
    return Order.demo_buy("order-1", "account-1", quantity)


@pytest.mark.parametrize(
    "amount", ["1.005", "-0.001", "NaN", "Infinity", "1E+100"]
)
def test_rejects_invalid_money(amount):
    with pytest.raises(ValidationError):
        Money.from_text(amount)


def test_money_requires_decimal_eur_and_normalizes_exact_cents():
    assert Money.from_text("1.000").amount.as_tuple().exponent == -2
    with pytest.raises(ValidationError):
        Money(amount=0.1)
    with pytest.raises(ValidationError):
        Money.from_text("1.00", "USD")
    with pytest.raises(DomainError):
        Money.from_text("invalid")


def test_rounding_and_arithmetic_do_not_depend_on_caller_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert Money.round_calculated(Decimal("1.005")).amount == Decimal(
            "1.00"
        )
        assert Money.round_calculated(Decimal("1.015")).amount == Decimal(
            "1.02"
        )
        assert (
            Money.from_text("1000.01") + Money.from_text("0.02")
        ).amount == Decimal("1000.03")
        assert order().reservation_amount.amount == Decimal("800.00")


def test_signed_money_is_valid_but_negative_demo_balance_is_not():
    assert Money.from_text("-1.00").amount == Decimal("-1.00")
    with pytest.raises(ValidationError):
        Account(id="account-1", posted_cash=Money.from_text("-1.00"))


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, "8", 10**20])
def test_rejects_invalid_quantities(quantity):
    with pytest.raises(ValidationError):
        order(quantity)


def test_rejects_unapproved_instrument_and_price():
    with pytest.raises(ValidationError):
        Order(
            id="order-1",
            account_id="account-1",
            quantity=1,
            instrument="OTHER",
        )
    with pytest.raises(ValidationError):
        Order(
            id="order-1",
            account_id="account-1",
            quantity=1,
            unit_price=Money.from_text("99.99"),
        )


def test_reservation_timeout_and_insufficient_funds():
    original = account()
    reserved = original.reserve(order())
    assert original.active_reservations.amount == Decimal("0.00")
    assert reserved.posted_cash.amount == Decimal("1000.00")
    assert reserved.active_reservations.amount == Decimal("800.00")
    assert reserved.available_cash.amount == Decimal("200.00")
    unknown = order().start_send().mark_timeout()
    assert unknown.business_status == BusinessStatus.PENDING
    assert unknown.communication_status == CommunicationStatus.UNKNOWN
    with pytest.raises(DomainError, match="definitive rejection"):
        reserved.release_rejected(unknown)
    with pytest.raises(DomainError, match="Insufficient"):
        reserved.reserve(Order.demo_buy("order-2", "account-1", 3))


def test_rejection_releases_without_spending():
    reserved = account().reserve(order())
    rejected = order().reject()
    released = reserved.release_rejected(rejected)
    assert released.posted_cash.amount == Decimal("1000.00")
    assert released.available_cash.amount == Decimal("1000.00")
    assert released.active_reservations.amount == Decimal("0.00")


def test_confirmed_fill_consumes_demo_reservation_once():
    filled = order().accept().fill()
    booked = account().reserve(order()).book_demo_fill(filled)
    assert booked.posted_cash.amount == Decimal("200.00")
    assert booked.available_cash.amount == Decimal("200.00")
    assert booked.active_reservations.amount == Decimal("0.00")
    with pytest.raises(DomainError, match="No matching"):
        booked.book_demo_fill(filled)


@pytest.mark.parametrize(
    "terminal", [BusinessStatus.FILLED, BusinessStatus.REJECTED]
)
@pytest.mark.parametrize("action", ["accept", "fill", "reject", "start_send"])
def test_terminal_states_cannot_transition(terminal, action):
    final = (
        order().fill()
        if terminal == BusinessStatus.FILLED
        else order().reject()
    )
    with pytest.raises(DomainError):
        getattr(final, action)()


def test_public_mutations_and_unvalidated_copy_updates_are_blocked():
    original = order()
    for field, value in [
        ("quantity", 0),
        ("business_status", BusinessStatus.REJECTED),
        ("reservation_amount", Money.from_text("1.00")),
    ]:
        with pytest.raises(ValidationError):
            setattr(original, field, value)
    with pytest.raises(DomainError):
        original.fill().model_copy(
            update={"business_status": BusinessStatus.PENDING}
        )
    reserved = account().reserve(original)
    with pytest.raises(ValidationError):
        reserved.reservations[0].amount = Money.from_text("1.00")
    with pytest.raises(ValidationError):
        reserved.reservations += (
            Reservation(order_id="extra", amount=Money.from_text("1.00")),
        )
    assert reserved.available_cash.amount == Decimal("200.00")


def test_construction_checks_nested_reservation_invariants():
    reservation = Reservation(
        order_id="order-1", amount=Money.from_text("800.00")
    )
    with pytest.raises(ValidationError):
        Account(
            id="account-1",
            posted_cash=Money.from_text("1000.00"),
            reservations=(reservation, reservation),
        )
    with pytest.raises(ValidationError):
        Account(
            id="account-1",
            posted_cash=Money.from_text("100.00"),
            reservations=(reservation,),
        )


def test_mismatched_or_duplicate_reservations_fail():
    with pytest.raises(DomainError):
        Account(id="other", posted_cash=Money.from_text("1000.00")).reserve(
            order()
        )
    reserved = account().reserve(order())
    with pytest.raises(DomainError):
        reserved.reserve(order())
    with pytest.raises(DomainError):
        reserved.release_rejected(order(1).reject())


def test_communication_and_business_status_remain_separate():
    unknown = order().start_send().mark_timeout()
    accepted = unknown.accept()
    assert accepted.communication_status == CommunicationStatus.UNKNOWN
    confirmed = accepted.confirm_partner_response()
    assert confirmed.communication_status == CommunicationStatus.CONFIRMED
    assert confirmed.business_status == BusinessStatus.ACCEPTED
    retried = unknown.start_send()
    assert retried.id == unknown.id
    with pytest.raises(DomainError):
        order().mark_timeout()
