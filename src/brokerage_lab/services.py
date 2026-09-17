"""Application use cases without HTTP or SQLAlchemy session management."""

from brokerage_lab.domain import Account
from brokerage_lab.unit_of_work import UnitOfWork


class AccountNotFoundError(Exception):
    """The requested account does not exist."""


def get_account(account_id: str, uow: UnitOfWork) -> Account:
    with uow:
        account = uow.accounts.get(account_id)
        if account is None:
            raise AccountNotFoundError("Account not found")
        return account
