"""Documented wire formats, separate from durable execution fingerprints."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BeforeValidator,
    PlainSerializer,
    WithJsonSchema,
)

from .domain import Quantity


def parse_quantity(value):
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,5})?", value):
            raise ValueError("Use a decimal string with at most 5 places")
        number = Decimal(value)
        if number != number.to_integral_value():
            raise ValueError("This lab supports whole shares only")
        return int(number)
    return value


WireQuantity = Annotated[
    Quantity,
    BeforeValidator(parse_quantity),
    PlainSerializer(
        lambda value: format(Decimal(value), ".5f"),
        return_type=str,
        when_used="json",
    ),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]{1,5})?$"},
                {"type": "integer", "minimum": 1},
            ],
            "description": "Whole shares; integer input is a legacy alias.",
        },
        mode="validation",
    ),
    WithJsonSchema(
        {"type": "string", "pattern": r"^[0-9]+\.[0-9]{5}$"},
        mode="serialization",
    ),
]
CashAmount = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: format(value, ".2f"), return_type=str, when_used="json"
    ),
]
SharePrice = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: format(value, ".4f"), return_type=str, when_used="json"
    ),
]


def validate_timestamp_input(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("Timestamp must be ISO 8601 with a timezone")
    if isinstance(value, str) and not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})",
        value,
    ):
        raise ValueError("Timestamp must be ISO 8601 with a timezone")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(validate_timestamp_input)]


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def wire_timestamps(value, key=""):
    if isinstance(value, dict):
        return {k: wire_timestamps(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [wire_timestamps(v, key) for v in value]
    if isinstance(value, datetime):
        return timestamp(value)
    if isinstance(value, str) and (
        key.endswith("_at") or key in {"timestamp", "cutoff"}
    ):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return timestamp(parsed)
    return value


class ApiJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(wire_timestamps(content))
