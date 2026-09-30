"""Balanced EUR journals; account cash is a rebuildable projection."""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import AccountRow, ReservationRow
from .domain import DomainError, Money
from .models import InstrumentMovement, JournalEntry, JournalTransaction


def post_journal(
    db: Session,
    *,
    account_id: str,
    amount: Decimal,
    kind: str,
    reference: str,
    reason: str,
    correction_of: str | None = None,
) -> str:
    amount = Money(amount=amount).amount
    if not amount:
        raise DomainError("Journal amount must be nonzero")
    transaction_id = str(uuid4())
    db.add(
        JournalTransaction(
            id=transaction_id,
            account_id=account_id,
            kind=kind,
            reference=reference,
            reason=reason,
            correction_of=correction_of,
        )
    )
    db.flush()
    for bucket, signed_amount in (
        ("CLIENT_CASH", amount),
        ("CLEARING", -amount),
    ):
        db.add(
            JournalEntry(
                id=str(uuid4()),
                transaction_id=transaction_id,
                bucket=bucket,
                amount=signed_amount,
                currency="EUR",
            )
        )
    db.flush()
    return transaction_id


def fund_account(
    db: Session,
    account_id: str,
    client_id: str,
    amount: Decimal = Decimal("1000.00"),
) -> AccountRow:
    amount = Money(amount=amount).amount
    if amount <= 0:
        raise DomainError("Demo funding must be positive")
    account = AccountRow(
        id=account_id,
        owner_client_id=client_id,
        posted_cash=amount,
        currency="EUR",
    )
    db.add(account)
    db.flush()
    post_journal(
        db,
        account_id=account_id,
        amount=amount,
        kind="FUNDING",
        reference=f"funding:{account_id}",
        reason="Synthetic demo funding",
    )
    return account


def cash_from_ledger(db: Session, account_id: str) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(JournalEntry.amount), 0))
        .join(JournalTransaction)
        .where(
            JournalTransaction.account_id == account_id,
            JournalEntry.bucket == "CLIENT_CASH",
        )
    )
    return Money(amount=Decimal(db.scalar(stmt))).amount


def positions(
    db: Session,
    account_id: str,
    *,
    after: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    stmt = (
        select(
            InstrumentMovement.instrument,
            func.sum(InstrumentMovement.quantity),
        )
        .where(InstrumentMovement.account_id == account_id)
        .group_by(InstrumentMovement.instrument)
        .order_by(InstrumentMovement.instrument)
    )
    if after is not None:
        stmt = stmt.where(InstrumentMovement.instrument > after)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [
        dict(instrument=instrument, quantity=quantity)
        for instrument, quantity in db.execute(stmt)
    ]


def rebuild_cash(db: Session, account_id: str) -> Decimal:
    account = db.scalar(
        select(AccountRow).where(AccountRow.id == account_id).with_for_update()
    )
    if account is None:
        raise DomainError("Account not found")
    amount = cash_from_ledger(db, account_id)
    reserved = db.scalar(
        select(func.coalesce(func.sum(ReservationRow.amount), 0)).where(
            ReservationRow.account_id == account_id
        )
    )
    if amount < reserved:
        raise DomainError("Ledger projection cannot cover active reservations")
    account.posted_cash = amount
    return amount


def reverse_journal(db: Session, transaction_id: str, reason: str) -> str:
    original = db.get(JournalTransaction, transaction_id)
    if original is None or original.kind != "EXECUTION":
        raise DomainError("Only an execution journal can be reversed")
    db.scalar(
        select(AccountRow)
        .where(AccountRow.id == original.account_id)
        .with_for_update()
    )
    existing = db.scalar(
        select(JournalTransaction).where(
            JournalTransaction.correction_of == transaction_id
        )
    )
    if existing:
        return existing.id
    amount = db.scalar(
        select(JournalEntry.amount).where(
            JournalEntry.transaction_id == transaction_id,
            JournalEntry.bucket == "CLIENT_CASH",
        )
    )
    correction_id = post_journal(
        db,
        account_id=original.account_id,
        amount=-amount,
        kind="CORRECTION",
        reference=f"correction:{transaction_id}",
        reason=reason,
        correction_of=transaction_id,
    )
    movement = db.scalar(
        select(InstrumentMovement).where(
            InstrumentMovement.transaction_id == transaction_id
        )
    )
    if movement is not None:
        db.add(
            InstrumentMovement(
                id=str(uuid4()),
                account_id=original.account_id,
                transaction_id=correction_id,
                instrument=movement.instrument,
                quantity=-movement.quantity,
            )
        )
    rebuild_cash(db, original.account_id)
    return correction_id
