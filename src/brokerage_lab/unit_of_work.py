"""One explicit transaction shared by account and order repositories."""

from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from brokerage_lab.repositories import (
    AccountRepository,
    OrderRepository,
    SqlAlchemyAccountRepository,
    SqlAlchemyOrderRepository,
)


class UnitOfWork(Protocol):
    accounts: AccountRepository
    orders: OrderRepository

    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork:
    """Single-use context: explicit commit, otherwise rollback, always close."""

    def __init__(self, engine: Engine):
        self._engine = engine
        self._session: Session | None = None
        self._used = False

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("Create a new Unit of Work for each operation")
        self._used = True
        self._session = Session(self._engine, autobegin=False, close_resets_only=False)
        self._session.begin()
        self.accounts = SqlAlchemyAccountRepository(self._session)
        self.orders = SqlAlchemyOrderRepository(self._session)
        return self

    def commit(self) -> None:
        self._active_session().commit()

    def rollback(self) -> None:
        self._active_session().rollback()

    def _active_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("Unit of Work must be used inside its context")
        return self._session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._active_session()
        try:
            session.rollback()
        finally:
            session.close()
            self._session = None
