"""Compare dated evidence; differences never directly adjust money."""

from collections import Counter
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import PartnerReport, ReportItem
from .domain import DomainError
from .executions import book_execution
from .models import (
    BreakHistory,
    OrderEvent,
    ProcessedExecution,
    ReconciliationBreak,
    ReconciliationRun,
)


def compare_records(
    local: list[ReportItem], report: PartnerReport
) -> list[dict]:
    local = [item for item in local if item.executed_at <= report.cutoff]
    remote = [
        item for item in report.items if item.executed_at <= report.cutoff
    ]
    findings = []
    for source, items in (("local", local), ("partner", remote)):
        counts = Counter(item.order_id for item in items)
        for order_id, count in counts.items():
            if count > 1:
                findings.append(
                    dict(
                        category="DUPLICATE",
                        order_id=order_id,
                        evidence={"source": source, "count": count},
                    )
                )
    left = {item.order_id: item for item in local}
    right = {item.order_id: item for item in remote}
    for order_id in sorted(left.keys() | right.keys()):
        a, b = left.get(order_id), right.get(order_id)
        evidence = {
            "local": a.model_dump(mode="json") if a else None,
            "partner": b.model_dump(mode="json") if b else None,
        }
        categories = []
        if a is None:
            categories.append("MISSING_LOCAL")
        elif b is None:
            categories.append("MISSING_PROVIDER")
        else:
            if a.amount != b.amount:
                categories.append("AMOUNT_MISMATCH")
            if a.status != b.status or a.execution_id != b.execution_id:
                categories.append("STATUS_MISMATCH")
        for category in categories:
            findings.append(
                dict(category=category, order_id=order_id, evidence=evidence)
            )
    return findings


def reconcile(
    engine,
    report: PartnerReport,
    *,
    local_fixture: list[ReportItem] | None = None,
    order_ids: list[str] | None = None,
) -> str:
    with Session(engine) as db, db.begin():
        if local_fixture is None:
            stmt = select(ProcessedExecution).where(
                ProcessedExecution.provider == report.provider,
                ProcessedExecution.executed_at <= report.cutoff,
            )
            if order_ids is not None:
                stmt = stmt.where(ProcessedExecution.order_id.in_(order_ids))
            local = [
                ReportItem(
                    order_id=row.order_id,
                    execution_id=row.execution_id,
                    status="FILLED",
                    amount=row.payload["amount"],
                    executed_at=row.executed_at,
                )
                for row in db.scalars(stmt)
            ]
            events = (
                select(OrderEvent)
                .where(OrderEvent.kind.in_(["ACCEPTED", "REJECTED"]))
                .order_by(OrderEvent.observed_at)
            )
            if order_ids is not None:
                events = events.where(OrderEvent.order_id.in_(order_ids))
            terminal = {}
            filled_ids = {item.order_id for item in local}
            for event in db.scalars(events):
                observed = event.detail.get("partner_observed_at")
                if observed:
                    item = ReportItem(
                        order_id=event.order_id,
                        status=event.kind,
                        amount="0.00",
                        executed_at=observed,
                    )
                    if (
                        item.executed_at <= report.cutoff
                        and item.order_id not in filled_ids
                    ):
                        terminal[event.order_id] = item
            local.extend(terminal.values())
        else:
            local = local_fixture
        run_id = str(uuid4())
        db.add(
            ReconciliationRun(
                id=run_id,
                provider=report.provider,
                report_id=report.report_id,
                cutoff=report.cutoff,
                source_report=report.model_dump(mode="json"),
            )
        )
        db.flush()
        for finding in compare_records(local, report):
            break_id = str(uuid4())
            db.add(ReconciliationBreak(id=break_id, run_id=run_id, **finding))
            db.flush()
            db.add(
                BreakHistory(
                    id=str(uuid4()),
                    break_id=break_id,
                    status="OPEN",
                    actor="reconciler",
                    reason="Report comparison",
                )
            )
        return run_id


def update_break(
    db: Session, break_id: str, status: str, actor: str, reason: str
) -> None:
    case = db.scalar(
        select(ReconciliationBreak)
        .where(ReconciliationBreak.id == break_id)
        .with_for_update()
    )
    if case is None:
        raise DomainError("Reconciliation break not found")
    allowed = {
        "OPEN": {"INVESTIGATING"},
        "INVESTIGATING": {"RESOLVED"},
        "RESOLVED": set(),
    }
    if status not in allowed[case.status]:
        raise DomainError("Invalid reconciliation status transition")
    case.status = status
    db.add(
        BreakHistory(
            id=str(uuid4()),
            break_id=break_id,
            status=status,
            actor=actor,
            reason=reason,
        )
    )


def recover_break(engine, transport, break_id: str, actor: str) -> str:
    with Session(engine) as db:
        case = db.get(ReconciliationBreak, break_id)
        if case is None or case.category != "MISSING_LOCAL":
            raise DomainError("Recovery requires a missing local execution")
        order_id = case.order_id
    # Network happens after the read transaction/session has closed.
    result = transport.lookup(order_id)
    if result is None or result.status != "FILLED" or result.execution is None:
        raise DomainError("No confirmed execution evidence from partner")
    if (
        result.client_order_id != order_id
        or result.execution.order_id != order_id
    ):
        raise DomainError("Recovery evidence refers to another order")
    with Session(engine) as db, db.begin():
        transaction_id = book_execution(db, result.execution)
        case = db.scalar(
            select(ReconciliationBreak)
            .where(ReconciliationBreak.id == break_id)
            .with_for_update()
        )
        if case.status == "OPEN":
            update_break(
                db,
                break_id,
                "INVESTIGATING",
                actor,
                "Looked up identified partner execution",
            )
        if case.status == "INVESTIGATING":
            update_break(
                db,
                break_id,
                "RESOLVED",
                actor,
                f"Execution booked as {transaction_id}",
            )
        return transaction_id
