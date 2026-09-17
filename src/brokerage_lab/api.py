"""Local read-only demo API. Order submission arrives with access control later in T1."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from brokerage_lab.config import Settings
from brokerage_lab.db import make_engine
from brokerage_lab.demo import DEMO_ACCOUNT_ID
from brokerage_lab.schemas import CashResponse, HealthResponse
from brokerage_lab.services import AccountNotFoundError, get_account
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = (
            engine if engine is not None else make_engine(Settings().database_url.get_secret_value())
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
        description="Local brokerage domain and PostgreSQL foundation. Demo reads only.",
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
            raise HTTPException(status_code=503, detail="Database is not ready") from None
        return HealthResponse()

    @app.get("/demo/account", response_model=CashResponse)
    def demo_account(request: Request) -> CashResponse:
        try:
            account = get_account(DEMO_ACCOUNT_ID, SqlAlchemyUnitOfWork(request.app.state.engine))
        except AccountNotFoundError:
            raise HTTPException(
                status_code=404, detail="Demo account not found; run the demo seed command"
            ) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Database is not ready") from None
        return CashResponse.from_account(account)

    return app
