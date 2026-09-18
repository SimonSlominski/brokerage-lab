"""Small persistence contracts and SQLAlchemy adapters; repositories never commit."""

from typing import Protocol

from sqlalchemy.orm import Session

from brokerage_lab.db import (
    AccountRow,
    OrderRow,
    ReservationRow,
    load_account,
    order_from_row,
    order_to_row,
)
from brokerage_lab.domain import Account, Order, Reservation


class AccountRepository(Protocol):
    def get(self, account_id: str) -> Account | None: ...
    def add(self, account: Account) -> None: ...
    def get_for_update(self, account_id: str) -> Account | None: ...
    def add_reservation(
        self, account_id: str, reservation: Reservation
    ) -> None: ...


class OrderRepository(Protocol):
    def get(self, order_id: str) -> Order | None: ...
    def add(self, order: Order) -> None: ...


class SqlAlchemyAccountRepository:
    def __init__(self, session: Session):
        self._session = session

    def get(self, account_id: str) -> Account | None:
        return load_account(self._session, account_id)

    def get_for_update(self, account_id: str) -> Account | None:
        return load_account(self._session, account_id, for_update=True)

    def add_reservation(
        self, account_id: str, reservation: Reservation
    ) -> None:
        """Persist a validated reservation while the caller holds its account lock."""
        self._session.add(
            ReservationRow(
                order_id=reservation.order_id,
                account_id=account_id,
                amount=reservation.amount.amount,
                currency=reservation.amount.currency,
            )
        )
        self._session.flush()

    def add(self, account: Account) -> None:
        """Insert an initial account, not an update or reservation operation."""
        if account.reservations:
            raise ValueError(
                "New accounts must not contain existing reservations"
            )
        self._session.add(
            AccountRow(
                id=account.id,
                posted_cash=account.posted_cash.amount,
                currency=account.posted_cash.currency,
            )
        )
        self._session.flush()


class SqlAlchemyOrderRepository:
    def __init__(self, session: Session):
        self._session = session

    def get(self, order_id: str) -> Order | None:
        row = self._session.get(OrderRow, order_id)
        return None if row is None else order_from_row(row)

    def add(self, order: Order) -> None:
        """Insert an order inside the caller's transaction; does not reserve cash."""
        self._session.add(order_to_row(order))
        self._session.flush()
