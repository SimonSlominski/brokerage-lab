"""Public response schemas; domain objects and database rows stay separate."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from brokerage_lab.domain import Account, Identifier


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(ResponseModel):
    status: Literal["ok"] = "ok"


class CashResponse(ResponseModel):
    account_id: Identifier
    currency: Literal["EUR"]
    posted_cash: Decimal
    active_reservations: Decimal
    available_cash: Decimal

    @classmethod
    def from_account(cls, account: Account) -> "CashResponse":
        return cls(
            account_id=account.id,
            currency=account.posted_cash.currency,
            posted_cash=account.posted_cash.amount,
            active_reservations=account.active_reservations.amount,
            available_cash=account.available_cash.amount,
        )
