"""Validated contracts at the partner and operational boundaries."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from .domain import Identifier, Money, Quantity

IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128)]
FailureMode = Literal[
    "normal", "lost_response", "timeout_before", "delayed", "reject"
]
ScenarioName = Literal[
    "lost_response",
    "retry_storm",
    "worker_crash",
    "duplicate_execution",
    "amount_mismatch",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PartnerOrderCreate(Contract):
    client_order_id: Identifier
    account_id: Identifier
    quantity: Quantity
    instrument: Literal["SYNTH-100"] = "SYNTH-100"
    currency: Literal["EUR"] = "EUR"
    side: Literal["BUY"] = "BUY"
    run_id: Identifier | None = None
    mode: FailureMode = "normal"


class Execution(Contract):
    provider: Literal["simulator"] = "simulator"
    execution_id: Identifier
    order_id: Identifier
    account_id: Identifier
    quantity: Quantity
    instrument: Literal["SYNTH-100"] = "SYNTH-100"
    currency: Literal["EUR"] = "EUR"
    amount: Decimal
    executed_at: AwareDatetime

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value):
        if isinstance(value, (float, int, bool)):
            raise ValueError("Amount must be an exact decimal string")
        amount = (
            Money.from_text(value)
            if isinstance(value, str)
            else Money(amount=value)
        )
        if amount.amount <= 0:
            raise ValueError("Execution amount must be positive")
        return amount.amount


class PartnerResult(Contract):
    client_order_id: Identifier
    provider_order_id: Identifier
    status: Literal["ACCEPTED", "FILLED", "REJECTED"]
    observed_at: AwareDatetime
    execution: Execution | None = None


class ReportItem(Contract):
    order_id: Identifier
    execution_id: Identifier | None = None
    status: Literal["ACCEPTED", "FILLED", "REJECTED"]
    amount: Decimal
    executed_at: AwareDatetime

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value):
        if isinstance(value, (float, int, bool)):
            raise ValueError("Amount must be an exact decimal string")
        return (
            Money.from_text(value).amount
            if isinstance(value, str)
            else Money(amount=value).amount
        )


class PartnerReport(Contract):
    provider: Literal["simulator"] = "simulator"
    report_id: Identifier
    cutoff: AwareDatetime
    items: list[ReportItem]


class ScenarioCreate(Contract):
    scenario: ScenarioName
    seed: int = Field(default=42, ge=0, le=2147483647)


class BreakUpdate(Contract):
    status: Literal["INVESTIGATING", "RESOLVED"]
    reason: str = Field(min_length=5, max_length=500)


class CorrectionCreate(Contract):
    reason: str = Field(min_length=5, max_length=500)


def fingerprint(payload: dict) -> str:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(content.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
