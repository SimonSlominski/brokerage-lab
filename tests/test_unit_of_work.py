"""Verify actual commit and rollback across repositories on PostgreSQL."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import Session

from brokerage_lab.db import AccountRow, OrderRow
from brokerage_lab.demo import seed_demo
from brokerage_lab.domain import Account, Money, Order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.postgres


def test_explicit_commit_is_visible_in_new_session(postgres_engine):
    seed_demo(postgres_engine)
    account = Account(id="uow-account", posted_cash=Money.from_text("1000.00"))
    order = Order.demo_buy("uow-order", account.id, 8)
    with SqlAlchemyUnitOfWork(postgres_engine) as uow:
        uow.accounts.add(account)
        uow.orders.add(order)
        assert uow.accounts.get(account.id) == account
        assert uow.orders.get(order.id) == order
        with Session(postgres_engine) as independent:
            assert independent.get(AccountRow, account.id) is None
            assert independent.get(OrderRow, order.id) is None
        uow.commit()
    with Session(postgres_engine) as independent:
        assert independent.get(AccountRow, account.id).posted_cash == Decimal(
            "1000.00"
        )
        assert independent.get(OrderRow, order.id).quantity == 8


@pytest.mark.parametrize("failure", [False, True])
def test_without_commit_both_flushed_writes_are_rolled_back(
    postgres_engine, failure
):
    seed_demo(postgres_engine)
    account = Account(
        id="rollback-account", posted_cash=Money.from_text("1000.00")
    )
    try:
        with SqlAlchemyUnitOfWork(postgres_engine) as uow:
            uow.accounts.add(account)
            uow.orders.add(Order.demo_buy("rollback-order", account.id, 8))
            assert uow.orders.get("rollback-order") is not None
            if failure:
                raise RuntimeError("Injected failure before commit")
    except RuntimeError as error:
        assert failure
        assert str(error) == "Injected failure before commit"
    with Session(postgres_engine) as independent:
        assert independent.get(AccountRow, account.id) is None
        assert independent.get(OrderRow, "rollback-order") is None


def test_database_failure_does_not_leave_partial_account(postgres_engine):
    seed_demo(postgres_engine)
    account = Account(
        id="failed-account", posted_cash=Money.from_text("1000.00")
    )
    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(postgres_engine) as uow:
            uow.accounts.add(account)
            order = Order.demo_buy("duplicate-order", account.id, 8)
            uow.orders.add(order)
            uow.orders.add(order)
            uow.commit()
    with Session(postgres_engine) as independent:
        assert independent.get(AccountRow, account.id) is None
        assert independent.get(OrderRow, "duplicate-order") is None


def test_explicit_rollback_and_closed_context_cannot_write(postgres_engine):
    account = Account(id="discarded", posted_cash=Money.from_text("1000.00"))
    uow = SqlAlchemyUnitOfWork(postgres_engine)
    with uow:
        uow.accounts.add(account)
        uow.rollback()
        with pytest.raises(InvalidRequestError):
            uow.accounts.get(account.id)
    with pytest.raises(RuntimeError, match="inside its context"):
        uow.commit()
    with pytest.raises(RuntimeError, match="new Unit of Work"):
        with uow:
            pass
    with Session(postgres_engine) as independent:
        assert independent.get(AccountRow, account.id) is None


def test_exception_after_commit_does_not_undo_durable_changes(postgres_engine):
    account = Account(id="committed", posted_cash=Money.from_text("1000.00"))
    with pytest.raises(RuntimeError, match="After commit"):
        with SqlAlchemyUnitOfWork(postgres_engine) as uow:
            uow.accounts.add(account)
            uow.commit()
            raise RuntimeError("After commit")
    with Session(postgres_engine) as independent:
        assert independent.get(AccountRow, account.id).posted_cash == Decimal(
            "1000.00"
        )
