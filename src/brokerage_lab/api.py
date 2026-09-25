"""Client API: validate inputs, authorize ownership, call use cases."""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import AuthenticatedClient, DemoAuthSettings
from .config import Settings
from .contracts import IdempotencyKey
from .db import OrderRow, make_engine
from .demo import DEMO_ACCOUNT_ID
from .domain import DomainError, InsufficientFundsError
from .ledger import cash_from_ledger, positions
from .operations import router as operations_router
from .schemas import (
    CashResponse,
    CreateOrderRequest,
    HealthResponse,
    OrderResponse,
)
from .services import (
    AccountNotFoundError,
    OrderNotFoundError,
    create_order,
    get_account,
    get_order,
    timeline,
)
from .unit_of_work import SqlAlchemyUnitOfWork

RequestKey = Annotated[IdempotencyKey, Header(alias="Idempotency-Key")]


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.identities = DemoAuthSettings().identities()
        app.state.engine = engine or make_engine(
            Settings().database_url.get_secret_value()
        )
        yield
        if engine is None:
            app.state.engine.dispose()

    app = FastAPI(
        title="Brokerage Lab",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Synthetic EUR BUY orders with durable idempotency and recovery."
        ),
    )
    app.include_router(operations_router)

    @app.exception_handler(AccountNotFoundError)
    @app.exception_handler(OrderNotFoundError)
    async def missing_resource(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InsufficientFundsError)
    async def insufficient_cash(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Database operation failed; order outcome may be unknown"
                )
            },
        )

    @app.get("/health/live", response_model=HealthResponse)
    def live():
        return HealthResponse()

    @app.get("/health/ready", response_model=HealthResponse)
    def ready(request: Request):
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT id FROM accounts LIMIT 1"))
                connection.execute(
                    text("SELECT id FROM outbox_messages LIMIT 1")
                )
        except SQLAlchemyError:
            raise HTTPException(503, "Database is not ready") from None
        return HealthResponse()

    def read_cash(account_id: str, request: Request, client_id: str):
        account = get_account(
            account_id,
            SqlAlchemyUnitOfWork(request.app.state.engine),
            client_id=client_id,
        )
        return CashResponse.from_account(account)

    @app.get("/demo/account", response_model=CashResponse)
    def demo_account(request: Request, client_id: AuthenticatedClient):
        return read_cash(DEMO_ACCOUNT_ID, request, client_id)

    @app.get("/accounts/{account_id}/cash", response_model=CashResponse)
    def account_cash(
        account_id: str, request: Request, client_id: AuthenticatedClient
    ):
        return read_cash(account_id, request, client_id)

    def submit(account_id, payload, request, response, client_id, key):
        body = create_order(
            account_id,
            payload.quantity,
            SqlAlchemyUnitOfWork(request.app.state.engine),
            client_id=client_id,
            key=key,
        )
        response.headers["Location"] = f"/orders/{body['order_id']}"
        return body

    @app.post("/demo/orders", status_code=201, response_model=OrderResponse)
    def demo_order(
        payload: CreateOrderRequest,
        request: Request,
        response: Response,
        client_id: AuthenticatedClient,
        key: RequestKey,
    ):
        return submit(
            DEMO_ACCOUNT_ID, payload, request, response, client_id, key
        )

    @app.post(
        "/accounts/{account_id}/orders",
        status_code=201,
        response_model=OrderResponse,
    )
    def account_order(
        account_id: str,
        payload: CreateOrderRequest,
        request: Request,
        response: Response,
        client_id: AuthenticatedClient,
        key: RequestKey,
    ):
        return submit(account_id, payload, request, response, client_id, key)

    def owned_order(order_id, request, client_id):
        # Fetch identity only; get_order performs the ownership check.
        with Session(request.app.state.engine) as db:
            account_id = db.scalar(
                select(OrderRow.account_id).where(OrderRow.id == order_id)
            )
        if account_id is None:
            raise OrderNotFoundError("Order not found")
        return get_order(
            account_id,
            order_id,
            SqlAlchemyUnitOfWork(request.app.state.engine),
            client_id=client_id,
        )

    @app.get("/orders/{order_id}", response_model=OrderResponse)
    def order_read(
        order_id: str, request: Request, client_id: AuthenticatedClient
    ):
        return OrderResponse.from_order(
            owned_order(order_id, request, client_id)
        )

    @app.get("/demo/orders/{order_id}", response_model=OrderResponse)
    def demo_order_read(
        order_id: str, request: Request, client_id: AuthenticatedClient
    ):
        order = get_order(
            DEMO_ACCOUNT_ID,
            order_id,
            SqlAlchemyUnitOfWork(request.app.state.engine),
            client_id=client_id,
        )
        return OrderResponse.from_order(order)

    @app.get("/orders/{order_id}/timeline")
    def order_timeline(
        order_id: str, request: Request, client_id: AuthenticatedClient
    ):
        owned_order(order_id, request, client_id)
        with Session(request.app.state.engine) as db:
            return timeline(db, order_id)

    @app.get("/accounts/{account_id}/positions")
    def account_positions(
        account_id: str, request: Request, client_id: AuthenticatedClient
    ):
        read_cash(account_id, request, client_id)
        with Session(request.app.state.engine) as db:
            return positions(db, account_id)

    @app.get("/accounts/{account_id}/ledger-balance")
    def ledger_balance(
        account_id: str, request: Request, client_id: AuthenticatedClient
    ):
        read_cash(account_id, request, client_id)
        with Session(request.app.state.engine) as db:
            return {
                "account_id": account_id,
                "posted_cash": str(cash_from_ledger(db, account_id)),
            }

    return app
