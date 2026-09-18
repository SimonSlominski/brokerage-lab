"""Application use cases without HTTP or SQLAlchemy session management."""

from uuid import uuid4

from brokerage_lab.domain import Account, Order
from brokerage_lab.unit_of_work import UnitOfWork


class AccountNotFoundError(Exception):
    """The requested account does not exist."""


def get_account(account_id: str, uow: UnitOfWork) -> Account:
    with uow:
        account = uow.accounts.get(account_id)
        if account is None:
            raise AccountNotFoundError("Account not found")
        return account


class OrderNotFoundError(Exception):
    """No order exists within the requested account."""


def submit_order(account_id: str, quantity: int, uow: UnitOfWork) -> Order:
    """Reserve cash and insert one local order in the same transaction."""
    order = Order.demo_buy(str(uuid4()), account_id, quantity)
    with uow:
        account = uow.accounts.get_for_update(account_id)
        if account is None:
            raise AccountNotFoundError("Account not found")
        reserved_account = account.reserve(order)
        uow.orders.add(order)
        uow.accounts.add_reservation(
            account.id, reserved_account.reservations[-1]
        )
        uow.commit()
    return order


def get_order(account_id: str, order_id: str, uow: UnitOfWork) -> Order:
    with uow:
        order = uow.orders.get(order_id)
        if order is None or order.account_id != account_id:
            raise OrderNotFoundError("Order not found")
        return order
