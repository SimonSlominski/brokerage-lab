"""Local demo account and order API; multi-client authorization is future work."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from brokerage_lab.config import Settings
from brokerage_lab.db import make_engine
from brokerage_lab.demo import DEMO_ACCOUNT_ID
from brokerage_lab.domain import Identifier, InsufficientFundsError
from brokerage_lab.schemas import (
    CashResponse,
    CreateOrderRequest,
    HealthResponse,
    OrderResponse,
)
from brokerage_lab.services import (
    AccountNotFoundError,
    OrderNotFoundError,
    get_account,
    get_order,
    submit_order,
)
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = (
            engine
            if engine is not None
            else make_engine(Settings().database_url.get_secret_value())
        )
        try:
            yield
        finally:
            if engine is None:
                app.state.engine.dispose()

    app = FastAPI(
        title="Brokerage Lab",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Local demo only: reserve EUR cash for synthetic BUY orders. "
            "No partner execution, authentication or request idempotency yet."
        ),
    )

    @app.get("/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        return HealthResponse()

    @app.get("/health/ready", response_model=HealthResponse)
    def ready(request: Request) -> HealthResponse:
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                connection.execute(text("SELECT id FROM accounts LIMIT 1"))
        except SQLAlchemyError:
            raise HTTPException(
                status_code=503, detail="Database is not ready"
            ) from None
        return HealthResponse()

    @app.get("/demo/account", response_model=CashResponse)
    def demo_account(request: Request) -> CashResponse:
        try:
            account = get_account(
                DEMO_ACCOUNT_ID, SqlAlchemyUnitOfWork(request.app.state.engine)
            )
        except AccountNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="Demo account not found; run the demo seed command",
            ) from None
        except SQLAlchemyError:
            raise HTTPException(
                status_code=503, detail="Database is not ready"
            ) from None
        return CashResponse.from_account(account)

    @app.post(
        "/demo/orders",
        response_model=OrderResponse,
        status_code=201,
        responses={
            404: {"description": "Demo account is missing"},
            409: {"description": "Insufficient available cash; no writes"},
            503: {"description": "Database operation failed"},
        },
        description=(
            "Create a local PENDING order and reserve cash atomically for "
            "the fixed demo account. Each request creates a new order; "
            "retries are not yet idempotent. This does not execute a trade."
        ),
    )
    def create_demo_order(
        payload: CreateOrderRequest, request: Request, response: Response
    ) -> OrderResponse:
        try:
            order = submit_order(
                DEMO_ACCOUNT_ID,
                payload.quantity,
                SqlAlchemyUnitOfWork(request.app.state.engine),
            )
        except AccountNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="Demo account not found; run the demo seed command",
            ) from None
        except InsufficientFundsError:
            raise HTTPException(
                status_code=409, detail="Insufficient available cash"
            ) from None
        except SQLAlchemyError:
            # A connection loss during commit may leave the outcome unknown.
            raise HTTPException(
                status_code=503,
                detail="Database operation failed; order outcome may be unknown",
            ) from None
        response.headers["Location"] = f"/demo/orders/{order.id}"
        return OrderResponse.from_order(order)

    @app.get("/demo/orders/{order_id}", response_model=OrderResponse)
    def read_demo_order(
        order_id: Identifier, request: Request
    ) -> OrderResponse:
        try:
            order = get_order(
                DEMO_ACCOUNT_ID,
                order_id,
                SqlAlchemyUnitOfWork(request.app.state.engine),
            )
        except OrderNotFoundError:
            raise HTTPException(
                status_code=404, detail="Order not found"
            ) from None
        except SQLAlchemyError:
            raise HTTPException(
                status_code=503, detail="Database is not ready"
            ) from None
        return OrderResponse.from_order(order)

    return app
