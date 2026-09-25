"""One transaction for execution identity, cash, units and order state."""

from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from .contracts import Execution, PartnerResult, fingerprint
from .db import AccountRow, OrderRow, ReservationRow
from .domain import DomainError
from .ledger import post_journal
from .models import InstrumentMovement, OrderEvent, ProcessedExecution


class ExecutionConflict(DomainError):
    """An execution identity or order conflicts with durable evidence."""


def record_event(
    db: Session,
    order_id: str,
    kind: str,
    detail: dict | None = None,
    source: str = "partner",
) -> None:
    db.add(
        OrderEvent(
            id=str(uuid4()),
            order_id=order_id,
            kind=kind,
            source=source,
            detail=detail or {},
        )
    )


def book_execution(db: Session, execution: Execution) -> str:
    payload = execution.model_dump(mode="json")
    digest = fingerprint(payload)
    lock_hash = fingerprint(
        dict(provider=execution.provider, execution_id=execution.execution_id)
    )[:15]
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(lock_hash, 16)}
    )
    account = db.scalar(
        select(AccountRow)
        .where(AccountRow.id == execution.account_id)
        .with_for_update()
    )
    previous = db.get(
        ProcessedExecution, (execution.provider, execution.execution_id)
    )
    if previous:
        if previous.fingerprint != digest:
            raise ExecutionConflict("Execution identity payload conflict")
        return previous.transaction_id
    order = db.get(OrderRow, execution.order_id)
    if account is None or order is None or order.account_id != account.id:
        raise ExecutionConflict("Execution account or order is unknown")
    if (
        order.quantity != execution.quantity
        or order.instrument != execution.instrument
        or order.reservation_amount != execution.amount
    ):
        raise ExecutionConflict("Execution terms differ from the order")
    if order.business_status in {"FILLED", "REJECTED"}:
        raise ExecutionConflict(
            "A terminal order cannot receive a new execution"
        )
    reservation = db.get(ReservationRow, order.id)
    if reservation is None or reservation.amount != execution.amount:
        raise ExecutionConflict("Execution has no matching reservation")
    transaction_id = post_journal(
        db,
        account_id=account.id,
        amount=-execution.amount,
        kind="EXECUTION",
        reference=f"{execution.provider}:{execution.execution_id}",
        reason="Confirmed full synthetic BUY execution",
    )
    db.add(
        ProcessedExecution(
            provider=execution.provider,
            execution_id=execution.execution_id,
            order_id=order.id,
            fingerprint=digest,
            payload=payload,
            transaction_id=transaction_id,
            executed_at=execution.executed_at,
        )
    )
    db.add(
        InstrumentMovement(
            id=str(uuid4()),
            account_id=account.id,
            transaction_id=transaction_id,
            instrument=execution.instrument,
            quantity=execution.quantity,
        )
    )
    account.posted_cash -= execution.amount
    db.delete(reservation)
    order.business_status = "FILLED"
    order.communication_status = "CONFIRMED"
    record_event(
        db,
        order.id,
        "EXECUTION_BOOKED",
        {
            "execution_id": execution.execution_id,
            "journal_transaction_id": transaction_id,
        },
    )
    db.flush()
    return transaction_id


def apply_partner_result(db: Session, result: PartnerResult) -> None:
    if result.status == "FILLED":
        if (
            result.execution is None
            or result.execution.order_id != result.client_order_id
        ):
            raise ExecutionConflict(
                "Filled result must identify its execution"
            )
        book_execution(db, result.execution)
        return
    order = db.get(OrderRow, result.client_order_id)
    if order is None:
        raise ExecutionConflict("Partner result has no local order")
    db.scalar(
        select(AccountRow)
        .where(AccountRow.id == order.account_id)
        .with_for_update()
    )
    db.refresh(order)
    if order.business_status in {"FILLED", "REJECTED"}:
        if result.status not in {"ACCEPTED", order.business_status}:
            raise ExecutionConflict("Conflicting terminal partner result")
        return
    if result.execution is not None:
        raise ExecutionConflict(
            "Non-filled result cannot contain an execution"
        )
    order.business_status = result.status
    order.communication_status = "CONFIRMED"
    if result.status == "REJECTED":
        db.execute(
            delete(ReservationRow).where(ReservationRow.order_id == order.id)
        )
    record_event(
        db,
        order.id,
        result.status,
        {
            "provider_order_id": result.provider_order_id,
            "partner_observed_at": result.observed_at.isoformat(),
        },
    )
