"""Versioned public contract; the original demo API remains unchanged."""

import base64
import binascii
import json
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api_formats import ApiJSONResponse
from .auth_v1 import AuthenticatedV1Client
from .db import OrderRow
from .demo import DEMO_ACCOUNT_ID
from .ledger import cash_from_ledger, positions
from .schemas import (
    ApiCashResponse,
    ApiCreateOrderRequest,
    ApiError,
    ApiOrderResponse,
    ApiValidationError,
)
from .services import (
    OrderNotFoundError,
    create_order,
    get_account,
    get_order,
    timeline,
)
from .unit_of_work import SqlAlchemyUnitOfWork

router = APIRouter(
    prefix="/v1",
    default_response_class=ApiJSONResponse,
    responses={
        400: {"model": ApiError},
        401: {"model": ApiError},
        404: {"model": ApiError},
        409: {"model": ApiError},
        422: {"model": ApiValidationError | ApiError},
        503: {"model": ApiError},
    },
)
RequestKey = Annotated[
    str, Field(min_length=1, max_length=100), Header(alias="Idempotency-Key")
]


def owned_account(engine, account_id, client_id):
    return get_account(
        account_id, SqlAlchemyUnitOfWork(engine), client_id=client_id
    )


def owned_order(engine, order_id, client_id):
    with Session(engine) as db:
        account_id = db.scalar(
            select(OrderRow.account_id).where(OrderRow.id == order_id)
        )
    if account_id is None:
        raise OrderNotFoundError("Order not found")
    return get_order(
        account_id, order_id, SqlAlchemyUnitOfWork(engine), client_id=client_id
    )


@router.get("/demo/account", response_model=ApiCashResponse)
def demo_account(request: Request, client_id: AuthenticatedV1Client):
    return ApiCashResponse.from_account(
        owned_account(request.app.state.engine, DEMO_ACCOUNT_ID, client_id)
    )


@router.get("/accounts/{account_id}/cash", response_model=ApiCashResponse)
def account_cash(
    account_id: str, request: Request, client_id: AuthenticatedV1Client
):
    return ApiCashResponse.from_account(
        owned_account(request.app.state.engine, account_id, client_id)
    )


def submit(account_id, payload, request, response, client_id, key):
    body = create_order(
        account_id,
        payload.quantity,
        SqlAlchemyUnitOfWork(request.app.state.engine),
        client_id=client_id,
        key=key,
    )
    response.headers["Location"] = f"/v1/orders/{body['order_id']}"
    return body


@router.post("/demo/orders", status_code=201, response_model=ApiOrderResponse)
def demo_order(
    payload: ApiCreateOrderRequest,
    request: Request,
    response: Response,
    client_id: AuthenticatedV1Client,
    key: RequestKey,
):
    return submit(DEMO_ACCOUNT_ID, payload, request, response, client_id, key)


@router.post(
    "/accounts/{account_id}/orders",
    status_code=201,
    response_model=ApiOrderResponse,
)
def account_order(
    account_id: str,
    payload: ApiCreateOrderRequest,
    request: Request,
    response: Response,
    client_id: AuthenticatedV1Client,
    key: RequestKey,
):
    return submit(account_id, payload, request, response, client_id, key)


@router.get("/orders/{order_id}", response_model=ApiOrderResponse)
def order_read(
    order_id: str, request: Request, client_id: AuthenticatedV1Client
):
    return ApiOrderResponse.from_order(
        owned_order(request.app.state.engine, order_id, client_id)
    )


@router.get("/orders/{order_id}/timeline")
def order_timeline(
    order_id: str, request: Request, client_id: AuthenticatedV1Client
):
    owned_order(request.app.state.engine, order_id, client_id)
    with Session(request.app.state.engine) as db:
        return {
            "data": timeline(db, order_id),
            "pagination": {"next_cursor": None},
        }


def position_page(rows, account_id, cursor, limit):
    after = ""
    if cursor:
        try:
            token = json.loads(
                base64.b64decode(cursor, altchars=b"-_", validate=True)
            )
            if (
                not isinstance(token, dict)
                or token.get("account") != account_id
                or not isinstance(token.get("after"), str)
            ):
                raise ValueError("Cursor scope mismatch")
            after = token["after"]
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise HTTPException(400, "Invalid pagination cursor") from None
    remaining = sorted(
        (row for row in rows if row["instrument"] > after),
        key=lambda row: row["instrument"],
    )
    selected = remaining[:limit]
    next_cursor = None
    if len(remaining) > limit:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "account": account_id,
                    "after": selected[-1]["instrument"],
                }
            ).encode()
        ).decode()
    return {
        "data": [
            dict(row, quantity=format(Decimal(row["quantity"]), ".5f"))
            for row in selected
        ],
        "pagination": {"next_cursor": next_cursor},
    }


@router.get("/accounts/{account_id}/positions")
def account_positions(
    account_id: str,
    request: Request,
    client_id: AuthenticatedV1Client,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
):
    owned_account(request.app.state.engine, account_id, client_id)
    with Session(request.app.state.engine) as db:
        return position_page(
            positions(db, account_id), account_id, cursor, limit
        )


@router.get("/accounts/{account_id}/ledger-balance")
def ledger_balance(
    account_id: str, request: Request, client_id: AuthenticatedV1Client
):
    owned_account(request.app.state.engine, account_id, client_id)
    with Session(request.app.state.engine) as db:
        return {
            "account_id": account_id,
            "posted_cash": format(cash_from_ledger(db, account_id), ".2f"),
        }
