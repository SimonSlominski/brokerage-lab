"""Operator-only recovery and observations; failure controls are local-only."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import OperatorIdentity, require_demo_mode
from .config import Settings
from .contracts import (
    BreakUpdate,
    CorrectionCreate,
    Execution,
    ScenarioCreate,
    utc_now,
)
from .db import OrderRow
from .executions import book_execution
from .ledger import reverse_journal
from .models import (
    BreakHistory,
    OutboxMessage,
    ProcessedExecution,
    ReconciliationBreak,
    ReconciliationRun,
    ScenarioRun,
)
from .reconciliation import reconcile, recover_break, update_break
from .transport import PartnerTransport

router = APIRouter()


def partner_transport() -> PartnerTransport:
    settings = Settings()
    return PartnerTransport(
        settings.partner_url, settings.partner_api_key.get_secret_value()
    )


def metrics(db: Session) -> dict:
    oldest = db.scalar(
        select(func.min(OutboxMessage.created_at)).where(
            OutboxMessage.status.in_(["PENDING", "CLAIMED", "ERROR"])
        )
    )
    return dict(
        oldest_outbox_age_seconds=max(0, (utc_now() - oldest).total_seconds())
        if oldest
        else 0,
        retry_count=db.scalar(
            select(
                func.coalesce(
                    func.sum(func.greatest(OutboxMessage.attempts - 1, 0)), 0
                )
            )
        ),
        unknown_external_outcomes=db.scalar(
            select(func.count())
            .select_from(OrderRow)
            .where(OrderRow.communication_status == "UNKNOWN")
        ),
        open_reconciliation_breaks=db.scalar(
            select(func.count())
            .select_from(ReconciliationBreak)
            .where(ReconciliationBreak.status != "RESOLVED")
        ),
        unreconciled_executions=db.scalar(
            select(func.count())
            .select_from(ReconciliationBreak)
            .where(
                ReconciliationBreak.category.in_(
                    ["MISSING_LOCAL", "MISSING_PROVIDER"]
                ),
                ReconciliationBreak.status != "RESOLVED",
            )
        ),
        error_queue=db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.status == "ERROR")
        ),
        processed_executions=db.scalar(
            select(func.count()).select_from(ProcessedExecution)
        ),
    )


@router.get("/metrics")
def read_metrics(request: Request, operator: OperatorIdentity):
    with Session(request.app.state.engine) as db:
        return metrics(db)


@router.post("/internal/executions")
def execution_received(
    payload: Execution, request: Request, operator: OperatorIdentity
):
    with Session(request.app.state.engine) as db, db.begin():
        transaction_id = book_execution(db, payload)
    return {"journal_transaction_id": transaction_id}


@router.post("/reconciliation-runs", status_code=201)
def run_reconciliation(request: Request, operator: OperatorIdentity):
    transport = partner_transport()
    try:
        report = transport.report()
    finally:
        transport.close()
    run_id = reconcile(request.app.state.engine, report)
    return {"run_id": run_id}


@router.get("/reconciliation-runs/{run_id}")
def read_reconciliation(
    run_id: str, request: Request, operator: OperatorIdentity
):
    from fastapi import HTTPException

    with Session(request.app.state.engine) as db:
        run = db.get(ReconciliationRun, run_id)
        if run is None:
            raise HTTPException(404, "Reconciliation run not found")
        breaks = db.scalars(
            select(ReconciliationBreak).where(
                ReconciliationBreak.run_id == run_id
            )
        ).all()
        return dict(
            run_id=run.id,
            report_id=run.report_id,
            cutoff=run.cutoff,
            breaks=[
                dict(
                    id=case.id,
                    category=case.category,
                    status=case.status,
                    evidence=case.evidence,
                )
                for case in breaks
            ],
        )


@router.patch("/reconciliation-breaks/{break_id}")
def change_break(
    break_id: str,
    payload: BreakUpdate,
    request: Request,
    operator: OperatorIdentity,
):
    with Session(request.app.state.engine) as db, db.begin():
        update_break(db, break_id, payload.status, operator, payload.reason)
    return {"status": payload.status}


@router.get("/reconciliation-breaks/{break_id}/history")
def break_history(break_id: str, request: Request, operator: OperatorIdentity):
    with Session(request.app.state.engine) as db:
        rows = db.scalars(
            select(BreakHistory)
            .where(BreakHistory.break_id == break_id)
            .order_by(BreakHistory.created_at)
        )
        return [
            dict(
                status=row.status,
                actor=row.actor,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.post("/reconciliation-breaks/{break_id}/recover")
def recover(break_id: str, request: Request, operator: OperatorIdentity):
    transport = partner_transport()
    try:
        result = recover_break(
            request.app.state.engine, transport, break_id, operator
        )
    finally:
        transport.close()
    return {"journal_transaction_id": result}


@router.post("/journal/{transaction_id}/reverse")
def reverse(
    transaction_id: str,
    payload: CorrectionCreate,
    request: Request,
    operator: OperatorIdentity,
):
    with Session(request.app.state.engine) as db, db.begin():
        result = reverse_journal(db, transaction_id, payload.reason)
    return {"correction_id": result}


@router.post("/lab/runs", status_code=201)
def run_scenario(
    payload: ScenarioCreate, request: Request, operator: OperatorIdentity
):
    from .scenarios import execute_scenario

    require_demo_mode()
    return execute_scenario(
        request.app.state.engine, payload.scenario, payload.seed
    )


@router.get("/lab/runs/{run_id}")
def read_scenario(run_id: str, request: Request, operator: OperatorIdentity):
    from fastapi import HTTPException

    with Session(request.app.state.engine) as db:
        run = db.get(ScenarioRun, run_id)
        if run is None:
            raise HTTPException(404, "Scenario run not found")
        return dict(
            run_id=run.id,
            scenario=run.scenario,
            seed=run.seed,
            status=run.status,
            evidence=run.evidence,
            error=run.error,
        )


@router.get("/lab", response_class=HTMLResponse)
def panel(request: Request, operator: OperatorIdentity):
    from .panel import render_panel

    return render_panel(request.app.state.engine)
