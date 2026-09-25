"""Independent durable execution simulator accessed only through HTTP."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from secrets import compare_digest
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import AwareDatetime
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Settings
from .contracts import (
    Execution,
    PartnerOrderCreate,
    PartnerReport,
    PartnerResult,
    ReportItem,
    fingerprint,
    utc_now,
)
from .db import make_engine
from .partner_models import PartnerOrder, PartnerReportRecord


def create_app(engine=None, token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = engine or make_engine(
            Settings().database_url.get_secret_value()
        )
        app.state.token = (
            token or Settings().partner_api_key.get_secret_value()
        )
        yield
        if engine is None:
            app.state.engine.dispose()

    app = FastAPI(title="Independent Partner Simulator", lifespan=lifespan)

    def authorize(x_partner_key: Annotated[str, Header()] = ""):
        if not app.state.token or not compare_digest(
            x_partner_key.encode(), app.state.token.encode()
        ):
            raise HTTPException(401, "Invalid partner credential")

    @app.get("/health")
    def health():
        with app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM partner_orders LIMIT 1"))
        return {"status": "ok"}

    @app.post(
        "/orders",
        dependencies=[Depends(authorize)],
        response_model=PartnerResult,
    )
    async def create_order(obj_in: PartnerOrderCreate):
        if obj_in.mode != "normal" and Settings().app_env != "development":
            raise HTTPException(403, "Failure modes require development mode")
        if obj_in.mode == "timeout_before":
            await asyncio.sleep(1)
            raise HTTPException(504, "Simulated timeout before acceptance")
        digest = fingerprint(obj_in.model_dump(mode="json", exclude={"mode"}))
        lock_hash = fingerprint({"id": obj_in.client_order_id})[:15]
        with Session(app.state.engine) as db, db.begin():
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int(lock_hash, 16)},
            )
            row = db.get(PartnerOrder, obj_in.client_order_id)
            first_delivery = row is None
            if row:
                if row.fingerprint != digest:
                    raise HTTPException(
                        422, "Partner identity payload conflict"
                    )
                row.deliveries += 1
                body = row.result
            else:
                now = utc_now()
                rejected = obj_in.mode == "reject"
                execution = (
                    None
                    if rejected
                    else Execution(
                        execution_id=str(uuid4()),
                        order_id=obj_in.client_order_id,
                        account_id=obj_in.account_id,
                        quantity=obj_in.quantity,
                        amount=Decimal("100.00") * obj_in.quantity,
                        executed_at=now,
                    )
                )
                result = PartnerResult(
                    client_order_id=obj_in.client_order_id,
                    provider_order_id=str(uuid4()),
                    status="REJECTED" if rejected else "FILLED",
                    observed_at=now,
                    execution=execution,
                )
                body = result.model_dump(mode="json")
                db.add(
                    PartnerOrder(
                        client_order_id=obj_in.client_order_id,
                        provider_order_id=result.provider_order_id,
                        fingerprint=digest,
                        result=body,
                        run_id=obj_in.run_id,
                    )
                )
        # The commit has completed. No database transaction spans this delay.
        if first_delivery and obj_in.mode in {"lost_response", "delayed"}:
            await asyncio.sleep(1 if obj_in.mode == "lost_response" else 0.05)
        return body

    @app.get(
        "/orders/{client_order_id}",
        dependencies=[Depends(authorize)],
        response_model=PartnerResult,
    )
    def read_order(client_order_id: str):
        with Session(app.state.engine) as db:
            row = db.get(PartnerOrder, client_order_id)
            if row is None:
                raise HTTPException(404, "Partner order not found")
            return row.result

    @app.get(
        "/reports",
        dependencies=[Depends(authorize)],
        response_model=PartnerReport,
    )
    def report(cutoff: AwareDatetime | None = None, run_id: str | None = None):
        cutoff = cutoff or utc_now()
        with Session(app.state.engine) as db, db.begin():
            stmt = select(PartnerOrder)
            if run_id is not None:
                stmt = stmt.where(PartnerOrder.run_id == run_id)
            items = []
            for row in db.scalars(stmt):
                result = PartnerResult.model_validate(row.result)
                if result.observed_at > cutoff:
                    continue
                execution = result.execution
                items.append(
                    ReportItem(
                        order_id=result.client_order_id,
                        execution_id=execution.execution_id
                        if execution
                        else None,
                        amount=execution.amount
                        if execution
                        else Decimal("0.00"),
                        status=result.status,
                        executed_at=result.observed_at,
                    )
                )
            body = PartnerReport(
                report_id=str(uuid4()), cutoff=cutoff, items=items
            )
            db.add(
                PartnerReportRecord(
                    id=body.report_id, body=body.model_dump(mode="json")
                )
            )
        return body

    @app.post("/demo/reports", dependencies=[Depends(authorize)])
    def store_report_fixture(payload: PartnerReport):
        if Settings().app_env != "development":
            raise HTTPException(
                403, "Report fixtures require development mode"
            )
        body = payload.model_dump(mode="json")
        with Session(app.state.engine) as db, db.begin():
            existing = db.get(PartnerReportRecord, payload.report_id)
            if existing and existing.body != body:
                raise HTTPException(422, "Report identity payload conflict")
            if not existing:
                db.add(PartnerReportRecord(id=payload.report_id, body=body))
        return {"report_id": payload.report_id}

    @app.get(
        "/reports/{report_id}",
        dependencies=[Depends(authorize)],
        response_model=PartnerReport,
    )
    def read_report(report_id: str):
        with Session(app.state.engine) as db:
            report = db.get(PartnerReportRecord, report_id)
            if report is None:
                raise HTTPException(404, "Partner report not found")
            return report.body

    return app
