"""Boundary formats must not alter exact domain values or durable evidence."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from brokerage_lab.api_formats import (
    CashAmount,
    SharePrice,
    Timestamp,
    WireQuantity,
    timestamp,
    wire_timestamps,
)
from brokerage_lab.contracts import Execution, fingerprint


def test_decimal_wire_scales_are_exact():
    assert TypeAdapter(CashAmount).dump_json(Decimal("12")) == b'"12.00"'
    assert TypeAdapter(SharePrice).dump_json(Decimal("100")) == b'"100.0000"'
    quantity = TypeAdapter(WireQuantity)
    value = quantity.validate_python("9999999999999999.00000")
    assert value == 9999999999999999
    assert quantity.dump_json(value) == b'"9999999999999999.00000"'


@pytest.mark.parametrize("value", [True, 8.0, "8e0", "8.000001", "0.5"])
def test_quantity_does_not_round_or_accept_float(value):
    with pytest.raises(ValidationError):
        TypeAdapter(WireQuantity).validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-28T15:07:12.345+0200",
        "2026-09-28T15:07:12.345+02:00",
        "2026-09-28T13:07:12.345Z",
    ],
)
def test_timestamp_normalizes_timezone_at_public_boundary(value):
    parsed = TypeAdapter(Timestamp).validate_python(value)
    assert timestamp(parsed) == "2026-09-28T13:07:12.345Z"


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-28T13:07:12",
        "2026-09-28",
        1790600832,
        True,
        datetime(2026, 9, 28),
    ],
)
def test_ambiguous_timestamp_is_rejected(value):
    with pytest.raises(ValidationError):
        TypeAdapter(Timestamp).validate_python(value)


def test_presentation_does_not_change_persisted_execution_fingerprint():
    event = Execution(
        execution_id="e1",
        order_id="o1",
        account_id="a1",
        quantity=8,
        amount="800.00",
        executed_at=datetime(
            2026, 9, 28, microsecond=123456, tzinfo=timezone.utc
        ),
    )
    stored = event.model_dump(mode="json")
    original_digest = fingerprint(stored)
    public = wire_timestamps(stored)
    assert public["executed_at"] == "2026-09-28T00:00:00.123Z"
    assert stored["executed_at"].endswith(".123456Z")
    assert fingerprint(stored) == original_digest
