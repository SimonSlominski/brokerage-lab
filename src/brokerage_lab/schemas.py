"""HTTP input and response schemas, separate from persistence models."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

from brokerage_lab.domain import (
    Account,
    BusinessStatus,
    CommunicationStatus,
    Identifier,
    Order,
    Quantity,
)

from .api_formats import CashAmount, SharePrice, WireQuantity


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


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quantity: Quantity
    instrument: Literal["SYNTH-100"] = "SYNTH-100"
    side: Literal["BUY"] = "BUY"
    currency: Literal["EUR"] = "EUR"


class OrderResponse(ResponseModel):
    order_id: Identifier
    account_id: Identifier
    instrument: Literal["SYNTH-100"]
    side: Literal["BUY"]
    quantity: Quantity
    currency: Literal["EUR"]
    unit_price: Decimal
    reservation_amount: Decimal
    business_status: BusinessStatus
    communication_status: CommunicationStatus

    @classmethod
    def from_order(cls, order: Order) -> "OrderResponse":
        return cls(
            order_id=order.id,
            account_id=order.account_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            currency=order.unit_price.currency,
            unit_price=order.unit_price.amount,
            reservation_amount=order.reservation_amount.amount,
            business_status=order.business_status,
            communication_status=order.communication_status,
        )


class ApiOrderResponse(OrderResponse):
    """Public representation; stored replay payloads keep their old format."""

    quantity: WireQuantity
    unit_price: SharePrice
    reservation_amount: CashAmount

    @field_serializer("side")
    def serialize_side(self, value) -> Literal["buy"]:
        return "buy"


class ApiCreateOrderRequest(CreateOrderRequest):
    quantity: WireQuantity
    side: Literal["buy"] = "buy"


class ApiCashResponse(CashResponse):
    posted_cash: CashAmount
    active_reservations: CashAmount
    available_cash: CashAmount


class ApiError(ResponseModel):
    message: str


class ApiValidationIssue(ResponseModel):
    location: list[str | int]
    message: str


class ApiValidationError(ApiError):
    errors: list[ApiValidationIssue]
